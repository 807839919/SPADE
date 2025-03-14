_base_ = ["../_base_/datasets/psg.py", "../_base_/custom_runtime.py"]

find_unused_parameters = True

# HACK:
object_classes = [
    "person",
    "bicycle",
    "car",
    "motorcycle",
    "airplane",
    "bus",
    "train",
    "truck",
    "boat",
    "traffic light",
    "fire hydrant",
    "stop sign",
    "parking meter",
    "bench",
    "bird",
    "cat",
    "dog",
    "horse",
    "sheep",
    "cow",
    "elephant",
    "bear",
    "zebra",
    "giraffe",
    "backpack",
    "umbrella",
    "handbag",
    "tie",
    "suitcase",
    "frisbee",
    "skis",
    "snowboard",
    "sports ball",
    "kite",
    "baseball bat",
    "baseball glove",
    "skateboard",
    "surfboard",
    "tennis racket",
    "bottle",
    "wine glass",
    "cup",
    "fork",
    "knife",
    "spoon",
    "bowl",
    "banana",
    "apple",
    "sandwich",
    "orange",
    "broccoli",
    "carrot",
    "hot dog",
    "pizza",
    "donut",
    "cake",
    "chair",
    "couch",
    "potted plant",
    "bed",
    "dining table",
    "toilet",
    "tv",
    "laptop",
    "mouse",
    "remote",
    "keyboard",
    "cell phone",
    "microwave",
    "oven",
    "toaster",
    "sink",
    "refrigerator",
    "book",
    "clock",
    "vase",
    "scissors",
    "teddy bear",
    "hair drier",
    "toothbrush",
    "banner",
    "blanket",
    "bridge",
    "cardboard",
    "counter",
    "curtain",
    "door-stuff",
    "floor-wood",
    "flower",
    "fruit",
    "gravel",
    "house",
    "light",
    "mirror-stuff",
    "net",
    "pillow",
    "platform",
    "playingfield",
    "railroad",
    "river",
    "road",
    "roof",
    "sand",
    "sea",
    "shelf",
    "snow",
    "stairs",
    "tent",
    "towel",
    "wall-brick",
    "wall-stone",
    "wall-tile",
    "wall-wood",
    "water-other",
    "window-blind",
    "window-other",
    "tree-merged",
    "fence-merged",
    "ceiling-merged",
    "sky-other-merged",
    "cabinet-merged",
    "table-merged",
    "floor-other-merged",
    "pavement-merged",
    "mountain-merged",
    "grass-merged",
    "dirt-merged",
    "paper-merged",
    "food-other-merged",
    "building-other-merged",
    "rock-merged",
    "wall-other-merged",
    "rug-merged",
]

predicate_classes = [
    "over",
    "in front of",
    "beside",
    "on",
    "in",
    "attached to",
    "hanging from",
    "on back of",
    "falling off",
    "going down",
    "painted on",
    "walking on",
    "running on",
    "crossing",
    "standing on",
    "lying on",
    "sitting on",
    "flying over",
    "jumping over",
    "jumping from",
    "wearing",
    "holding",
    "carrying",
    "looking at",
    "guiding",
    "kissing",
    "eating",
    "drinking",
    "feeding",
    "biting",
    "catching",
    "picking",
    "playing with",
    "chasing",
    "climbing",
    "cleaning",
    "playing",
    "touching",
    "pushing",
    "pulling",
    "opening",
    "cooking",
    "talking to",
    "throwing",
    "slicing",
    "driving",
    "riding",
    "parked on",
    "driving on",
    "about to hit",
    "kicking",
    "swinging",
    "entering",
    "exiting",
    "enclosing",
    "leaning on",
]


