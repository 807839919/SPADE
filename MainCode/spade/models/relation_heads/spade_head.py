import time
from collections import defaultdict
from inspect import signature
from PIL import Image
import os

import clip
import json
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision
import numpy as np
from mmcv.cnn import Conv2d, Linear, build_activation_layer
from mmcv.cnn.bricks.transformer import FFN, build_positional_encoding
from mmcv.ops import batched_nms
from mmcv.runner import force_fp32
from mmdet.core import (bbox_cxcywh_to_xyxy, bbox_xyxy_to_cxcywh,
                        build_assigner, build_sampler, multi_apply,
                        reduce_mean)
from util.misc import (NestedTensor, nested_tensor_from_tensor_list,
                       accuracy, get_world_size, interpolate,
                       is_dist_avail_and_initialized, inverse_sigmoid)
from mmdet.datasets.coco_panoptic import INSTANCE_OFFSET
from mmdet.models.builder import HEADS, build_loss
from mmdet.models.dense_heads import AnchorFreeHead
from mmdet.models.utils import build_transformer
from torchvision import transforms
#####imports for tools
from packaging import version

import sys
from omegaconf import OmegaConf
from einops import rearrange, repeat

from StableDiffusion.ldm.util import instantiate_from_config
from SD_Extractor import UNetWrapper, TextAdapter
from data.relation import obj_text_label, relation_text_label
from .RelationGT import RGT

if version.parse(torchvision.__version__) < version.parse('0.7'):
    from torchvision.ops import _new_empty_tensor
    from torchvision.ops.misc import _output_size

with open('data/psg/relationship_descriptions.json', 'r') as f:
    relationship_descriptions = json.load(f)

class MaskPooling(nn.Module):
    def init(self, hard_pooling=False, mask_threshold=0.5, eps=1e-8):
        super().init()
        self.hard_pooling = hard_pooling
        self.mask_threshold = mask_threshold
        self.eps = eps
    def forward(self, features, masks):
        masks = torch.sigmoid(masks)
        if self.hard_pooling:
            masks = (masks > self.mask_threshold).float()
        norm = masks.sum(dim=(-1, -2), keepdim=True) + self.eps
        pooled = torch.einsum("bchw,bqhw->bqc", features, masks / norm)
        return pooled  # [B, Q, C]

