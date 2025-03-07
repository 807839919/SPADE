_base_ = [
    "../motifs/panoptic_fpn_r50_fpn_1x_predcls_psg.py",
]

custom_imports = dict(
    imports=[
        "spade.models.frameworks.psgtr",
        "spade.models.losses.seg_losses",
        "spade.models.frameworks.dual_transformer",
        "spade.models.relation_heads.psgformer_head",
        "spade.datasets",
        "spade.datasets.pipelines.loading",
        "spade.datasets.pipelines.rel_randomcrop",
        "spade.models.relation_heads.approaches.matcher",
        "spade.utils",
    ],
    allow_failed_imports=False,
)

model = dict(
    relation_head=dict(
        type="IMPHead",
        head_config=dict(
            # NOTE: Evaluation type
            use_gt_box=False,
            use_gt_label=False,
            num_iter=2,
        ),
    )
)

evaluation = dict(
    interval=1,
    metric="sgdet",
    relation_mode=True,
    classwise=True,
    iou_thrs=0.5,
    detection_method="pan_seg",
)

# Change batch size and learning rate
data = dict(
    samples_per_gpu=16,
)
# workers_per_gpu=0)  # FIXME: Is this the problem?
optimizer = dict(type="SGD", lr=0.001, momentum=0.9)

# Log config
project_name = "spade"
expt_name = "imp_panoptic_fpn_r50_fpn_1x_sgdet_psg"
work_dir = f"./work_dirs/{expt_name}"

log_config = dict(
    interval=50,
    hooks=[
        dict(type="TextLoggerHook"),
        # dict(type='TensorboardLoggerHook')
        dict(
            type="WandbLoggerHook",
            init_kwargs=dict(
                project=project_name,
                name=expt_name,
                # config=work_dir + "/cfg.yaml"
            ),
        ),
    ],
)