model = dict(
    type="PSGTr",
    backbone=dict(
        type="SwinTransformer",
        embed_dims=128,
        depths=[2, 2, 18, 2],
        num_heads=[4, 8, 16, 32],
        window_size=12,
        mlp_ratio=4,
        qkv_bias=True,
        qk_scale=None,
        drop_rate=0.0,
        attn_drop_rate=0.0,
        drop_path_rate=0.3,
        patch_norm=True,
        out_indices=(0, 1, 2, 3),
        with_cp=False,
        convert_weights=True,
        frozen_stages=-1,
        init_cfg=dict(
            type="Pretrained",
            checkpoint="https://github.com/SwinTransformer/storage/releases/download/v1.0.0/swin_base_patch4_window12_384.pth",
        ),
        pretrain_img_size=384,
    ),
    bbox_head=dict(
        type="CrossHead2",
        num_classes=len(object_classes),
        num_relations=len(predicate_classes),
        object_classes=object_classes,
        predicate_classes=predicate_classes,
        num_obj_query=100,
        num_rel_query=100,
        in_channels=[128, 256, 512, 1024],  # pass to pixel_decoder inside
        strides=[4, 8, 16, 32],
        feat_channels=256,
        out_channels=256,
        num_transformer_feat_level=3,
        embed_dims=256,
        enforce_decoder_input_project=False,
        pixel_decoder=dict(
            type="MSDeformAttnPixelDecoder",
            num_outs=3,
            norm_cfg=dict(type="GN", num_groups=32),
            act_cfg=dict(type="ReLU"),
            encoder=dict(
                type="DetrTransformerEncoder",
                num_layers=6,
                transformerlayers=dict(
                    type="BaseTransformerLayer",
                    attn_cfgs=dict(
                        type="MultiScaleDeformableAttention",
                        embed_dims=256,
                        num_heads=8,
                        num_levels=3,
                        num_points=4,
                        im2col_step=64,
                        dropout=0.0,
                        batch_first=False,
                        norm_cfg=None,
                        init_cfg=None,
                    ),
                    ffn_cfgs=dict(
                        type="FFN",
                        embed_dims=256,
                        feedforward_channels=1024,
                        num_fcs=2,
                        ffn_drop=0.0,
                        act_cfg=dict(type="ReLU", inplace=True),
                    ),
                    operation_order=("self_attn", "norm", "ffn", "norm"),
                ),
                init_cfg=None,
            ),
            positional_encoding=dict(
                type="SinePositionalEncoding", num_feats=128, normalize=True
            ),
            init_cfg=None,
        ),
        transformer_decoder=dict(
            type="DetrTransformerDecoder",
            return_intermediate=False,
            num_layers=9,
            transformerlayers=dict(
                type="BaseTransformerLayer",
                attn_cfgs=dict(
                    type="MultiheadAttention",
                    embed_dims=256,
                    num_heads=8,
                    attn_drop=0.0,
                    proj_drop=0.0,
                    dropout_layer=None,
                    batch_first=False,
                ),
                ffn_cfgs=dict(
                    embed_dims=256,
                    feedforward_channels=2048,
                    num_fcs=2,
                    act_cfg=dict(type="ReLU", inplace=True),
                    ffn_drop=0.0,
                    dropout_layer=None,
                    add_identity=True,
                ),
                operation_order=(
                    "cross_attn",
                    "norm",
                    "self_attn",
                    "norm",
                    "ffn",
                    "norm",
                ),
            ),
        ),
        relation_decoder=dict(
            type="DetrTransformerDecoder",
            return_intermediate=True,
            num_layers=6,
            transformerlayers=dict(
                type="BaseTransformerLayer",
                attn_cfgs=dict(
                    type="MultiheadAttention",
                    embed_dims=256,
                    num_heads=8,
                    attn_drop=0.0,
                    proj_drop=0.0,
                    dropout_layer=None,
                    batch_first=False,
                ),
                ffn_cfgs=dict(
                    embed_dims=256,
                    feedforward_channels=2048,
                    num_fcs=2,
                    act_cfg=dict(type="ReLU", inplace=True),
                    ffn_drop=0.1,
                    dropout_layer=None,
                    add_identity=True,
                ),
                operation_order=(
                    "cross_attn",
                    "norm",
                    "self_attn",
                    "norm",
                    "ffn",
                    "norm",
                ),
            ),
        ),
        positional_encoding=dict(
            type="SinePositionalEncoding", num_feats=128, normalize=True
        ),
        # rel_cls_loss=dict(
        #     type="FocalLoss",
        #     loss_weight=2.0,
        #     reduction="mean",
        #     class_weight="/home/jhwang/pretrain/r_label_weight.pt",
        #     gamma=0.0,
        # ),
        rel_cls_loss=dict(
            type="SeesawLoss",
            num_classes=56,
            return_dict=True,
            loss_weight=2.0,
        ),
        subobj_cls_loss=dict(
            type="CrossEntropyLoss",
            use_sigmoid=False,
            loss_weight=4.0,
            reduction="mean",
            class_weight=[1.0] * 134,
        ),
        importance_match_loss=dict(
            type="BCEWithLogitsLoss",
            reduction="mean",
            loss_weight=5.0,
        ),
        loss_cls=dict(
            type="CrossEntropyLoss",
            use_sigmoid=False,
            loss_weight=2.0,
            reduction="mean",
            class_weight=[1.0] * 133 + [0.1],
        ),
        loss_mask=dict(
            type="CrossEntropyLoss", use_sigmoid=True, reduction="mean", loss_weight=5.0
        ),
        loss_dice=dict(
            type="DiceLoss",
            use_sigmoid=True,
            activate=True,
            reduction="mean",
            naive_dice=True,
            eps=1.0,
            loss_weight=5.0,
        ),
    ),
    # training and testing settings
    train_cfg=dict(
        id_assigner=dict(
            type="IdMatcher",
            sub_id_cost=dict(type="ClassificationCost", weight=1.0),
            obj_id_cost=dict(type="ClassificationCost", weight=1.0),
            r_cls_cost=dict(type="ClassificationCost", weight=0.0),
        ),
        num_points=12544,
        oversample_ratio=3.0,
        importance_sample_ratio=0.75,
        mask_assigner=dict(
            type="MaskHungarianAssigner",
            cls_cost=dict(type="ClassificationCost", weight=2.0),
            mask_cost=dict(type="CrossEntropyLossCost", weight=5.0, use_sigmoid=True),
            dice_cost=dict(type="DiceCost", weight=5.0, pred_act=True, eps=1.0),
        ),
        sampler=dict(type="MaskPseudoSampler"),
    ),
    test_cfg=dict(max_per_img=100),
)


