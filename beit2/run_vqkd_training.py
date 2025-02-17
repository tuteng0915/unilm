# --------------------------------------------------------
# BEiT v2: Masked Image Modeling with Vector-Quantized Visual Tokenizers (https://arxiv.org/abs/2208.06366)
# Github source: https://github.com/microsoft/unilm/tree/master/beitv2
# Copyright (c) 2022 Microsoft
# Licensed under The MIT License [see LICENSE for details]
# By Zhiliang Peng
# Based on BEiT, timm, DeiT and DINO code bases
# https://github.com/microsoft/unilm/tree/master/beit
# https://github.com/rwightman/pytorch-image-models/tree/master/timm
# https://github.com/facebookresearch/deit/
# https://github.com/facebookresearch/dino
# --------------------------------------------------------'

import argparse
import datetime
import numpy as np
import time
import torch
import torch.backends.cudnn as cudnn
import json
import os

from pathlib import Path

from timm.models import create_model
from optim_factory import create_optimizer

# from _datasets import build_vqkd_dataset
from data import get_data, get_dataset_fn, get_wds_dataset
from engine_for_vqkd import evaluate, train_one_epoch, calculate_codebook_usage
from utils import NativeScalerWithGradNormCount as NativeScaler
import utils

import modeling_vqkd



