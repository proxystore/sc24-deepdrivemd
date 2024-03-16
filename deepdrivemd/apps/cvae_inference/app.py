from pathlib import Path

import numpy as np
import pandas as pd
import torch
from mdlearn.nn.models.vae.symmetric_conv2d_vae import SymmetricConv2dVAETrainer
from sklearn.neighbors import LocalOutlierFactor

from proxystore.store.future import Future
from proxystore.stream.interface import StreamConsumer, StreamProducer
from proxystore.stream.shims.redis import RedisPublisher, RedisSubscriber

from deepdrivemd.api import Application
from deepdrivemd.apps.cvae_inference import (
    CVAEInferenceInput,
    CVAEInferenceOutput,
    CVAEInferenceSettings,
)
from deepdrivemd.apps.cvae_train import CVAESettings


class CVAEInferenceApplication(Application):
    config: CVAEInferenceSettings

    def __init__(
        self,
        config: CVAEInferenceSettings,
        model_weight_path: Path,
        redis_host: str,
        redis_port: int,
        stop_inference: Future[bool],
    ) -> None:
        super().__init__(config)

        # Initialize the model
        self.cvae_settings = CVAESettings.from_yaml(
            self.config.cvae_settings_yaml
        ).dict()
        self.trainer = SymmetricConv2dVAETrainer(**self.cvae_settings)

        # Load model weights to use for inference
        checkpoint = torch.load(model_weight_path, map_location=self.trainer.device)
        self.trainer.model.load_state_dict(checkpoint["model_state_dict"])

        store = stop_inference._factory.get_store()
        publisher = RedisPublisher(redis_host, redis_port)
        subscriber = RedisSubscriber(redis_host, redis_port, "inference-input")
        self.producer = StreamProducer(publisher, stores={"inference-output": store})
        self.consumer = StreamConsumer(subscriber)
        self.stop_inference = stop_inference

    def run(self) -> None:
        for metadata, input_data in self.consumer.iter_objects_with_metadata():
            # Note: it's possible we could get stuck waiting on the next object
            # if the stop_inference flag is set after we check it
            # and no new items are added to the inference-input stream.
            output_data = self.infer(input_data)
            self.producer.send("inference-output", output_data, metadata=metadata)
            if self.stop_inference.done():
                break

        self.producer.close(stores=False)
        self.consumer.close(stores=False)

    def infer(self, input_data: CVAEInferenceInput) -> CVAEInferenceOutput:
        # Log the input data
        input_data.dump_yaml(self.workdir / "input.yaml")

        # Load data
        contact_maps = np.concatenate(
            [np.load(p, allow_pickle=True) for p in input_data.contact_map_paths]
        )
        _rmsds = [np.load(p) for p in input_data.rmsd_paths]
        rmsds = np.concatenate(_rmsds)
        lengths = [len(d) for d in _rmsds]  # Number of frames in each simulation
        sim_frames = np.concatenate([np.arange(i) for i in lengths])
        sim_dirs = np.concatenate(
            [[str(p.parent)] * l for p, l in zip(input_data.rmsd_paths, lengths)]
        )
        assert len(rmsds) == len(sim_frames) == len(sim_dirs)

        # Generate latent embeddings in inference mode
        embeddings, *_ = self.trainer.predict(
            X=contact_maps, inference_batch_size=self.config.inference_batch_size
        )
        np.save(self.workdir / "embeddings.npy", embeddings)

        # Perform LocalOutlierFactor outlier detection on embeddings
        embeddings = np.nan_to_num(embeddings, nan=0.0)
        clf = LocalOutlierFactor(n_jobs=self.config.sklearn_num_jobs)
        clf.fit(embeddings)

        # Get best scores and corresponding indices where smaller
        # RMSDs are closer to folded state and smaller LOF score
        # is more of an outlier
        df = (
            pd.DataFrame(
                {
                    "rmsd": rmsds,
                    "lof": clf.negative_outlier_factor_,
                    "sim_dirs": sim_dirs,
                    "sim_frames": sim_frames,
                }
            )
            .sort_values("lof")  # First sort by lof score
            .head(self.config.num_outliers)  # Take the smallest num_outliers lof scores
            .sort_values("rmsd")  # Finally, sort the smallest lof scores by rmsd
        )

        df.to_csv(self.workdir / "outliers.csv")

        # Map each of the selections back to the correct simulation file and frame
        return CVAEInferenceOutput(
            sim_dirs=list(map(Path, df.sim_dirs)), sim_frames=list(df.sim_frames)
        )