custom_imports = dict(
    imports=[
        "spade.models.frameworks.psgtr",
        "spade.models.losses.seg_losses",
        "spade.datasets",
        "spade.datasets.pipelines.loading",
        "spade.datasets.pipelines.rel_randomcrop",
        "spade.models.relation_heads.approaches.matcher",
        "spade.utils",
    ],
    allow_failed_imports=False,
)

dataset_type = "PanopticSceneGraphDataset"

img_norm_cfg = dict(
    mean=[123.675, 116.28, 103.53], std=[58.395, 57.12, 57.375], to_rgb=True
)
# train_pipeline, NOTE the img_scale and the Pad's size_divisor is different
# from the default setting in mmdet.
train_pipeline = [
    dict(type="LoadImageFromFile"),
    dict(
        type="LoadPanopticSceneGraphAnnotations",
        with_bbox=True,
        with_rel=True,
        with_mask=True,
        with_seg=True,
    ),
    dict(type="RandomFlip", flip_ratio=0.5),
    dict(
        type="AutoAugment",
        policies=[
            [
                dict(
                    type="Resize",
                    img_scale=[
                        (480, 1333),
                        (512, 1333),
                        (544, 1333),
                        (576, 1333),
                        (608, 1333),
                        (640, 1333),
                        (672, 1333),
                        (704, 1333),
                        (736, 1333),
                        (768, 1333),
                        (800, 1333),
                    ],
                    multiscale_mode="value",
                    keep_ratio=True,
                )
            ],
            [
                dict(
                    type="Resize",
                    img_scale=[(400, 1333), (500, 1333), (600, 1333)],
                    multiscale_mode="value",
                    keep_ratio=True,
                ),
                dict(
                    type="RelRandomCrop",
                    crop_type="absolute_range",
                    crop_size=(384, 600),
                    allow_negative_crop=False,
                ),  # no empty relations
                dict(
                    type="Resize",
                    img_scale=[
                        (480, 1333),
                        (512, 1333),
                        (544, 1333),
                        (576, 1333),
                        (608, 1333),
                        (640, 1333),
                        (672, 1333),
                        (704, 1333),
                        (736, 1333),
                        (768, 1333),
                        (800, 1333),
                    ],
                    multiscale_mode="value",
                    override=True,
                    keep_ratio=True,
                ),
            ],
        ],
    ),
    dict(type="Normalize", **img_norm_cfg),
    dict(type="Pad", size_divisor=1),
    dict(type="RelsFormatBundle"),
    dict(type="Collect", keys=["img", "gt_bboxes", "gt_labels", "gt_rels", "gt_masks"]),
]
# test_pipeline, NOTE the Pad's size_divisor is different from the default
# setting (size_divisor=32). While there is little effect on the performance
# whether we use the default setting or use size_divisor=1.
test_pipeline = [
    dict(type="LoadImageFromFile"),
    dict(type="LoadSceneGraphAnnotations", with_bbox=True, with_rel=True),
    dict(
        type="MultiScaleFlipAug",
        img_scale=(1333, 800),
        flip=False,
        transforms=[
            dict(type="Resize", keep_ratio=True),
            dict(type="RandomFlip"),
            dict(type="Normalize", **img_norm_cfg),
            dict(type="Pad", size_divisor=1),
            dict(type="ImageToTensor", keys=["img"]),
            dict(type="ToTensor", keys=["gt_bboxes", "gt_labels"]),
            dict(
                type="ToDataContainer",
                fields=(dict(key="gt_bboxes"), dict(key="gt_labels")),
            ),
            dict(type="Collect", keys=["img"]),
        ],
    ),
]