def get_args():
    parser = argparse.ArgumentParser('BEiT pre-training script', add_help=False)
    parser.add_argument('--batch_size', default=64, type=int)
    parser.add_argument('--epochs', default=10, type=int)
    parser.add_argument('--save_ckpt_freq', default=20, type=int)
    # Model parameters
    parser.add_argument('--model', default='vqkd_encoder_base_decoder_3x768x12_clip', type=str, metavar='MODEL',  help='Name of model to train')  

    parser.add_argument('--rec_loss_type', default='cosine', type=str, metavar='MODEL',
                        help='type of loss to calculate reconstruction distance')

    parser.add_argument('--codebook_n_emd', default=8192, type=int, metavar='MODEL',
                        help='number of codebook')
    parser.add_argument('--codebook_emd_dim', default=32, type=int, metavar='MODEL',
                        help='number of codebook')
    parser.add_argument('--ema_decay', default=0.99, type=float, metavar='MODEL', help='ema decay for quantizer')
    parser.add_argument('--quantize_kmeans_init', action='store_true', help='enable kmeans_init for quantizer')

    parser.add_argument('--process_type', default='default', type=str, choices=['default', 'dall-e', 'imagenet_norm', 'laion'],
                        help='Image process type (default, dall-e)')
    parser.add_argument('--input_size', default=224, type=int, help='images input size for backbone')

    # regress feature
    parser.add_argument('--teacher_model_type', default='open_clip_pix2struct', type=str, help='teacher_model_type during training')
    parser.add_argument('--teacher_input_size', default=224, type=int, help='teacher_input_size for clip-large p14')

    # Optimizer parameters
    parser.add_argument('--opt', default='adamw', type=str, metavar='OPTIMIZER',
                        help='Optimizer (default: "adamw"')
    parser.add_argument('--opt_eps', default=1e-8, type=float, metavar='EPSILON',
                        help='Optimizer Epsilon (default: 1e-8)')
    parser.add_argument('--opt_betas', default=None, type=float, nargs='+', metavar='BETA',
                        help='Optimizer Betas (default: None, use opt default)')
    parser.add_argument('--clip_grad', type=float, default=None, metavar='NORM',
                        help='Clip gradient norm (default: None, no clipping)')
    parser.add_argument('--weight_decay', type=float, default=1e-4,
                        help='weight decay (default: 1e-4)')
    parser.add_argument('--weight_decay_end', type=float, default=None, help="""Final value of the
        weight decay. We use a cosine schedule for WD. 
        (Set the same value with args.weight_decay to keep weight decay no change)""")

    parser.add_argument('--lr', type=float, default=5e-5, metavar='LR',
                        help='learning rate (default: 5e-5)')
    parser.add_argument('--warmup_lr', type=float, default=1e-6, metavar='LR',
                        help='warmup learning rate (default: 1e-6)')
    parser.add_argument('--min_lr', type=float, default=1e-5, metavar='LR',
                        help='lower lr bound for cyclic schedulers that hit 0 (1e-5)')

    parser.add_argument('--warmup_epochs', type=int, default=4, metavar='N',
                        help='epochs to warmup LR, if scheduler supports')
    parser.add_argument('--warmup_steps', type=int, default=-1, metavar='N',
                        help='epochs to warmup LR, if scheduler supports')

    # Augmentation parameters
    parser.add_argument('--color_jitter', type=float, default=0., metavar='PCT',
                        help='Color jitter factor (default: 0.)')
    parser.add_argument('--train_interpolation', type=str, default='bicubic',
                        help='Training interpolation (random, bilinear, bicubic, lanczos default: "bicubic")')
    parser.add_argument('--min_crop_scale', type=float, default=0.08, metavar='PCT',
                        help='min_crop_scale (default: 0.08)')

    # Dataset parameters
    parser.add_argument('--data_path', default='/zhangpai21/webdataset/laion-aes/train', type=str,
                        help='dataset path')
    parser.add_argument('--eval_data_path', default='/zhangpai21/dm/coco/train', type=str, help='dataset path')
    parser.add_argument('--data_set', default='laion', type=str, help='dataset path')
 
    parser.add_argument('--imagenet_default_mean_and_std', default=False, action='store_true')

    parser.add_argument('--output_dir', default='./tmp/',
                        help='path where to save, empty for no saving')
    parser.add_argument('--log_dir', default='./log/',
                        help='path where to tensorboard log')
    
    parser.add_argument('--device', default='cuda',
                        help='device to use for training / testing')
    parser.add_argument('--seed', default=0, type=int)
    parser.add_argument('--resume', default='', help='resume from checkpoint')
    parser.add_argument('--auto_resume', action='store_true')
    parser.add_argument('--no_auto_resume', action='store_false', dest='auto_resume')
    parser.set_defaults(auto_resume=True)

    parser.add_argument('--train_num_samples', default=None, type=int)

    # Eval
    parser.add_argument('--val_num_samples', default=100, type=int)
     
    parser.add_argument('--dist_eval', action='store_true', default=False,
                        help='Enabling distributed evaluation')
    parser.add_argument('--disable_eval', action='store_true', default=False)
    
    parser.add_argument('--eval', action='store_true', default=False, help="Perform evaluation only")
    parser.add_argument('--calculate_codebook_usage', action='store_true', default=False)

    parser.add_argument('--start_epoch', default=0, type=int, metavar='N',
                        help='start epoch')
    parser.add_argument('--num_workers', default=10, type=int)
    parser.add_argument('--pin_mem', action='store_true',
                        help='Pin CPU memory in DataLoader for more efficient (sometimes) transfer to GPU.')
    parser.add_argument('--no_pin_mem', action='store_false', dest='pin_mem',
                        help='')
    parser.set_defaults(pin_mem=True)
    
    # distributed training parameters
    parser.add_argument('--world_size', default=1, type=int,
                        help='number of distributed processes')
    parser.add_argument('--local_rank', default=-1, type=int)
    parser.add_argument('--dist_on_itp', action='store_true')
    parser.add_argument('--dist_url', default='env://', help='url used to set up distributed training')

    parser.add_argument('--workers', default=1, type=int)
    parser.add_argument('--dataset-resampled', action='store_true')

    return parser.parse_args()


def get_model(args, **kwargs):
       
    model = create_model(
        args.model,
        pretrained=False,
        as_tokenzer=False,
        n_code=args.codebook_n_emd,
        code_dim=args.codebook_emd_dim,
        img_size=args.input_size,
        rec_loss_type=args.rec_loss_type,
        teacher_model_type=args.teacher_model_type,
        teacher_input_size=args.teacher_input_size,
        decay=args.ema_decay,
        quantize_kmeans_init=args.quantize_kmeans_init,
        process_type=args.process_type
    )
    return model


