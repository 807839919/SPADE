# Copyright (c) OpenMMLab. All rights reserved.
import torch


def preprocess_panoptic_gt(gt_labels, gt_masks, gt_semantic_seg, num_things, num_stuff):
    num_classes = num_things + num_stuff
    things_labels = gt_labels
    gt_semantic_seg = gt_semantic_seg.squeeze(0)

    things_masks = gt_masks.pad(gt_semantic_seg.shape[-2:], pad_val=0).to_tensor(
        dtype=torch.bool, device=gt_labels.device
    )

    semantic_labels = torch.unique(
        gt_semantic_seg, sorted=False, return_inverse=False, return_counts=False
    )
    stuff_masks_list = []
    stuff_labels_list = []
    for label in semantic_labels:
        if label < num_things or label >= num_classes:
            continue
        stuff_mask = gt_semantic_seg == label
        stuff_masks_list.append(stuff_mask)
        stuff_labels_list.append(label)

    if len(stuff_masks_list) > 0:
        stuff_masks = torch.stack(stuff_masks_list, dim=0)
        stuff_labels = torch.stack(stuff_labels_list, dim=0)
        labels = torch.cat([things_labels, stuff_labels], dim=0)
        masks = torch.cat([things_masks, stuff_masks], dim=0)
    else:
        labels = things_labels
        masks = things_masks

    masks = masks.long()
    return labels, masks