@HEADS.register_module()
class spadeHead(AnchorFreeHead):
    _version = 2

    def __init__(
            self,
            num_classes,
            in_channels,
            num_relations,
            object_classes,
            predicate_classes,
            use_mask=True,
            num_query=100,
            num_reg_fcs=2,
            transformer=None,
            n_heads=8,
            swin_backbone=None,
            sync_cls_avg_factor=False,
            bg_cls_weight=0.02,
            positional_encoding=dict(type='SinePositionalEncoding',
                                     num_feats=128,
                                     normalize=True),
            sub_loss_cls=dict(type='CrossEntropyLoss',
                              use_sigmoid=False,
                              loss_weight=1.0,
                              class_weight=1.0),
            sub_loss_bbox=dict(type='L1Loss', loss_weight=5.0),
            sub_loss_iou=dict(type='GIoULoss', loss_weight=2.0),
            sub_focal_loss=dict(type='BCEFocalLoss', loss_weight=1.0),
            sub_dice_loss=dict(type='DiceLoss', loss_weight=1.0),
            obj_loss_cls=dict(type='CrossEntropyLoss',
                              use_sigmoid=False,
                              loss_weight=1.0,
                              class_weight=1.0),
            obj_loss_bbox=dict(type='L1Loss', loss_weight=5.0),
            obj_loss_iou=dict(type='GIoULoss', loss_weight=2.0),
            obj_focal_loss=dict(type='BCEFocalLoss', loss_weight=1.0),
            obj_dice_loss=dict(type='DiceLoss', loss_weight=1.0),
            rel_loss_cls=dict(type='CrossEntropyLoss',
                              use_sigmoid=False,
                              loss_weight=2.0,
                              class_weight=1.0),
            train_cfg=dict(assigner=dict(
                type='HTriMatcher',
                s_cls_cost=dict(type='ClassificationCost', weight=1.),
                s_reg_cost=dict(type='BBoxL1Cost', weight=5.0),
                s_iou_cost=dict(type='IoUCost', iou_mode='giou', weight=2.0),
                o_cls_cost=dict(type='ClassificationCost', weight=1.),
                o_reg_cost=dict(type='BBoxL1Cost', weight=5.0),
                o_iou_cost=dict(type='IoUCost', iou_mode='giou', weight=2.0),
                r_cls_cost=dict(type='ClassificationCost', weight=2.))),
            test_cfg=dict(max_per_img=100),
            unet_config=dict(),
            args=None,
            init_cfg=None,
            **kwargs):

        super(AnchorFreeHead, self).__init__(init_cfg)

        # config_path = os.environ.get("SD_Config")
        # ckpt_path = os.environ.get("SD_ckpt")

        config_path = "/home/user//Pair-Net-main/StableDiffusion/checkpoints/v1-inference.yaml"
        ckpt_path = "/home/user//Pair-Net-main/StableDiffusion/checkpoints/v1-5-pruned-emaonly.ckpt"

        config = OmegaConf.load(config_path)
        config.model.params.ckpt_path = ckpt_path
        config.model.params.cond_stage_config.target = 'ldm.modules.encoders.modules.AbstractEncoder'
        sd_model = instantiate_from_config(config.model)
        self.encoder_vq = sd_model.first_stage_model
        self.unet = UNetWrapper(sd_model.model, **unet_config)
        device = "cuda" if torch.cuda.is_available() else "cpu"
        self.unet.to(device)
        self.unet = self.unet.eval()
        sd_model.model = None
        sd_model.first_stage_model = None
        del sd_model.cond_stage_model
        del self.encoder_vq.decoder
        self.sd_model = sd_model
        self.sd_model = self.sd_model.eval()
        self.text_adapter = nn.Linear(512, 768)
        # self.clip_adapter = nn.Linear(512, 512)

        self.sync_cls_avg_factor = sync_cls_avg_factor
        # NOTE following the official DETR repo, bg_cls_weight means
        # relative classification weight of the no-object class.
        assert isinstance(bg_cls_weight, float), 'Expected bg_cls_weight to have type float.'
        self.bg_cls_weight = bg_cls_weight

        assert isinstance(use_mask, bool), 'Expected use_mask to have type bool.'
        self.use_mask = use_mask

        s_class_weight = sub_loss_cls.get('class_weight', None)
        assert isinstance(s_class_weight, float), 'Expected class_weight to have type float.'
        s_class_weight = torch.ones(num_classes + 1) * s_class_weight
        s_class_weight[-1] = bg_cls_weight
        sub_loss_cls.update({'class_weight': s_class_weight})

        o_class_weight = obj_loss_cls.get('class_weight', None)
        assert isinstance(o_class_weight, float), 'Expected class_weight to have type float.'
        o_class_weight = torch.ones(num_classes + 1) * o_class_weight
        o_class_weight[-1] = bg_cls_weight
        obj_loss_cls.update({'class_weight': o_class_weight})

        r_class_weight = rel_loss_cls.get('class_weight', None)
        assert isinstance(r_class_weight, float), 'Expected class_weight to have type float.'
        r_class_weight = torch.ones(num_relations + 1) * r_class_weight
        r_class_weight[0] = bg_cls_weight
        rel_loss_cls.update({'class_weight': r_class_weight})
        if 'bg_cls_weight' in rel_loss_cls:
            rel_loss_cls.pop('bg_cls_weight')

        if train_cfg:
            assert 'assigner' in train_cfg, 'assigner should be provided when train_cfg is set.'
            assigner = train_cfg['assigner']
            assert sub_loss_cls['loss_weight'] == assigner['s_cls_cost']['weight'], 'Mismatch in classification weight.'
            assert obj_loss_cls['loss_weight'] == assigner['o_cls_cost']['weight'], 'Mismatch in classification weight.'
            assert rel_loss_cls['loss_weight'] == assigner['r_cls_cost']['weight'], 'Mismatch in classification weight.'
            assert sub_loss_bbox['loss_weight'] == assigner['s_reg_cost'][
                'weight'], 'Mismatch in bbox regression weight.'
            assert obj_loss_bbox['loss_weight'] == assigner['o_reg_cost'][
                'weight'], 'Mismatch in bbox regression weight.'
            assert sub_loss_iou['loss_weight'] == assigner['s_iou_cost']['weight'], 'Mismatch in iou weight.'
            assert obj_loss_iou['loss_weight'] == assigner['o_iou_cost']['weight'], 'Mismatch in iou weight.'
            self.assigner = build_assigner(assigner)
            sampler_cfg = dict(type='PseudoSampler')
            self.sampler = build_sampler(sampler_cfg, context=self)
        self.num_query = num_query
        self.num_classes = num_classes
        self.num_relations = num_relations
        self.object_classes = object_classes
        self.predicate_classes = predicate_classes
        self.in_channels = in_channels
        self.num_reg_fcs = num_reg_fcs
        self.train_cfg = train_cfg
        self.test_cfg = test_cfg
        self.fp16_enabled = False
        self.swin = swin_backbone

        self.obj_loss_cls = build_loss(obj_loss_cls)
        self.obj_loss_bbox = build_loss(obj_loss_bbox)
        self.obj_loss_iou = build_loss(obj_loss_iou)

        self.sub_loss_cls = build_loss(sub_loss_cls)
        self.sub_loss_bbox = build_loss(sub_loss_bbox)
        self.sub_loss_iou = build_loss(sub_loss_iou)
        if self.use_mask:
            self.obj_dice_loss = build_loss(obj_dice_loss)
            self.sub_dice_loss = build_loss(sub_dice_loss)

        self.rel_loss_cls = build_loss(rel_loss_cls)

        if self.obj_loss_cls.use_sigmoid:
            self.obj_cls_out_channels = num_classes
        else:
            self.obj_cls_out_channels = num_classes + 1

        if self.sub_loss_cls.use_sigmoid:
            self.sub_cls_out_channels = num_classes
        else:
            self.sub_cls_out_channels = num_classes + 1

        if rel_loss_cls['use_sigmoid']:
            self.rel_cls_out_channels = num_relations
        else:
            self.rel_cls_out_channels = num_relations + 1

        self.act_cfg = transformer.get('act_cfg', dict(type='ReLU', inplace=True))
        self.activate = build_activation_layer(self.act_cfg)
        self.positional_encoding = build_positional_encoding(positional_encoding)
        self.transformer = build_transformer(transformer)
        self.n_heads = n_heads
        self.clip_model, _ = clip.load("ViT-B/32", device=device)
        self.clip_model = self.clip_model.eval()
        self.embed_dims = self.transformer.embed_dims
        self.image_adapter = nn.Conv2d(1280, self.embed_dims, kernel_size=1)

        self.logit_scale = nn.Parameter(torch.ones([]) * np.log(1 / 0.07))
        self.obj_logit_scale = nn.Parameter(torch.ones([]) * np.log(1 / 0.07))
        self.sub_logit_scale = nn.Parameter(torch.ones([]) * np.log(1 / 0.07))
        self.clip_tokenizer = clip.tokenize

        clip_label, obj_clip_label, v_linear_proj_weight, hoi_text, obj_text, train_clip_label = \
            self.init_classifier_with_CLIP(relation_text_label, obj_text_label)

        self.freeze()
        assert 'num_feats' in positional_encoding
        num_feats = positional_encoding['num_feats']
        assert num_feats * 2 == self.embed_dims, 'embed_dims should be exactly 2 times of num_feats.'

        self.input_proj = Conv2d(self.in_channels,
                                 self.embed_dims,
                                 kernel_size=1)
        self.s_dim_layer = Linear(512, 256)
        self.o_dim_layer = Linear(512, 256)
        self.box_embed = MLP(self.embed_dims, self.embed_dims, 4, 3)

        self.obj_class_fc = nn.Sequential(
            nn.Linear(self.embed_dims, 512),
            nn.LayerNorm(512),
        )
        self.obj_visual_projection = nn.Linear(512, self.sub_cls_out_channels)
        self.obj_visual_projection.weight.data = obj_clip_label / obj_clip_label.norm(dim=-1, keepdim=True)

        self.sub_class_fc = nn.Sequential(
            nn.Linear(self.embed_dims, 512),
            nn.LayerNorm(512),
        )
        self.sub_visual_projection = nn.Linear(512, self.sub_cls_out_channels)
        self.sub_visual_projection.weight.data = obj_clip_label / obj_clip_label.norm(dim=-1, keepdim=True)

        self.rel_class_fc = nn.Sequential(
            nn.Linear(self.embed_dims, 512),
            nn.LayerNorm(512),
        )

        self.visual_projection = nn.Linear(512, len(hoi_text))
        self.visual_projection.weight.data = train_clip_label / train_clip_label.norm(dim=-1, keepdim=True)

        self.query_embed_s = nn.Embedding(self.num_query, self.embed_dims)
        self.query_embed_o = nn.Embedding(self.num_query, self.embed_dims)
        self.pos_guided_embedd = nn.Embedding(self.num_query, self.embed_dims)

        if self.use_mask:
            self.bbox_attention = MHAttentionMap(self.embed_dims,
                                                 self.embed_dims,
                                                 self.n_heads,
                                                 dropout=0.0)
            if not self.swin:
                self.mask_head = MaskHeadSmallConv(
                    self.embed_dims + self.n_heads, [1024, 512, 256],
                    self.embed_dims)
            elif self.swin:
                self.mask_head = MaskHeadSmallConv(
                    self.embed_dims + self.n_heads, self.swin, self.embed_dims)
        self.reset_parameters()

    def init_weights(self):
        """Initialize weights of the transformer head."""
        # The initialization for transformer is important
        self.transformer.init_weights()

    def reset_parameters(self):
        nn.init.uniform_(self.pos_guided_embedd.weight)

    def freeze(self):
        for param in self.unet.parameters():
            param.requires_grad = False
        for param in self.clip_model.parameters():
            param.requires_grad = False
        for param in self.sd_model.parameters():
            param.requires_grad = False
        for param in self.encoder_vq.parameters():
            param.requires_grad = False
        for param in self.text_adapter.parameters():
            param.requires_grad = False

    def extract_clip_spatial_features(self, x):
        x = self.clip_model.visual.conv1(x)  # [B, embed_dim, H/patch, W/patch]
        return x

    def extract_feat(self, img, targets, clip_feats):
        """Extract features from images."""
        clip_feats = self.text_adapter(clip_feats.float())
        target_sd_inputs = torch.cat([t['sd_inputs'].unsqueeze(0) for t in targets])
        t = torch.zeros((img.shape[0],), device=img.device).long()
        with torch.no_grad():
            latents = self.encoder_vq.encode(target_sd_inputs)
            latents = latents.mode().detach()
            outs = self.unet(latents, t, c_crossattn=[clip_feats])
        return outs

    def init_classifier_with_CLIP(self, relation_text_label, obj_text_label):
        device = "cuda" if torch.cuda.is_available() else "cpu"
        text_inputs = torch.cat([clip.tokenize(relation_text_label[id]) for id in relation_text_label.keys()])
        hoi_text_label_del = relation_text_label.copy()
        text_inputs_del = torch.cat(
            [clip.tokenize(relation_text_label[id]) for id in hoi_text_label_del.keys()])
        obj_text_inputs = torch.cat([clip.tokenize(obj_text[1]) for obj_text in obj_text_label])
        clip_model, preprocess = clip.load("ViT-B/32", device=device)
        clip_model.eval()
        with torch.no_grad():
            text_embedding = clip_model.encode_text(text_inputs.to(device))
            text_embedding_del = clip_model.encode_text(text_inputs_del.to(device))
            obj_text_embedding = clip_model.encode_text(obj_text_inputs.to(device))
            v_linear_proj_weight = clip_model.visual.proj.detach()
        del clip_model
        return text_embedding.float(), obj_text_embedding.float(), v_linear_proj_weight.float(), \
            hoi_text_label_del, obj_text_inputs, text_embedding_del.float()

    def forward(self, samples: NestedTensor, targets, is_training=True):
        input_query_bbox = input_query_label = attn_mask = dn_meta = None
        if isinstance(samples, (list, torch.Tensor)):
            samples = nested_tensor_from_tensor_list(samples)
        sd_feat, poss = self.backbone(samples)
        srcs = []
        masks = []
        for l, feat in enumerate(sd_feat):
            src, mask = feat.decompose()
            srcs.append(self.input_proj[l](src))
            masks.append(mask)
            assert mask is not None
        if self.num_feature_levels > len(srcs):
            _len_srcs = len(srcs)
            for l in range(_len_srcs, self.num_feature_levels):
                if l == _len_srcs:
                    src = self.input_proj[l](sd_feat[-1].tensors)
                else:
                    src = self.input_proj[l](srcs[-1])
                m = samples.mask
                mask = F.interpolate(m[None].float(), size=src.shape[-2:]).to(torch.bool)[0]
                pos_l = self.backbone[1](NestedTensor(src, mask)).to(src.dtype)
                srcs.append(src)
                masks.append(mask)
                poss.append(pos_l)
        batch_size = srcs[-1].size(0)

        sd_decoder_4 = self.image_adapter_4(sd_feat[-1])
        sd_decoder_3 = self.image_adapter_3(sd_feat[-2])
        sd_decoder_2 = self.image_adapter_2(sd_feat[-3])
        sd_decoder_1 = self.image_adapter_1(sd_feat[-4])
        sd_decoder_4 = torch.nn.functional.interpolate(sd_decoder_4, size=[srcs[-1].shape[-2], srcs[-1].shape[-1]])
        sd_decoder_3 = torch.nn.functional.interpolate(sd_decoder_3, size=[srcs[-2].shape[-2], srcs[-2].shape[-1]])
        sd_decoder_2 = torch.nn.functional.interpolate(sd_decoder_2, size=[srcs[-3].shape[-2], srcs[-3].shape[-1]])
        sd_decoder_1 = torch.nn.functional.interpolate(sd_decoder_1, size=[srcs[-4].shape[-2], srcs[-4].shape[-1]])
        sd_decoder = [sd_decoder_1, sd_decoder_2, sd_decoder_3, sd_decoder_4]

        h_hs, o_hs, memory = self.transformer(srcs, masks,
                                              self.query_embed_s.weight,
                                              self.query_embed_o.weight,
                                              self.pos_guided_embedd.weight,
                                              poss, sd_decoder)

        obj_outputs_coord = self.box_embed(o_hs).sigmoid()
        sub_outputs_coord = self.box_embed(h_hs).sigmoid()

        sub_logit_scale = self.sub_logit_scale.exp()
        h_hs = self.sub_class_fc(h_hs)
        h_hs = h_hs / h_hs.norm(dim=-1, keepdim=True)
        sub_outputs_class = sub_logit_scale * self.sub_visual_projection(h_hs)
        h_hs = self.s_dim_layer(h_hs)

        obj_logit_scale = self.obj_logit_scale.exp()
        o_hs = self.obj_class_fc(o_hs)
        o_hs = o_hs / o_hs.norm(dim=-1, keepdim=True)
        obj_outputs_class = obj_logit_scale * self.obj_visual_projection(o_hs)
        o_hs = self.o_dim_layer(o_hs)

        all_cls_scores = dict(sub=sub_outputs_class, obj=obj_outputs_class)
        inter_hs = self.RGT(obj_outputs_coord, sub_outputs_coord, masks, h_hs, o_hs)
        logit_scale = self.logit_scale.exp()
        inter_hs = self.rel_class_fc(inter_hs)
        outputs_inter_hs = inter_hs.clone()
        inter_hs = inter_hs / inter_hs.norm(dim=-1, keepdim=True)
        rel_outputs_class = logit_scale * self.visual_projection(inter_hs)
        all_cls_scores['rel'] = rel_outputs_class

        if self.use_mask:
            ########### for segmentation #############
            sub_bbox_mask = self.bbox_attention(h_hs[-1],
                                                memory,
                                                mask=masks)
            obj_bbox_mask = self.bbox_attention(o_hs[-1],
                                                memory,
                                                mask=masks)
            sub_seg_masks = self.mask_head(srcs, sub_bbox_mask,
                                           [srcs[2], srcs[1], srcs[0]])
            outputs_sub_seg_masks = sub_seg_masks.view(batch_size,
                                                       self.num_query,
                                                       sub_seg_masks.shape[-2],
                                                       sub_seg_masks.shape[-1])
            obj_seg_masks = self.mask_head(srcs, obj_bbox_mask,
                                           [srcs[2], srcs[1], srcs[0]])
            outputs_obj_seg_masks = obj_seg_masks.view(batch_size,
                                                       self.num_query,
                                                       obj_seg_masks.shape[-2],
                                                       obj_seg_masks.shape[-1])
            all_bbox_preds = dict(sub=sub_outputs_coord,
                                  obj=obj_outputs_coord,
                                  sub_seg=outputs_sub_seg_masks,
                                  obj_seg=outputs_obj_seg_masks)
            # ----------------- Added: 利用池化特征增强开放词汇预测 -----------------
            # 利用 CLIP 图像编码器提取空间特征
            clip_spatial_feats = self.extract_clip_spatial_features(samples.tensors)
            # 对 subject 和 object 分支分别进行 mask pooling
            pooled_sub_feats = self.pooling_module(clip_spatial_feats, outputs_sub_seg_masks)
            pooled_obj_feats = self.pooling_module(clip_spatial_feats, outputs_obj_seg_masks)
            # 对 relation 分支，将 subject 与 object 的 mask 相加后池化
            combined_masks = outputs_sub_seg_masks + outputs_obj_seg_masks
            combined_masks = torch.clamp(combined_masks, 0, 1)
            pooled_rel_feats = self.pooling_module(clip_spatial_feats, combined_masks)
            # 分别经过各自的线性投影，并乘以相应的 logit scale
            pooled_sub_logits = self.sub_logit_scale.exp() * self.pooling_sub_projection(pooled_sub_feats)
            pooled_obj_logits = self.obj_logit_scale.exp() * self.pooling_obj_projection(pooled_obj_feats)
            pooled_rel_logits = self.logit_scale.exp() * self.pooling_rel_projection(pooled_rel_feats)
            # 与原始预测的 logits 按几何均值融合
            final_sub_logits = torch.exp(
                self.alpha_pool * torch.log(sub_outputs_class + 1e-8) + (1 - self.alpha_pool) * torch.log(
                    pooled_sub_logits + 1e-8))
            final_obj_logits = torch.exp(
                self.alpha_pool * torch.log(obj_outputs_class + 1e-8) + (1 - self.alpha_pool) * torch.log(
                    pooled_obj_logits + 1e-8))
            final_rel_logits = torch.exp(
                self.alpha_pool * torch.log(rel_outputs_class + 1e-8) + (1 - self.alpha_pool) * torch.log(
                    pooled_rel_logits + 1e-8))
            all_cls_scores['sub'] = final_sub_logits
            all_cls_scores['obj'] = final_obj_logits
            all_cls_scores['rel'] = final_rel_logits
            # ------------------------------------------------------------------------
        else:
            all_bbox_preds = dict(sub=sub_outputs_coord, obj=obj_outputs_coord)
        return all_cls_scores, all_bbox_preds

    @force_fp32(apply_to=("all_cls_scores_list", "all_bbox_preds_list"))
    def loss(
            self,
            all_cls_scores_list,
            all_bbox_preds_list,
            gt_rels_list,
            gt_bboxes_list,
            gt_labels_list,
            gt_masks_list,
            img_metas,
            gt_bboxes_ignore=None,
    ):
        # NOTE defaultly only the outputs from the last feature scale is used.
        all_cls_scores = all_cls_scores_list
        all_bbox_preds = all_bbox_preds_list
        assert (
                gt_bboxes_ignore is None
        ), "Only supports for gt_bboxes_ignore setting to None."
        all_s_cls_scores = all_cls_scores["sub"]
        all_o_cls_scores = all_cls_scores["obj"]

        all_s_bbox_preds = all_bbox_preds["sub"]
        all_o_bbox_preds = all_bbox_preds["obj"]

        num_dec_layers = len(all_s_cls_scores)

        if self.use_mask:
            all_s_mask_preds = all_bbox_preds["sub_seg"]
            all_o_mask_preds = all_bbox_preds["obj_seg"]
            all_s_mask_preds = [all_s_mask_preds for _ in range(num_dec_layers)]
            all_o_mask_preds = [all_o_mask_preds for _ in range(num_dec_layers)]

        all_gt_bboxes_list = [gt_bboxes_list for _ in range(num_dec_layers)]
        all_gt_labels_list = [gt_labels_list for _ in range(num_dec_layers)]
        all_gt_rels_list = [gt_rels_list for _ in range(num_dec_layers)]
        all_gt_bboxes_ignore_list = [gt_bboxes_ignore for _ in range(num_dec_layers)]
        all_gt_masks_list = [gt_masks_list for _ in range(num_dec_layers)]
        img_metas_list = [img_metas for _ in range(num_dec_layers)]

        all_r_cls_scores = [None for _ in range(num_dec_layers)]
        all_r_cls_scores = all_cls_scores["rel"]

        if self.use_mask:
            # s_losses_cls, o_losses_cls, r_losses_cls, s_losses_bbox, o_losses_bbox, s_losses_iou, o_losses_iou, s_focal_losses, s_dice_losses, o_focal_losses, o_dice_losses = multi_apply(
            #     self.loss_single, all_s_cls_scores, all_o_cls_scores, all_r_cls_scores, all_s_bbox_preds, all_o_bbox_preds,
            #     all_s_mask_preds, all_o_mask_preds,
            #     all_gt_rels_list,all_gt_bboxes_list, all_gt_labels_list,
            #     all_gt_masks_list, img_metas_list,
            #     all_gt_bboxes_ignore_list)
            (
                s_losses_cls,
                o_losses_cls,
                r_losses_cls,
                s_losses_bbox,
                o_losses_bbox,
                s_losses_iou,
                o_losses_iou,
                s_dice_losses,
                o_dice_losses,
            ) = multi_apply(
                self.loss_single,
                all_s_cls_scores,
                all_o_cls_scores,
                all_r_cls_scores,
                all_s_bbox_preds,
                all_o_bbox_preds,
                all_s_mask_preds,
                all_o_mask_preds,
                all_gt_rels_list,
                all_gt_bboxes_list,
                all_gt_labels_list,
                all_gt_masks_list,
                img_metas_list,
                all_gt_bboxes_ignore_list,
            )
        else:
            all_s_mask_preds = [None for _ in range(num_dec_layers)]
            all_o_mask_preds = [None for _ in range(num_dec_layers)]
            (
                s_losses_cls,
                o_losses_cls,
                r_losses_cls,
                s_losses_bbox,
                o_losses_bbox,
                s_losses_iou,
                o_losses_iou,
                s_dice_losses,
                o_dice_losses,
            ) = multi_apply(
                self.loss_single,
                all_s_cls_scores,
                all_o_cls_scores,
                all_r_cls_scores,
                all_s_bbox_preds,
                all_o_bbox_preds,
                all_s_mask_preds,
                all_o_mask_preds,
                all_gt_rels_list,
                all_gt_bboxes_list,
                all_gt_labels_list,
                all_gt_masks_list,
                img_metas_list,
                all_gt_bboxes_ignore_list,
            )

        loss_dict = dict()
        # loss from the last decoder layer
        loss_dict["s_loss_cls"] = s_losses_cls[-1]
        loss_dict["o_loss_cls"] = o_losses_cls[-1]
        loss_dict["r_loss_cls"] = r_losses_cls[-1]
        loss_dict["s_loss_bbox"] = s_losses_bbox[-1]
        loss_dict["o_loss_bbox"] = o_losses_bbox[-1]
        loss_dict["s_loss_iou"] = s_losses_iou[-1]
        loss_dict["o_loss_iou"] = o_losses_iou[-1]
        if self.use_mask:
            # loss_dict['s_focal_losses'] = s_focal_losses[-1]
            # loss_dict['o_focal_losses'] = o_focal_losses[-1]
            loss_dict["s_dice_losses"] = s_dice_losses[-1]
            loss_dict["o_dice_losses"] = o_dice_losses[-1]

        # loss from other decoder layers
        num_dec_layer = 0
        for (
                s_loss_cls_i,
                o_loss_cls_i,
                r_loss_cls_i,
                s_loss_bbox_i,
                o_loss_bbox_i,
                s_loss_iou_i,
                o_loss_iou_i,
        ) in zip(
            s_losses_cls[:-1],
            o_losses_cls[:-1],
            r_losses_cls[:-1],
            s_losses_bbox[:-1],
            o_losses_bbox[:-1],
            s_losses_iou[:-1],
            o_losses_iou[:-1],
        ):
            loss_dict[f"d{num_dec_layer}.s_loss_cls"] = s_loss_cls_i
            loss_dict[f"d{num_dec_layer}.o_loss_cls"] = o_loss_cls_i
            loss_dict[f"d{num_dec_layer}.r_loss_cls"] = r_loss_cls_i
            loss_dict[f"d{num_dec_layer}.s_loss_bbox"] = s_loss_bbox_i
            loss_dict[f"d{num_dec_layer}.o_loss_bbox"] = o_loss_bbox_i
            loss_dict[f"d{num_dec_layer}.s_loss_iou"] = s_loss_iou_i
            loss_dict[f"d{num_dec_layer}.o_loss_iou"] = o_loss_iou_i
            num_dec_layer += 1
        return loss_dict

    def loss_single(
            self,
            s_cls_scores,
            o_cls_scores,
            r_cls_scores,
            s_bbox_preds,
            o_bbox_preds,
            s_mask_preds,
            o_mask_preds,
            gt_rels_list,
            gt_bboxes_list,
            gt_labels_list,
            gt_masks_list,
            img_metas,
            gt_bboxes_ignore_list=None,
    ):
        num_imgs = s_cls_scores.size(0)

        s_cls_scores_list = [s_cls_scores[i] for i in range(num_imgs)]
        o_cls_scores_list = [o_cls_scores[i] for i in range(num_imgs)]
        r_cls_scores_list = [r_cls_scores[i] for i in range(num_imgs)]
        s_bbox_preds_list = [s_bbox_preds[i] for i in range(num_imgs)]
        o_bbox_preds_list = [o_bbox_preds[i] for i in range(num_imgs)]

        if self.use_mask:
            s_mask_preds_list = [s_mask_preds[i] for i in range(num_imgs)]
            o_mask_preds_list = [o_mask_preds[i] for i in range(num_imgs)]
        else:
            s_mask_preds_list = [None for i in range(num_imgs)]
            o_mask_preds_list = [None for i in range(num_imgs)]

        cls_reg_targets = self.get_targets(
            s_cls_scores_list,
            o_cls_scores_list,
            r_cls_scores_list,
            s_bbox_preds_list,
            o_bbox_preds_list,
            s_mask_preds_list,
            o_mask_preds_list,
            gt_rels_list,
            gt_bboxes_list,
            gt_labels_list,
            gt_masks_list,
            img_metas,
            gt_bboxes_ignore_list,
        )

        (
            s_labels_list,
            o_labels_list,
            r_labels_list,
            s_label_weights_list,
            o_label_weights_list,
            r_label_weights_list,
            s_bbox_targets_list,
            o_bbox_targets_list,
            s_bbox_weights_list,
            o_bbox_weights_list,
            s_mask_targets_list,
            o_mask_targets_list,
            num_total_pos,
            num_total_neg,
            s_mask_preds_list,
            o_mask_preds_list,
        ) = cls_reg_targets
        s_labels = torch.cat(s_labels_list, 0)
        o_labels = torch.cat(o_labels_list, 0)
        r_labels = torch.cat(r_labels_list, 0)

        s_label_weights = torch.cat(s_label_weights_list, 0)
        o_label_weights = torch.cat(o_label_weights_list, 0)
        r_label_weights = torch.cat(r_label_weights_list, 0)

        s_bbox_targets = torch.cat(s_bbox_targets_list, 0)
        o_bbox_targets = torch.cat(o_bbox_targets_list, 0)

        s_bbox_weights = torch.cat(s_bbox_weights_list, 0)
        o_bbox_weights = torch.cat(o_bbox_weights_list, 0)

        if self.use_mask:
            s_mask_targets = torch.cat(s_mask_targets_list, 0).float().flatten(1)
            o_mask_targets = torch.cat(o_mask_targets_list, 0).float().flatten(1)

            s_mask_preds = torch.cat(s_mask_preds_list, 0).flatten(1)
            o_mask_preds = torch.cat(o_mask_preds_list, 0).flatten(1)
            num_matches = o_mask_preds.shape[0]

            # mask loss
            # s_focal_loss = self.sub_focal_loss(s_mask_preds,s_mask_targets,num_matches)
            s_dice_loss = self.sub_dice_loss(s_mask_preds, s_mask_targets, num_matches)

            # o_focal_loss = self.obj_focal_loss(o_mask_preds,o_mask_targets,num_matches)
            o_dice_loss = self.obj_dice_loss(o_mask_preds, o_mask_targets, num_matches)
        else:
            s_dice_loss = None
            o_dice_loss = None

        # classification loss
        s_cls_scores = s_cls_scores.reshape(-1, self.sub_cls_out_channels)
        o_cls_scores = o_cls_scores.reshape(-1, self.obj_cls_out_channels)
        r_cls_scores = r_cls_scores.reshape(-1, self.rel_cls_out_channels)

        # construct weighted avg_factor to match with the official DETR repo
        cls_avg_factor = num_total_pos * 1.0 + num_total_neg * self.bg_cls_weight
        if self.sync_cls_avg_factor:
            cls_avg_factor = reduce_mean(s_cls_scores.new_tensor([cls_avg_factor]))
        cls_avg_factor = max(cls_avg_factor, 1)

        ###NOTE change cls_avg_factor for objects as we do not calculate object classification loss for unmatched ones

        s_loss_cls = self.sub_loss_cls(
            s_cls_scores, s_labels, s_label_weights, avg_factor=num_total_pos * 1.0
        )

        o_loss_cls = self.obj_loss_cls(
            o_cls_scores, o_labels, o_label_weights, avg_factor=num_total_pos * 1.0
        )

        r_loss_cls = self.rel_loss_cls(
            r_cls_scores, r_labels, r_label_weights, avg_factor=cls_avg_factor
        )

        # Compute the average number of gt boxes across all gpus, for
        # normalization purposes
        num_total_pos = o_loss_cls.new_tensor([num_total_pos])
        num_total_pos = torch.clamp(reduce_mean(num_total_pos), min=1).item()

        # construct factors used for rescale bboxes
        factors = []
        for img_meta, bbox_pred in zip(img_metas, s_bbox_preds):
            img_h, img_w, _ = img_meta["img_shape"]
            factor = (
                bbox_pred.new_tensor([img_w, img_h, img_w, img_h])
                .unsqueeze(0)
                .repeat(bbox_pred.size(0), 1)
            )
            factors.append(factor)
        factors = torch.cat(factors, 0)

        # DETR regress the relative position of boxes (cxcywh) in the image,
        # thus the learning target is normalized by the image size. So here
        # we need to re-scale them for calculating IoU loss
        s_bbox_preds = s_bbox_preds.reshape(-1, 4)
        s_bboxes = bbox_cxcywh_to_xyxy(s_bbox_preds) * factors
        s_bboxes_gt = bbox_cxcywh_to_xyxy(s_bbox_targets) * factors

        o_bbox_preds = o_bbox_preds.reshape(-1, 4)
        o_bboxes = bbox_cxcywh_to_xyxy(o_bbox_preds) * factors
        o_bboxes_gt = bbox_cxcywh_to_xyxy(o_bbox_targets) * factors

        # regression IoU loss, defaultly GIoU loss
        s_loss_iou = self.sub_loss_iou(
            s_bboxes, s_bboxes_gt, s_bbox_weights, avg_factor=num_total_pos
        )
        o_loss_iou = self.obj_loss_iou(
            o_bboxes, o_bboxes_gt, o_bbox_weights, avg_factor=num_total_pos
        )

        # regression L1 loss
        s_loss_bbox = self.sub_loss_bbox(
            s_bbox_preds, s_bbox_targets, s_bbox_weights, avg_factor=num_total_pos
        )
        o_loss_bbox = self.obj_loss_bbox(
            o_bbox_preds, o_bbox_targets, o_bbox_weights, avg_factor=num_total_pos
        )
        # return s_loss_cls, o_loss_cls, r_loss_cls, s_loss_bbox, o_loss_bbox, s_loss_iou, o_loss_iou, s_focal_loss, s_dice_loss, o_focal_loss, o_dice_loss
        return (
            s_loss_cls,
            o_loss_cls,
            r_loss_cls,
            s_loss_bbox,
            o_loss_bbox,
            s_loss_iou,
            o_loss_iou,
            s_dice_loss,
            o_dice_loss,
        )

    def get_targets(
            self,
            s_cls_scores_list,
            o_cls_scores_list,
            r_cls_scores_list,
            s_bbox_preds_list,
            o_bbox_preds_list,
            s_mask_preds_list,
            o_mask_preds_list,
            gt_rels_list,
            gt_bboxes_list,
            gt_labels_list,
            gt_masks_list,
            img_metas,
            gt_bboxes_ignore_list=None,
    ):
        assert (
                gt_bboxes_ignore_list is None
        ), "Only supports for gt_bboxes_ignore setting to None."
        num_imgs = len(s_cls_scores_list)
        gt_bboxes_ignore_list = [gt_bboxes_ignore_list for _ in range(num_imgs)]

        (
            s_labels_list,
            o_labels_list,
            r_labels_list,
            s_label_weights_list,
            o_label_weights_list,
            r_label_weights_list,
            s_bbox_targets_list,
            o_bbox_targets_list,
            s_bbox_weights_list,
            o_bbox_weights_list,
            s_mask_targets_list,
            o_mask_targets_list,
            pos_inds_list,
            neg_inds_list,
            s_mask_preds_list,
            o_mask_preds_list,
        ) = multi_apply(
            self._get_target_single,
            s_cls_scores_list,
            o_cls_scores_list,
            r_cls_scores_list,
            s_bbox_preds_list,
            o_bbox_preds_list,
            s_mask_preds_list,
            o_mask_preds_list,
            gt_rels_list,
            gt_bboxes_list,
            gt_labels_list,
            gt_masks_list,
            img_metas,
            gt_bboxes_ignore_list,
        )
        num_total_pos = sum((inds.numel() for inds in pos_inds_list))
        num_total_neg = sum((inds.numel() for inds in neg_inds_list))
        return (
            s_labels_list,
            o_labels_list,
            r_labels_list,
            s_label_weights_list,
            o_label_weights_list,
            r_label_weights_list,
            s_bbox_targets_list,
            o_bbox_targets_list,
            s_bbox_weights_list,
            o_bbox_weights_list,
            s_mask_targets_list,
            o_mask_targets_list,
            num_total_pos,
            num_total_neg,
            s_mask_preds_list,
            o_mask_preds_list,
        )

    def _get_target_single(
            self,
            s_cls_score,
            o_cls_score,
            r_cls_score,
            s_bbox_pred,
            o_bbox_pred,
            s_mask_preds,
            o_mask_preds,
            gt_rels,
            gt_bboxes,
            gt_labels,
            gt_masks,
            img_meta,
            gt_bboxes_ignore=None,
    ):
        """ "Compute regression and classification targets for one image.

        Outputs from a single decoder layer of a single feature level are used.

        Args:
            s_cls_score (Tensor): Subject box score logits from a single decoder layer
                for one image. Shape [num_query, cls_out_channels].
            o_cls_score (Tensor): Object box score logits from a single decoder layer
                for one image. Shape [num_query, cls_out_channels].
            r_cls_score (Tensor): Relation score logits from a single decoder layer
                for one image. Shape [num_query, cls_out_channels].
            s_bbox_pred (Tensor): Sigmoid outputs of Subject bboxes from a single decoder layer
                for one image, with normalized coordinate (cx, cy, w, h) and
                shape [num_query, 4].
            o_bbox_pred (Tensor): Sigmoid outputs of object bboxes from a single decoder layer
                for one image, with normalized coordinate (cx, cy, w, h) and
                shape [num_query, 4].
            s_mask_preds (Tensor): Logits before sigmoid subject masks from a single decoder layer
                for one image, with shape [num_query, H, W].
            o_mask_preds (Tensor): Logits before sigmoid object masks from a single decoder layer
                for one image, with shape [num_query, H, W].
            gt_rels (Tensor): Ground truth relation triplets for one image with
                shape (num_gts, 3) in [gt_sub_id, gt_obj_id, gt_rel_class] format.
            gt_bboxes (Tensor): Ground truth bboxes for one image with
                shape (num_gts, 4) in [tl_x, tl_y, br_x, br_y] format.
            gt_labels (Tensor): Ground truth class indices for one image
                with shape (num_gts, ).
            img_meta (dict): Meta information for one image.
            gt_bboxes_ignore (Tensor, optional): Bounding boxes
                which can be ignored. Default None.

        Returns:
            tuple[Tensor]: a tuple containing the following for one image.

                - s/o/r_labels (Tensor): Labels of each image.
                - s/o/r_label_weights (Tensor]): Label weights of each image.
                - s/o_bbox_targets (Tensor): BBox targets of each image.
                - s/o_bbox_weights (Tensor): BBox weights of each image.
                - s/o_mask_targets (Tensor): Mask targets of each image.
                - pos_inds (Tensor): Sampled positive indices for each image.
                - neg_inds (Tensor): Sampled negative indices for each image.
                - s/o_mask_preds (Tensor): Matched mask preds of each image.
        """

        num_bboxes = s_bbox_pred.size(0)
        gt_sub_bboxes = []
        gt_obj_bboxes = []
        gt_sub_labels = []
        gt_obj_labels = []
        gt_rel_labels = []
        if self.use_mask:
            gt_sub_masks = []
            gt_obj_masks = []

        assert len(gt_masks) == len(gt_bboxes)

        for rel_id in range(gt_rels.size(0)):
            gt_sub_bboxes.append(gt_bboxes[int(gt_rels[rel_id, 0])])
            gt_obj_bboxes.append(gt_bboxes[int(gt_rels[rel_id, 1])])
            gt_sub_labels.append(gt_labels[int(gt_rels[rel_id, 0])])
            gt_obj_labels.append(gt_labels[int(gt_rels[rel_id, 1])])
            gt_rel_labels.append(gt_rels[rel_id, 2])
            if self.use_mask:
                gt_sub_masks.append(gt_masks[int(gt_rels[rel_id, 0])].unsqueeze(0))
                gt_obj_masks.append(gt_masks[int(gt_rels[rel_id, 1])].unsqueeze(0))

        gt_sub_bboxes = torch.vstack(gt_sub_bboxes).type_as(gt_bboxes).reshape(-1, 4)
        gt_obj_bboxes = torch.vstack(gt_obj_bboxes).type_as(gt_bboxes).reshape(-1, 4)
        gt_sub_labels = torch.vstack(gt_sub_labels).type_as(gt_labels).reshape(-1)
        gt_obj_labels = torch.vstack(gt_obj_labels).type_as(gt_labels).reshape(-1)
        gt_rel_labels = torch.vstack(gt_rel_labels).type_as(gt_labels).reshape(-1)

        # assigner and sampler, only return subject&object assign result
        s_assign_result, o_assign_result = self.assigner.assign(
            s_bbox_pred,
            o_bbox_pred,
            s_cls_score,
            o_cls_score,
            r_cls_score,
            gt_sub_bboxes,
            gt_obj_bboxes,
            gt_sub_labels,
            gt_obj_labels,
            gt_rel_labels,
            img_meta,
            gt_bboxes_ignore,
        )

        s_sampling_result = self.sampler.sample(
            s_assign_result, s_bbox_pred, gt_sub_bboxes
        )
        o_sampling_result = self.sampler.sample(
            o_assign_result, o_bbox_pred, gt_obj_bboxes
        )
        pos_inds = o_sampling_result.pos_inds
        neg_inds = o_sampling_result.neg_inds  #### no-rel class indices in prediction

        # label targets
        s_labels = gt_sub_bboxes.new_full(
            (num_bboxes,), self.num_classes, dtype=torch.long
        )  ### 0-based, class [num_classes]  as background
        s_labels[pos_inds] = gt_sub_labels[s_sampling_result.pos_assigned_gt_inds]
        s_label_weights = gt_sub_bboxes.new_zeros(num_bboxes)
        s_label_weights[pos_inds] = 1.0

        o_labels = gt_obj_bboxes.new_full(
            (num_bboxes,), self.num_classes, dtype=torch.long
        )  ### 0-based, class [num_classes] as background
        o_labels[pos_inds] = gt_obj_labels[o_sampling_result.pos_assigned_gt_inds]
        o_label_weights = gt_obj_bboxes.new_zeros(num_bboxes)
        o_label_weights[pos_inds] = 1.0

        r_labels = gt_obj_bboxes.new_full(
            (num_bboxes,), 0, dtype=torch.long
        )  ### 1-based, class 0 as background
        r_labels[pos_inds] = gt_rel_labels[o_sampling_result.pos_assigned_gt_inds]
        r_label_weights = gt_obj_bboxes.new_ones(num_bboxes)

        if self.use_mask:
            gt_sub_masks = torch.cat(gt_sub_masks, axis=0).type_as(gt_masks[0])
            gt_obj_masks = torch.cat(gt_obj_masks, axis=0).type_as(gt_masks[0])

            assert gt_sub_masks.size() == gt_obj_masks.size()
            # mask targets for subjects and objects
            s_mask_targets = gt_sub_masks[s_sampling_result.pos_assigned_gt_inds, ...]
            s_mask_preds = s_mask_preds[pos_inds]

            o_mask_targets = gt_obj_masks[o_sampling_result.pos_assigned_gt_inds, ...]
            o_mask_preds = o_mask_preds[pos_inds]

            s_mask_preds = interpolate(
                s_mask_preds[:, None],
                size=gt_sub_masks.shape[-2:],
                mode="bilinear",
                align_corners=False,
            ).squeeze(1)

            o_mask_preds = interpolate(
                o_mask_preds[:, None],
                size=gt_obj_masks.shape[-2:],
                mode="bilinear",
                align_corners=False,
            ).squeeze(1)
        else:
            s_mask_targets = None
            s_mask_preds = None
            o_mask_targets = None
            o_mask_preds = None

        # bbox targets for subjects and objects
        s_bbox_targets = torch.zeros_like(s_bbox_pred)
        s_bbox_weights = torch.zeros_like(s_bbox_pred)
        s_bbox_weights[pos_inds] = 1.0

        o_bbox_targets = torch.zeros_like(o_bbox_pred)
        o_bbox_weights = torch.zeros_like(o_bbox_pred)
        o_bbox_weights[pos_inds] = 1.0
        img_h, img_w, _ = img_meta["img_shape"]

        # DETR regress the relative position of boxes (cxcywh) in the image.
        # Thus the learning target should be normalized by the image size, also
        # the box format should be converted from defaultly x1y1x2y2 to cxcywh.
        factor = o_bbox_pred.new_tensor([img_w, img_h, img_w, img_h]).unsqueeze(0)

        pos_gt_s_bboxes_normalized = s_sampling_result.pos_gt_bboxes / factor
        pos_gt_s_bboxes_targets = bbox_xyxy_to_cxcywh(pos_gt_s_bboxes_normalized)
        s_bbox_targets[pos_inds] = pos_gt_s_bboxes_targets

        pos_gt_o_bboxes_normalized = o_sampling_result.pos_gt_bboxes / factor
        pos_gt_o_bboxes_targets = bbox_xyxy_to_cxcywh(pos_gt_o_bboxes_normalized)
        o_bbox_targets[pos_inds] = pos_gt_o_bboxes_targets

        return (
            s_labels,
            o_labels,
            r_labels,
            s_label_weights,
            o_label_weights,
            r_label_weights,
            s_bbox_targets,
            o_bbox_targets,
            s_bbox_weights,
            o_bbox_weights,
            s_mask_targets,
            o_mask_targets,
            pos_inds,
            neg_inds,
            s_mask_preds,
            o_mask_preds,
        )  ###return the interpolated predicted masks

    # over-write because img_metas are needed as inputs for bbox_head.
    def forward_train(
            self,
            x,
            img_metas,
            gt_rels,
            gt_bboxes,
            gt_labels=None,
            gt_masks=None,
            gt_bboxes_ignore=None,
            proposal_cfg=None,
            **kwargs,
    ):

        assert proposal_cfg is None, '"proposal_cfg" must be None'
        outs = self(x, img_metas)
        if gt_labels is None:
            loss_inputs = outs + (gt_rels, gt_bboxes, gt_masks, img_metas)
        else:
            loss_inputs = outs + (gt_rels, gt_bboxes, gt_labels, gt_masks, img_metas)
        losses = self.loss(*loss_inputs, gt_bboxes_ignore=gt_bboxes_ignore)
        return losses

    @force_fp32(apply_to=("all_cls_scores_list", "all_bbox_preds_list"))
    def get_bboxes(self, cls_scores, bbox_preds, img_metas, rescale=False):
        # NOTE defaultly only using outputs from the last feature level,
        # and only the outputs from the last decoder layer is used.

        result_list = []
        for img_id in range(len(img_metas)):
            s_cls_score = cls_scores["sub"][-1, img_id, ...]
            o_cls_score = cls_scores["obj"][-1, img_id, ...]
            r_cls_score = cls_scores["rel"][-1, img_id, ...]
            s_bbox_pred = bbox_preds["sub"][-1, img_id, ...]
            o_bbox_pred = bbox_preds["obj"][-1, img_id, ...]
            img_shape = img_metas[img_id]["img_shape"]
            scale_factor = img_metas[img_id]["scale_factor"]
            if self.use_mask:
                s_mask_pred = bbox_preds["sub_seg"][img_id, ...]
                o_mask_pred = bbox_preds["obj_seg"][img_id, ...]
            else:
                s_mask_pred = None
                o_mask_pred = None
            triplets = self._get_bboxes_single(
                s_cls_score,
                o_cls_score,
                r_cls_score,
                s_bbox_pred,
                o_bbox_pred,
                s_mask_pred,
                o_mask_pred,
                img_shape,
                scale_factor,
                rescale,
            )
            result_list.append(triplets)

        return result_list

    def _get_bboxes_single(
            self,
            s_cls_score,
            o_cls_score,
            r_cls_score,
            s_bbox_pred,
            o_bbox_pred,
            s_mask_pred,
            o_mask_pred,
            img_shape,
            scale_factor,
            rescale=False,
    ):
        assert len(s_cls_score) == len(o_cls_score)
        assert len(s_cls_score) == len(s_bbox_pred)
        assert len(s_cls_score) == len(o_bbox_pred)

        mask_size = (
            round(img_shape[0] / scale_factor[1]),
            round(img_shape[1] / scale_factor[0]),
        )
        max_per_img = self.test_cfg.get("max_per_img", self.num_query)

        assert self.sub_loss_cls.use_sigmoid == False
        assert self.obj_loss_cls.use_sigmoid == False
        assert self.rel_loss_cls.use_sigmoid == False
        assert len(s_cls_score) == len(r_cls_score)

        # 0-based label input for objects and self.num_classes as default background cls
        s_logits = F.softmax(s_cls_score, dim=-1)[..., :-1]
        o_logits = F.softmax(o_cls_score, dim=-1)[..., :-1]

        s_scores, s_labels = s_logits.max(-1)
        o_scores, o_labels = o_logits.max(-1)

        r_lgs = F.softmax(r_cls_score, dim=-1)
        r_logits = r_lgs[..., 1:]
        r_scores, r_indexes = r_logits.reshape(-1).topk(max_per_img)
        r_labels = r_indexes % self.num_relations + 1
        triplet_index = r_indexes // self.num_relations

        s_scores = s_scores[triplet_index]
        s_labels = s_labels[triplet_index] + 1
        s_bbox_pred = s_bbox_pred[triplet_index]

        o_scores = o_scores[triplet_index]
        o_labels = o_labels[triplet_index] + 1
        o_bbox_pred = o_bbox_pred[triplet_index]

        r_dists = r_lgs.reshape(-1, self.num_relations + 1)[
            triplet_index
        ]  #### NOTE: to match the evaluation in vg

        labels = torch.cat((s_labels, o_labels), 0)
        complete_labels = labels
        scores = torch.cat((s_scores, o_scores), 0)

        if self.use_mask:
            s_mask_pred = s_mask_pred[triplet_index]
            o_mask_pred = o_mask_pred[triplet_index]
            s_mask_pred = F.interpolate(
                s_mask_pred.unsqueeze(1), size=mask_size
            ).squeeze(1)
            o_mask_pred = F.interpolate(
                o_mask_pred.unsqueeze(1), size=mask_size
            ).squeeze(1)

            #### for panoptic postprocessing ####
            masks = torch.cat((s_mask_pred, o_mask_pred), 0)

            s_mask_pred = torch.sigmoid(s_mask_pred) > 0.85
            o_mask_pred = torch.sigmoid(o_mask_pred) > 0.85
            output_masks = torch.cat((s_mask_pred, o_mask_pred), 0)

            keep = (labels != s_logits.shape[-1] - 1) & (
                    scores > 0.85
            )  ## the threshold is set to 0.85
            labels = labels[keep] - 1
            masks = masks[keep]
            scores = scores[keep]
            h, w = masks.shape[-2:]

            if labels.numel() == 0:
                pan_img = torch.ones(mask_size).cpu().to(torch.long)
            else:
                masks = masks.flatten(1)
                stuff_equiv_classes = defaultdict(lambda: [])
                for k, label in enumerate(labels):
                    if label.item() >= 80:
                        stuff_equiv_classes[label.item()].append(k)

                def get_ids_area(masks, scores, dedup=False):
                    # This helper function creates the final panoptic segmentation image
                    # It also returns the area of the masks that appears on the image

                    m_id = masks.transpose(0, 1).softmax(-1)

                    if m_id.shape[-1] == 0:
                        # We didn't detect any mask :(
                        m_id = torch.zeros((h, w), dtype=torch.long, device=m_id.device)
                    else:
                        m_id = m_id.argmax(-1).view(h, w)

                    if dedup:
                        # Merge the masks corresponding to the same stuff class
                        for equiv in stuff_equiv_classes.values():
                            if len(equiv) > 1:
                                for eq_id in equiv:
                                    m_id.masked_fill_(m_id.eq(eq_id), equiv[0])

                    seg_img = m_id * INSTANCE_OFFSET + labels[m_id]
                    seg_img = seg_img.view(h, w).cpu().to(torch.long)
                    m_id = m_id.view(h, w).cpu()
                    area = []
                    for i in range(len(scores)):
                        area.append(m_id.eq(i).sum().item())
                    return area, seg_img

                area, pan_img = get_ids_area(masks, scores, dedup=True)
                if labels.numel() > 0:
                    # We know filter empty masks as long as we find some
                    while True:
                        filtered_small = torch.as_tensor(
                            [area[i] <= 4 for i, c in enumerate(labels)],
                            dtype=torch.bool,
                            device=keep.device,
                        )
                        if filtered_small.any().item():
                            scores = scores[~filtered_small]
                            labels = labels[~filtered_small]
                            masks = masks[~filtered_small]
                            area, pan_img = get_ids_area(masks, scores)
                        else:
                            break

        s_det_bboxes = bbox_cxcywh_to_xyxy(s_bbox_pred)
        s_det_bboxes[:, 0::2] = s_det_bboxes[:, 0::2] * img_shape[1]
        s_det_bboxes[:, 1::2] = s_det_bboxes[:, 1::2] * img_shape[0]
        s_det_bboxes[:, 0::2].clamp_(min=0, max=img_shape[1])
        s_det_bboxes[:, 1::2].clamp_(min=0, max=img_shape[0])
        if rescale:
            s_det_bboxes /= s_det_bboxes.new_tensor(scale_factor)
        s_det_bboxes = torch.cat((s_det_bboxes, s_scores.unsqueeze(1)), -1)

        o_det_bboxes = bbox_cxcywh_to_xyxy(o_bbox_pred)
        o_det_bboxes[:, 0::2] = o_det_bboxes[:, 0::2] * img_shape[1]
        o_det_bboxes[:, 1::2] = o_det_bboxes[:, 1::2] * img_shape[0]
        o_det_bboxes[:, 0::2].clamp_(min=0, max=img_shape[1])
        o_det_bboxes[:, 1::2].clamp_(min=0, max=img_shape[0])
        if rescale:
            o_det_bboxes /= o_det_bboxes.new_tensor(scale_factor)
        o_det_bboxes = torch.cat((o_det_bboxes, o_scores.unsqueeze(1)), -1)

        det_bboxes = torch.cat((s_det_bboxes, o_det_bboxes), 0)
        rel_pairs = torch.arange(len(det_bboxes), dtype=torch.int).reshape(2, -1).T

        if self.use_mask:
            return (
                det_bboxes,
                complete_labels,
                rel_pairs,
                output_masks,
                pan_img,
                r_scores,
                r_labels,
                r_dists,
            )
        else:
            return det_bboxes, labels, rel_pairs, r_scores, r_labels, r_dists

    def simple_test_bboxes(self, feats, img_metas, rescale=False):
        # forward of this head requires img_metas
        # start = time.time()
        outs = self.forward(feats, img_metas)
        # forward_time =time.time()
        # print('------forward-----')
        # print(forward_time - start)
        results_list = self.get_bboxes(*outs, img_metas, rescale=rescale)
        # print('-----get_bboxes-----')
        # print(time.time() - forward_time)
        return results_list




