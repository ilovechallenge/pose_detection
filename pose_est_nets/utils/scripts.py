"""Helper functions to build pipeline components from config dictionary."""

import imgaug.augmenters as iaa
from omegaconf import DictConfig, ListConfig, OmegaConf
from typeguard import typechecked
from typing import Dict, Union

from pose_est_nets.datasets.datamodules import BaseDataModule, UnlabeledDataModule
from pose_est_nets.datasets.datasets import BaseTrackingDataset, HeatmapDataset
from pose_est_nets.losses.factory import LossFactory
from pose_est_nets.models.heatmap_tracker import (
    HeatmapTracker,
    SemiSupervisedHeatmapTracker,
)
from pose_est_nets.models.regression_tracker import (
    RegressionTracker,
    SemiSupervisedRegressionTracker,
)
from pose_est_nets.utils.io import check_if_semi_supervised


@typechecked
def get_imgaug_tranform(cfg: DictConfig) -> iaa.Sequential:
    # define data transform pipeline
    data_transform = iaa.Resize(
        {
            "height": cfg.data.image_resize_dims.height,
            "width": cfg.data.image_resize_dims.width,
        }
    )
    return iaa.Sequential([data_transform])


@typechecked
def get_dataset(
    cfg: DictConfig, data_dir: str, imgaug_transform: iaa.Sequential
) -> Union[BaseTrackingDataset, HeatmapDataset]:

    if cfg.model.model_type == "regression":
        dataset = BaseTrackingDataset(
            root_directory=data_dir,
            csv_path=cfg.data.csv_file,
            header_rows=OmegaConf.to_object(cfg.data.header_rows),
            imgaug_transform=imgaug_transform,
        )
    elif cfg.model.model_type == "heatmap":
        dataset = HeatmapDataset(
            root_directory=data_dir,
            csv_path=cfg.data.csv_file,
            header_rows=OmegaConf.to_object(cfg.data.header_rows),
            imgaug_transform=imgaug_transform,
            downsample_factor=cfg.data.downsample_factor,
        )
    else:
        raise NotImplementedError(
            "%s is an invalid cfg.model.model_type" % cfg.model.model_type
        )
    return dataset


@typechecked
def get_datamodule(
    cfg: DictConfig, dataset: Union[BaseTrackingDataset, HeatmapDataset], video_dir: str
) -> Union[BaseDataModule, UnlabeledDataModule]:

    semi_supervised = check_if_semi_supervised(cfg.model.losses_to_use)
    if not semi_supervised:
        if not (cfg.training.gpu_id, int):
            raise NotImplementedError(
                "Cannot currently fit fully supervised model on multiple gpus"
            )
        data_module = BaseDataModule(
            dataset=dataset,
            train_batch_size=cfg.training.train_batch_size,
            val_batch_size=cfg.training.val_batch_size,
            test_batch_size=cfg.training.test_batch_size,
            num_workers=cfg.training.num_workers,
            train_probability=cfg.training.train_prob,
            val_probability=cfg.training.val_prob,
            train_frames=cfg.training.train_frames,
            torch_seed=cfg.training.rng_seed_data_pt,
        )
    else:
        if not (cfg.training.gpu_id, int):
            raise NotImplementedError(
                "Cannot currently fit semi-supervised model on multiple gpus"
            )
        data_module = UnlabeledDataModule(
            dataset=dataset,
            video_paths_list=video_dir,
            train_batch_size=cfg.training.train_batch_size,
            val_batch_size=cfg.training.val_batch_size,
            test_batch_size=cfg.training.test_batch_size,
            num_workers=cfg.training.num_workers,
            train_probability=cfg.training.train_prob,
            val_probability=cfg.training.val_prob,
            train_frames=cfg.training.train_frames,
            unlabeled_sequence_length=cfg.training.unlabeled_sequence_length,
            torch_seed=cfg.training.rng_seed_data_pt,
            dali_seed=cfg.training.rng_seed_data_dali,
            device_id=cfg.training.gpu_id,
        )
    return data_module


