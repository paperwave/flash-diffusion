import datetime
import os
from copy import deepcopy

import braceexpand
import torch.nn as nn
import yaml
from diffusers import (
    DiffusionPipeline,
    DPMSolverMultistepScheduler,
    EulerDiscreteScheduler,
    LCMScheduler,
    StableDiffusionXLPipeline,
)
from peft import LoraConfig
from pytorch_lightning import Trainer, loggers
from pytorch_lightning.callbacks import ModelCheckpoint

from flash.data.datasets import DataModule, DataModuleConfig
from flash.data.filters import (
    FilterOnCondition,
    FilterOnConditionConfig,
    KeyFilter,
    KeyFilterConfig,
)
from flash.data.mappers import (
    KeyRenameMapper,
    KeyRenameMapperConfig,
    KeysFromJSONMapper,
    KeysFromJSONMapperConfig,
    MapperWrapper,
    RemoveKeysMapper,
    RemoveKeysMapperConfig,
    RescaleMapper,
    RescaleMapperConfig,
    SelectKeysMapper,
    SelectKeysMapperConfig,
    TorchvisionMapper,
    TorchvisionMapperConfig,
)
from flash.models.embedders import ClipEmbedder, ClipEmbedderConfig, ConditionerWrapper
from flash.models.flash import FlashDiffusion, FlashDiffusionConfig
from flash.models.unets import DiffusersUNet2DCondWrapper
from flash.models.vae import AutoencoderKLDiffusers, AutoencoderKLDiffusersConfig
from flash.trainer import TrainingConfig, TrainingPipeline
from flash.trainer.loggers import WandbSampleLogger


