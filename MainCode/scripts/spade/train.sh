#!/bin/bash
CUDA_VISIBLE_DEVICES=0,1 TORCH_DISTRIBUTED_DEBUG=DETAIL PYTHONPATH='.':$PYTHONPATH \
python -m torch.distributed.launch --nproc_per_node=2 --master_port=28500 \
tools/train.py configs/spade/spade.py --gpus 2 --launcher pytorch