@typechecked
def get_loss_factories(
    cfg: DictConfig,
    data_module: Union[BaseDataModule, UnlabeledDataModule]
) -> dict:
    """Note: much of this replaces the function `format_and_update_loss_info`."""

    # loss_param_dict = OmegaConf.to_object(cfg.losses)
    # losses_to_use = OmegaConf.to_object(cfg.model.losses_to_use)

    loss_params_dict = {"supervised": {}, "unsupervised": {}}

    # collect all supervised losses in a dict; no extra params needed
    # set "log_weight = 0.0" so that weight = 1 and effective weight is (1 / 2)
    if cfg.model.model_type == "heatmap":
        loss_params_dict["supervised"]["heatmap_" + cfg.model.heatmap_loss_type] = {
            "log_weight": 0.0
        }
    else:
        loss_params_dict["supervised"][cfg.model.model_type] = {
            "log_weight": 0.0
        }

    # collect all unsupervised losses and their params in a dict
    for loss_name in cfg.model.losses_to_use:
        # general parameters
        loss_params_dict["unsupervised"][loss_name] = cfg.losses[loss_name]
        loss_params_dict["unsupervised"][loss_name]["loss_name"] = loss_name
        # loss-specific parameters
        if loss_name == "unimodal_mse" or loss_name == "unimodal_wasserstein":
            if cfg.model.model_type == "regression":
                raise NotImplementedError(
                    f"unimodal loss can only be used with classes inheriting from "
                    f"HeatmapTracker. \nYou specified a RegressionTracker model."
                )
            # record original image dims (after initial resizing)
            height_og = cfg.data.image_resize_dims.height
            width_og = cfg.data.image_resize_dims.width
            loss_params_dict["unsupervised"][loss_name]["original_image_height"] = \
                height_og
            loss_params_dict["unsupervised"][loss_name]["original_image_width"] = \
                width_og
            # record downsampled image dims
            height_ds = int(height_og // (2 ** cfg.data.downsample_factor))
            width_ds = int(width_og // (2 ** cfg.data.downsample_factor))
            loss_params_dict["unsupervised"][loss_name]["downsampled_image_height"] = \
                height_ds
            loss_params_dict["unsupervised"][loss_name]["downsampled_image_width"] = \
                width_ds
        elif loss_name == "pca_multiview":
            loss_params_dict["unsupervised"][loss_name]["mirrored_column_matches"] = \
                cfg.data.mirrored_column_matches

    # build supervised loss factory, which orchestrates all supervised losses
    loss_factory_sup = LossFactory(
        losses_params_dict=loss_params_dict["supervised"],
        data_module=data_module,
    )
    # build unsupervised loss factory, which orchestrates all unsupervised losses
    loss_factory_unsup = LossFactory(
        losses_params_dict=loss_params_dict["unsupervised"],
        data_module=data_module,
        learn_weights=cfg.model.learn_weights,
    )

    return {"supervised": loss_factory_sup, "unsupervised": loss_factory_unsup}


@typechecked
def get_model(
    cfg: DictConfig,
    data_module: Union[BaseDataModule, UnlabeledDataModule],
    loss_factories: Dict[str, LossFactory],
) -> Union[
    RegressionTracker,
    HeatmapTracker,
    SemiSupervisedRegressionTracker,
    SemiSupervisedHeatmapTracker,
]:
    semi_supervised = check_if_semi_supervised(cfg.model.losses_to_use)
    if not semi_supervised:
        if cfg.model.model_type == "regression":
            model = RegressionTracker(
                num_keypoints=cfg.data.num_keypoints,
                loss_factory=loss_factories["supervised"],
                resnet_version=cfg.model.resnet_version,
                torch_seed=cfg.training.rng_seed_model_pt,
            )
        elif cfg.model.model_type == "heatmap":
            model = HeatmapTracker(
                num_keypoints=cfg.data.num_keypoints,
                loss_factory=loss_factories["supervised"],
                resnet_version=cfg.model.resnet_version,
                downsample_factor=cfg.data.downsample_factor,
                output_shape=data_module.dataset.output_shape,
                torch_seed=cfg.training.rng_seed_model_pt,
            )
        else:
            raise NotImplementedError(
                "%s is an invalid cfg.model.model_type for a fully supervised model"
                % cfg.model.model_type
            )
        # add losses onto initialized model
        # model.add_loss_factory(loss_factories["supervised"])

    else:
        if cfg.model.model_type == "regression":
            model = SemiSupervisedRegressionTracker(
                num_keypoints=cfg.data.num_keypoints,
                loss_factory=loss_factories["supervised"],
                loss_factory_unsupervised=loss_factories["unsupervised"],
                resnet_version=cfg.model.resnet_version,
                torch_seed=cfg.training.rng_seed_model_pt,
            )

        elif cfg.model.model_type == "heatmap":
            model = SemiSupervisedHeatmapTracker(
                num_keypoints=cfg.data.num_keypoints,
                loss_factory=loss_factories["supervised"],
                loss_factory_unsupervised=loss_factories["unsupervised"],
                resnet_version=cfg.model.resnet_version,
                downsample_factor=cfg.data.downsample_factor,
                output_shape=data_module.dataset.output_shape,
                torch_seed=cfg.training.rng_seed_model_pt,
            )
        else:
            raise NotImplementedError(
                "%s is an invalid cfg.model.model_type for a semi-supervised model"
                % cfg.model.model_type
            )
    return model