def main(args):
    utils.init_distributed_mode(args)

    print(args)
    

    device = torch.device(args.device)

    # fix the seed for reproducibility
    seed = args.seed + utils.get_rank()
    torch.manual_seed(seed)
    np.random.seed(seed)
    # random.seed(seed)

    cudnn.benchmark = True

    model = get_model(args)

    # get dataset

    dataset_train = get_wds_dataset(args=args, preprocess_img=None, is_train=True, epoch=args.epochs, floor=False)
    dataset_val = get_wds_dataset(args=args, preprocess_img=None, is_train=False, epoch=args.epochs, floor=False)

    num_tasks = utils.get_world_size()
    global_rank = utils.get_rank()
    sampler_rank = global_rank
    num_training_steps_per_epoch = dataset_train.dataloader.num_batches // num_tasks

    # if True:  # args.distributed:
        
        # sampler_train = torch.utils.data.DistributedSampler(
        #     dataset_train, num_replicas=num_tasks, rank=sampler_rank, shuffle=True
        # )
        # print("Sampler_train = %s" % str(sampler_train))
        # if args.dist_eval:
        #     if len(dataset_val) % num_tasks != 0:
        #         print('Warning: Enabling distributed evaluation with an eval dataset not divisible by process number. '
        #                 'This will slightly alter validation results as extra duplicate entries are added to achieve '
        #                 'equal num of samples per-process.')
        #     sampler_val = torch.utils.data.DistributedSampler(
        #         dataset_val, num_replicas=num_tasks, rank=global_rank, shuffle=False)
        # else:
        #     sampler_val = torch.utils.data.SequentialSampler(dataset_val)
    # else:
    #     sampler_train = torch.utils.data.RandomSampler(dataset_train)
    #     sampler_val = torch.utils.data.SequentialSampler(dataset_val)

    if global_rank == 0 and args.log_dir is not None:
        os.makedirs(args.log_dir, exist_ok=True)
        log_writer = utils.TensorboardLogger(log_dir=args.log_dir)
    else:
        log_writer = None

    data_loader_train  = dataset_train.dataloader
    data_loader_val = dataset_val.dataloader

    model.to(device)
    model_without_ddp = model
    if not args.eval:
        print("Model = %s" % str(model_without_ddp))
    for part in ['encoder', 'decoder']:
        model_part = eval(f"model.{part}")
        n_learnable_parameters = sum(p.numel() for p in model_part.parameters() if p.requires_grad)
        n_fix_parameters = sum(p.numel() for p in model_part.parameters() if not p.requires_grad)
        print(f'number of learnable params in model.{part}: {n_learnable_parameters / 1e6} M')
        print(f'number of fixed params in model.{part}: {n_fix_parameters / 1e6} M')

    n_learnable_parameters = sum(p.numel() for p in model.parameters() if p.requires_grad)
    n_fix_parameters = sum(p.numel() for p in model.parameters() if not p.requires_grad)
    print(f'total number of learnable params: {n_learnable_parameters / 1e6} M')
    print(f'total number of fixed params in : {n_fix_parameters / 1e6} M')

    total_batch_size = args.batch_size * utils.get_world_size()
    args.lr = total_batch_size / 128 * args.lr
    print("LR = %.8f" % args.lr)
    print("Min LR = %.8f" % args.min_lr)
    print("Weigth Decay = %.8f" % args.weight_decay)
    print("Batch size = %d" % total_batch_size)
    print("Number of training steps = %d" % num_training_steps_per_epoch)
    print("Number of training examples per epoch = %d" % (total_batch_size * num_training_steps_per_epoch))

    optimizer = create_optimizer(args, model_without_ddp)
    loss_scaler = NativeScaler()

    if args.distributed:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu], find_unused_parameters=True)
        model_without_ddp = model.module

    print("Use step level LR & WD scheduler!")
    lr_schedule_values = utils.cosine_scheduler(
        args.lr, args.min_lr, args.epochs, num_training_steps_per_epoch,
        warmup_epochs=args.warmup_epochs, warmup_steps=args.warmup_steps,
    )
    
    print("utils.auto_load_model")
    utils.auto_load_model(
        args=args, model=model, model_without_ddp=model_without_ddp, optimizer=optimizer, loss_scaler=loss_scaler)
    
    if args.eval:
        print("start evaluating")
        test_stats = evaluate(data_loader_val, model, device, log_writer, 0, args=args)
        exit(0)
    
    if args.calculate_codebook_usage:
        print("start calculating codebook usage")
        test_stats = calculate_codebook_usage(data_loader_val, model, device, log_writer, 0, args=args)
        exit(0)
        
    print(f"Start training for {args.epochs} epochs")
    start_time = time.time()
            
    for epoch in range(args.start_epoch, args.epochs):
        # if args.distributed:
        #     data_loader_train.sampler.set_epoch(epoch)
        if log_writer is not None:
            log_writer.set_step(epoch * num_training_steps_per_epoch)
        train_stats = train_one_epoch(
            model, 
            data_loader_train,
            optimizer, 
            device, 
            epoch, 
            loss_scaler,
            args.clip_grad, 
            log_writer=log_writer,
            start_steps=epoch * num_training_steps_per_epoch,
            lr_schedule_values=lr_schedule_values,
            args=args
        )
        if args.output_dir:
            # if (epoch + 1) % args.save_ckpt_freq == 0 or epoch + 1 == args.epochs:
            utils.save_model(
                args=args, model=model, model_without_ddp=model_without_ddp, optimizer=optimizer,
                loss_scaler=loss_scaler, epoch=epoch, save_ckpt_freq=args.save_ckpt_freq)
        
        if data_loader_val is not None:
            test_stats = evaluate(data_loader_val, model, device, log_writer, epoch, args=args)
            print(f"Validation loss of the network on the {len(dataset_val)} test images: {test_stats['loss']:.4f}")

            if log_writer is not None:
                log_writer.update(**test_stats, head="val/loss")
                
            log_stats = {**{f'train_{k}': v for k, v in train_stats.items()},
                        **{f'test_{k}': v for k, v in test_stats.items()},
                        'epoch': epoch, 'n_parameters': n_learnable_parameters}
        else:
            log_stats = {**{f'train_{k}': v for k, v in train_stats.items()},
                        'epoch': epoch, 'n_parameters': n_learnable_parameters}

        if args.output_dir and utils.is_main_process():
            if log_writer is not None:
                log_writer.flush()
            with open(os.path.join(args.output_dir, "log.txt"), mode="a", encoding="utf-8") as f:
                f.write(json.dumps(log_stats) + "\n")

    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    print('Training time {}'.format(total_time_str))


if __name__ == '__main__':
    opts = get_args()
    if opts.output_dir:
        Path(opts.output_dir).mkdir(parents=True, exist_ok=True)
    main(opts)

'''
python -m torch.distributed.launch --nproc_per_node=1 run_vqkd_training.py \
    --data_set image_folder \
    --data_path /zhangpai21/tsinghua0906/dingming0629/image_net/train_combine \
    --eval_data_path /path/to/imagenet-1k/eval \
    --output_dir /path/to/save/your_model \
    --log_dir /path/to/save/your_model \
    --process_type default \
    --train_interpolation bicubic \
    --min_crop_scale 0.08 \
    --model vqkd_encoder_base_decoder_3x768x12_clip \
    --teacher_input_size 224 \
    --codebook_n_emd 8192  \
    --codebook_emd_dim 32 \
    --quantize_kmeans_init \
    --rec_loss_type cosine \
    --batch_size 64 \
    --opt adamw \
    --opt_betas 0.9 0.99 \
    --weight_decay 1e-4  \
    --warmup_epochs 10 \
    --epochs 100 \
    --save_ckpt_freq 20 
'''