class MLP(nn.Module):
    """Very simple multi-layer perceptron (also called FFN) Copied from
    hoitr."""

    def __init__(self, input_dim, hidden_dim, output_dim, num_layers):
        super().__init__()
        self.num_layers = num_layers
        h = [hidden_dim] * (num_layers - 1)
        self.layers = nn.ModuleList(
            nn.Linear(n, k) for n, k in zip([input_dim] + h, h + [output_dim])
        )

    def forward(self, x):
        for i, layer in enumerate(self.layers):
            x = F.relu(layer(x)) if i < self.num_layers - 1 else layer(x)
        return x




def _expand(tensor, length: int):
    return tensor.unsqueeze(1).repeat(1, int(length), 1, 1, 1).flatten(0, 1)

class MaskHeadSmallConv(nn.Module):
    """Simple convolutional head, using group norm.

    Upsampling is done using a FPN approach
    """

    def __init__(self, dim, fpn_dims, context_dim):
        super().__init__()

        inter_dims = [
            dim,
            context_dim // 2,
            context_dim // 4,
            context_dim // 8,
            context_dim // 16,
            context_dim // 64,
        ]
        self.lay1 = nn.Conv2d(dim, dim, 3, padding=1)
        self.gn1 = nn.GroupNorm(8, dim)
        self.lay2 = torch.nn.Conv2d(dim, inter_dims[1], 3, padding=1)
        self.gn2 = torch.nn.GroupNorm(8, inter_dims[1])
        self.lay3 = torch.nn.Conv2d(inter_dims[1], inter_dims[2], 3, padding=1)
        self.gn3 = torch.nn.GroupNorm(8, inter_dims[2])
        self.lay4 = torch.nn.Conv2d(inter_dims[2], inter_dims[3], 3, padding=1)
        self.gn4 = torch.nn.GroupNorm(8, inter_dims[3])
        self.lay5 = torch.nn.Conv2d(inter_dims[3], inter_dims[4], 3, padding=1)
        self.gn5 = torch.nn.GroupNorm(8, inter_dims[4])
        self.out_lay = torch.nn.Conv2d(inter_dims[4], 1, 3, padding=1)

        self.dim = dim

        self.adapter1 = torch.nn.Conv2d(fpn_dims[0], inter_dims[1], 1)
        self.adapter2 = torch.nn.Conv2d(fpn_dims[1], inter_dims[2], 1)
        self.adapter3 = torch.nn.Conv2d(fpn_dims[2], inter_dims[3], 1)

        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_uniform_(m.weight, a=1)
                nn.init.constant_(m.bias, 0)

    def forward(self, x, bbox_mask, fpns):
        x = torch.cat([_expand(x, bbox_mask.shape[1]), bbox_mask.flatten(0, 1)], 1)

        x = self.lay1(x)
        x = self.gn1(x)
        x = F.relu(x)
        x = self.lay2(x)
        x = self.gn2(x)
        x = F.relu(x)

        cur_fpn = self.adapter1(fpns[0])
        if cur_fpn.size(0) != x.size(0):
            cur_fpn = _expand(cur_fpn, x.size(0) // cur_fpn.size(0))
        x = cur_fpn + F.interpolate(x, size=cur_fpn.shape[-2:], mode="nearest")
        x = self.lay3(x)
        x = self.gn3(x)
        x = F.relu(x)

        cur_fpn = self.adapter2(fpns[1])
        if cur_fpn.size(0) != x.size(0):
            cur_fpn = _expand(cur_fpn, x.size(0) // cur_fpn.size(0))
        x = cur_fpn + F.interpolate(x, size=cur_fpn.shape[-2:], mode="nearest")
        x = self.lay4(x)
        x = self.gn4(x)
        x = F.relu(x)

        cur_fpn = self.adapter3(fpns[2])
        if cur_fpn.size(0) != x.size(0):
            cur_fpn = _expand(cur_fpn, x.size(0) // cur_fpn.size(0))
        x = cur_fpn + F.interpolate(x, size=cur_fpn.shape[-2:], mode="nearest")
        x = self.lay5(x)
        x = self.gn5(x)
        x = F.relu(x)

        x = self.out_lay(x)
        return x

class MHAttentionMap(nn.Module):
    """This is a 2D attention module, which only returns the attention softmax
    (no multiplication by value)"""

    def __init__(self, query_dim, hidden_dim, num_heads, dropout=0.0, bias=True):
        super().__init__()
        self.num_heads = num_heads
        self.hidden_dim = hidden_dim
        self.dropout = nn.Dropout(dropout)

        self.q_linear = nn.Linear(query_dim, hidden_dim, bias=bias)
        self.k_linear = nn.Linear(query_dim, hidden_dim, bias=bias)

        nn.init.zeros_(self.k_linear.bias)
        nn.init.zeros_(self.q_linear.bias)
        nn.init.xavier_uniform_(self.k_linear.weight)
        nn.init.xavier_uniform_(self.q_linear.weight)
        self.normalize_fact = float(hidden_dim / self.num_heads) ** -0.5

    def forward(self, q, k, mask=None):
        q = self.q_linear(q)
        k = F.conv2d(
            k, self.k_linear.weight.unsqueeze(-1).unsqueeze(-1), self.k_linear.bias
        )
        qh = q.view(
            q.shape[0], q.shape[1], self.num_heads, self.hidden_dim // self.num_heads
        )
        kh = k.view(
            k.shape[0],
            self.num_heads,
            self.hidden_dim // self.num_heads,
            k.shape[-2],
            k.shape[-1],
        )
        weights = torch.einsum("bqnc,bnchw->bqnhw", qh * self.normalize_fact, kh)

        if mask is not None:
            weights.masked_fill_(mask.unsqueeze(1).unsqueeze(1), float("-inf"))
        weights = F.softmax(weights.flatten(2), dim=-1).view(weights.size())
        weights = self.dropout(weights)
        return weights

def interpolate(
        input, size=None, scale_factor=None, mode="nearest", align_corners=None
):
    """Equivalent to nn.functional.interpolate, but with support for empty
    batch sizes.

    This will eventually be supported natively by PyTorch, and this class can
    go away.
    """
    if version.parse(torchvision.__version__) < version.parse("0.7"):
        if input.numel() > 0:
            return torch.nn.functional.interpolate(
                input, size, scale_factor, mode, align_corners
            )

        output_shape = _output_size(2, input, size, scale_factor)
        output_shape = list(input.shape[:-2]) + list(output_shape)
        return _new_empty_tensor(input, output_shape)
    else:
        return torchvision.ops.misc.interpolate(
            input, size, scale_factor, mode, align_corners
        )