evaluation = dict(
    interval=1,
    metric="sgdet",
    relation_mode=True,
    classwise=True,
    iou_thrs=0.5,
    detection_method="pan_seg",
)

data = dict(
    samples_per_gpu=2,
    # workers_per_gpu=1,
    pin_memory=True,
    train=dict(pipeline=train_pipeline),
    val=dict(pipeline=test_pipeline),
    test=dict(pipeline=test_pipeline),
)

# optimizer
optimizer = dict(
    type="AdamW",
    lr=1e-4,
    weight_decay=1e-4,
    paramwise_cfg=dict(
        custom_keys={
            "backbone": dict(lr_mult=0.01, decay_mult=0.0),
            "transformer_decoder": dict(lr_mult=0.1, decay_mult=1),
            "pixel_decoder": dict(lr_mult=0.1, decay_mult=1),
            "decoder_input_projs": dict(lr_mult=0.1, decay_mult=1),
        },
        norm_decay_mult=0.0,
    ),
)

optimizer_config = dict(grad_clip=dict(max_norm=0.1, norm_type=2))

# learning policy
lr_config = dict(policy="step", gamma=0.5, step=[5, 10])
runner = dict(type="EpochBasedRunner", max_epochs=12)

project_name = "ATM"
expt_name = "psg_swinbase"
work_dir = f"./work_dirs/{expt_name}"
checkpoint_config = dict(interval=1, max_keep_ckpts=15)

log_config = dict(
    interval=50,
    hooks=[
        dict(type="TextLoggerHook"),
        # dict(type="TensorboardLoggerHook"),
        # dict(
        #     type="WandbLoggerHook",
        #     init_kwargs=dict(
        #         project=project_name,
        #         name=expt_name,
        #     ),
        # ),
    ],
)
auto_scale_lr = dict(enable=True, base_batch_size=8)
load_from = "/home/jhwang/pretrain/swin-b-clean.pth"