def main(args):
    # Load pretrained model as base
    pipe = DiffusionPipeline.from_pretrained(
        "runwayml/stable-diffusion-v1-5",
        # torch_dtype=torch.float16,
        use_safetensors=True,
    )
    teacher_unet = DiffusersUNet2DCondWrapper(
        in_channels=4,
        out_channels=4,
        center_input_sample=False,
        flip_sin_to_cos=True,
        freq_shift=0,
        down_block_types=[
            "CrossAttnDownBlock2D",
            "CrossAttnDownBlock2D",
            "CrossAttnDownBlock2D",
            "DownBlock2D",
        ],
        mid_block_type="UNetMidBlock2DCrossAttn",
        up_block_types=[
            "UpBlock2D",
            "CrossAttnUpBlock2D",
            "CrossAttnUpBlock2D",
            "CrossAttnUpBlock2D",
        ],
        only_cross_attention=False,
        block_out_channels=[320, 640, 1280, 1280],
        layers_per_block=2,
        downsample_padding=1,
        mid_block_scale_factor=1,
        dropout=0.0,
        act_fn="silu",
        norm_num_groups=32,
        norm_eps=1e-05,
        cross_attention_dim=768,
        transformer_layers_per_block=1,
        reverse_transformer_layers_per_block=None,
        encoder_hid_dim=None,
        encoder_hid_dim_type=None,
        attention_head_dim=8,
        num_attention_heads=None,
        dual_cross_attention=False,
        use_linear_projection=True,
        class_embed_type=None,
        addition_embed_type=None,  # 'text_time',
        addition_time_embed_dim=None,  # 256,
        num_class_embeds=None,
        upcast_attention=None,
        resnet_time_scale_shift="default",
        resnet_skip_time_act=False,
        resnet_out_scale_factor=1.0,
        time_embedding_type="positional",
        time_embedding_dim=None,
        time_embedding_act_fn=None,
        timestep_post_act=None,
        time_cond_proj_dim=None,
        conv_in_kernel=3,
        conv_out_kernel=3,
        projection_class_embeddings_input_dim=None,
        attention_type="default",
        class_embeddings_concat=False,
        mid_block_only_cross_attention=None,
        cross_attention_norm=None,
        addition_embed_type_num_heads=64,
    )

    # Adapt weights
    new_state_dict = teacher_unet.state_dict()
    for key in pipe.unet.state_dict():
        if key in [
            "down_blocks.0.attentions.0.proj_in.weight",
            "down_blocks.0.attentions.0.proj_out.weight",
            "down_blocks.0.attentions.1.proj_in.weight",
            "down_blocks.0.attentions.1.proj_out.weight",
            "down_blocks.1.attentions.0.proj_in.weight",
            "down_blocks.1.attentions.0.proj_out.weight",
            "down_blocks.1.attentions.1.proj_in.weight",
            "down_blocks.1.attentions.1.proj_out.weight",
            "down_blocks.2.attentions.0.proj_in.weight",
            "down_blocks.2.attentions.0.proj_out.weight",
            "down_blocks.2.attentions.1.proj_in.weight",
            "down_blocks.2.attentions.1.proj_out.weight",
            "up_blocks.1.attentions.0.proj_in.weight",
            "up_blocks.1.attentions.0.proj_out.weight",
            "up_blocks.1.attentions.1.proj_in.weight",
            "up_blocks.1.attentions.1.proj_out.weight",
            "up_blocks.1.attentions.2.proj_out.weight",
            "up_blocks.1.attentions.2.proj_in.weight",
            "up_blocks.2.attentions.0.proj_in.weight",
            "up_blocks.2.attentions.0.proj_out.weight",
            "up_blocks.2.attentions.1.proj_in.weight",
            "up_blocks.2.attentions.1.proj_out.weight",
            "up_blocks.2.attentions.2.proj_out.weight",
            "up_blocks.2.attentions.2.proj_in.weight",
            "up_blocks.3.attentions.0.proj_in.weight",
            "up_blocks.3.attentions.0.proj_out.weight",
            "up_blocks.3.attentions.1.proj_in.weight",
            "up_blocks.3.attentions.1.proj_out.weight",
            "up_blocks.3.attentions.2.proj_out.weight",
            "up_blocks.3.attentions.2.proj_in.weight",
            "mid_block.attentions.0.proj_in.weight",
            "mid_block.attentions.0.proj_out.weight",
        ]:
            new_state_dict[key] = pipe.unet.state_dict()[key].squeeze(-1).squeeze(-1)

        else:
            new_state_dict[key] = pipe.unet.state_dict()[key]

    teacher_unet.load_state_dict(new_state_dict, strict=True)

    ## CONDITIONERS ##
    # Get text encoders
    text_embedder_config = ClipEmbedderConfig(
        version="runwayml/stable-diffusion-v1-5",
        text_embedder_subfolder="text_encoder",
        tokenizer_subfolder="tokenizer",
        layer="last",
    )
    text_embedder = ClipEmbedder(text_embedder_config)

    # Freeze text encoders
    text_embedder.freeze()

    # Wrap conditioners
    conditioner = ConditionerWrapper(
        conditioners=[
            text_embedder,
        ],
    )
    ## VAE ##
    # Get VAE model
    vae_config = AutoencoderKLDiffusersConfig(
        version="runwayml/stable-diffusion-v1-5",
        subfolder="vae",
    )
    vae = AutoencoderKLDiffusers(vae_config)
    vae.freeze()

    ### Student UNet ###
    student_unet = deepcopy(teacher_unet)

    if args["LORA"]:
        ## LoRA config ##
        student_unet_lora_config = LoraConfig(
            r=args["LORA_RANK"],
            lora_alpha=args["LORA_RANK"],
            init_lora_weights="gaussian",
            target_modules=["to_k", "to_q", "to_v", "to_out.0"],
        )

        student_unet.add_adapter(student_unet_lora_config)

    teacher_unet.freeze()

    teacher_scheduler = eval(args["TEACHER_SCHEDULER"]).from_pretrained(
        "stabilityai/stable-diffusion-xl-base-1.0",
        subfolder="scheduler",
        timestep_spacing="trailing",
    )
    sampling_scheduler = eval(args["SAMPLING_SCHEDULER"]).from_pretrained(
        "stabilityai/stable-diffusion-xl-base-1.0",
        subfolder="scheduler",
        timestep_spacing="trailing",
    )
    teacher_sampling_scheduler = eval(
        args["TEACHER_SAMPLING_SCHEDULER"]
    ).from_pretrained(
        "stabilityai/stable-diffusion-xl-base-1.0",
        subfolder="scheduler",
    )

    # Discriminator
    discriminator_feature_dim = 64
    color_dim = 1280

    discriminator = nn.Sequential(
        nn.Conv2d(color_dim, discriminator_feature_dim, 3, 1, 1),
        nn.SiLU(True),
        nn.Conv2d(
            discriminator_feature_dim,
            discriminator_feature_dim * 2,
            4,
            2,
            1,
            bias=False,
        ),
        nn.SiLU(True),
        nn.GroupNorm(4, discriminator_feature_dim * 2),
        nn.Conv2d(discriminator_feature_dim * 2, 1, 4, 1, 0, bias=False),
        nn.Flatten(),
    )

    ## Diffusion Model ##
    # Get diffusion model
    config = FlashDiffusionConfig(
        ucg_keys=args["UCG_KEYS"],
        K=args["K"],
        num_iterations_per_K=args["NUM_ITERATIONS_PER_K"],
        guidance_scale_min=args["GUIDANCE_MIN"],
        guidance_scale_max=args["GUIDANCE_MAX"],
        timestep_distribution=args["TIMESTEP_DISTRIBUTION"],
        mixture_num_components=args["MIXTURE_NUM_COMPONENTS"],
        mixture_var=args["MIXTURE_VAR"],
        use_dmd_loss=args["USE_DMD_LOSS"],
        dmd_loss_scale=args["DMD_LOSS_SCALE"],
        distill_loss_type=args["DISTILL_LOSS_TYPE"],
        distill_loss_scale=args["DISTILL_LOSS_SCALE"],
        adversarial_loss_scale=args["ADVERSARIAL_LOSS_SCALE"],
        gan_loss_type=args["GAN_LOSS_TYPE"],
        mode_probs=args["MODE_PROBS"],
        use_teacher_as_real=args["USE_TEACHER_AS_REAL"],
        use_empty_prompt=args["USE_EMPTY_PROMPT"],
    )
    model = FlashDiffusion(
        config,
        student_denoiser=student_unet,
        teacher_denoiser=teacher_unet,
        teacher_noise_scheduler=teacher_scheduler,
        sampling_noise_scheduler=sampling_scheduler,
        teacher_sampling_noise_scheduler=teacher_sampling_scheduler,
        vae=vae,
        conditioner=conditioner,
        discriminator=discriminator,
    )

    del pipe

    ##################### DATA #####################

    # Define filters and mappers
    filters_mappers = [
        KeyFilter(KeyFilterConfig(keys=["jpg", "json"])),
        SelectKeysMapper(SelectKeysMapperConfig(keys=["jpg", "json"])),
        MapperWrapper(
            [
                KeysFromJSONMapper(
                    KeysFromJSONMapperConfig(
                        key="json",
                        keys_to_extract=[
                            "caption",
                            "aesthetic_score",
                        ],
                        remove_original=False,
                        strict=False,
                    )
                ),
                KeyRenameMapper(
                    KeyRenameMapperConfig(
                        key_map={
                            "jpg": "image",
                            "caption": "text",
                        }
                    )
                ),
                TorchvisionMapper(
                    TorchvisionMapperConfig(
                        key="image",
                        transforms=["CenterCrop", "ToTensor", "Resize"],
                        transforms_kwargs=[
                            {"size": (1024, 1024)},
                            {},
                            {"size": (512, 512)},
                        ],
                    )
                ),
                RemoveKeysMapper(RemoveKeysMapperConfig(keys=["json"])),
                RescaleMapper(RescaleMapperConfig(key="image")),
            ]
        ),
        FilterOnCondition(
            FilterOnConditionConfig(
                condition_key="aesthetic_score",
                condition_fn=lambda x: x >= 6.0,
            )
        ),
    ]

    shards_path_or_urls = args["SHARDS_PATH_OR_URLS"]

    # unbrace urls
    shards_path_or_urls_unbraced = []
    for shards_path_or_url in shards_path_or_urls:
        shards_path_or_urls_unbraced.extend(braceexpand.braceexpand(shards_path_or_url))

    data_module = DataModule(
        train_config=DataModuleConfig(
            shards_path_or_urls=shards_path_or_urls_unbraced,
            decoder="pil",
            per_worker_batch_size=args["BATCH_SIZE"],
            shuffle_after_filter_mappers_buffer_size=20,
            shuffle_before_filter_mappers_buffer_size=20,
            shuffle_before_split_by_node_buffer_size=20,
            shuffle_before_split_by_workers_buffer_size=20,
            num_workers=4,
        ),
        train_filters_mappers=filters_mappers,
        # train_batched_filters_mappers=batched_filters_mappers,
    )

    ##################### TRAIN #####################

    discriminator_trainable_params = [
        "discriminator.",
    ]

    # Training Config
    training_config = TrainingConfig(
        optimizers_name=["AdamW", "AdamW"],
        learning_rates=[args["LR"], args["LR_DISCRIMINATOR"]],
        log_keys=["image", "text"],
        trainable_params=[["student_denoiser"], discriminator_trainable_params],
        log_samples_model_kwargs={
            "max_samples": 8,
            "num_steps": args["NUM_STEPS"],
            "teacher_guidance_scale": args["TEACHER_SAMPLING_GUIDANCE_SCALE"],
            "log_teacher_samples": args["LOG_TEACHER_SAMPLES"],
            "conditioner_inputs": {
                "text": args["VALIDATION_PROMPTS"],
            },
        },
    )
    pipeline = TrainingPipeline(model=model, pipeline_config=training_config)

    training_signature = (
        datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + "-" + args["EXP_NAME"]
    )
    dir_path = f"logs/{training_signature}"
    ckpt_path = f"{dir_path}/checkpoints"
    os.makedirs(dir_path, exist_ok=True)
    os.makedirs(ckpt_path, exist_ok=True)
    run_name = training_signature

    trainer = Trainer(
        accelerator="gpu",
        devices=int(os.environ["SLURM_NPROCS"]) // int(os.environ["SLURM_NNODES"]),
        num_nodes=int(os.environ["SLURM_NNODES"]),
        strategy="ddp_find_unused_parameters_true",
        default_root_dir=dir_path,
        max_epochs=args["MAX_EPOCHS"],
        logger=loggers.WandbLogger(
            project="flash-diffusion",
            offline=False,
            save_dir=dir_path,
            name=run_name,
        ),
        callbacks=[
            WandbSampleLogger(log_batch_freq=args["LOG_EVERY_N_BATCHES"]),
            ModelCheckpoint(
                dirpath=ckpt_path,
                filename="{step}",
                every_n_train_steps=args["CKPT_EVERY_N_STEPS"],
                save_top_k=-1,  # to save all the models
            ),
        ],
        num_sanity_val_steps=0,
        precision="bf16-mixed",
        check_val_every_n_epoch=100000000,
    )

    trainer.fit(pipeline, data_module)


if __name__ == "__main__":
    config_path = "examples/configs/flash_sd.yaml"
    with open(config_path, "r") as f:
        args = yaml.safe_load(f)
        main(args)
