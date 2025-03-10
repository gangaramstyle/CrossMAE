# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
# --------------------------------------------------------
# References:
# DeiT: https://github.com/facebookresearch/deit
# BEiT: https://github.com/microsoft/unilm/tree/master/beit
# --------------------------------------------------------

import sys
sys.path.append('/cbica/home/gangarav/projects/rsna_lumbar/src')
from torch.utils.data import IterableDataset, DataLoader
import zarr
import pandas as pd
import random

import argparse
import datetime
import json
import numpy as np
import os
import time
from pathlib import Path

import torch
import torch.backends.cudnn as cudnn
from torch.utils.tensorboard import SummaryWriter
import torchvision.transforms as transforms
import torchvision.datasets as datasets

import timm
from timm.data.loader import MultiEpochsDataLoader

# assert timm.__version__ == "0.3.2"  # version check
import timm.optim.optim_factory as optim_factory

import util.misc as misc
from util.misc import NativeScalerWithGradNormCount as NativeScaler
from transformer_utils import handle_flash_attn

import models_mae
import models_cross

from engine_pretrain import train_one_epoch


def get_args_parser():
    parser = argparse.ArgumentParser('MAE pre-training', add_help=False)
    parser.add_argument('--batch_size', default=64, type=int,
                        help='Batch size per GPU (effective batch size is batch_size * accum_iter * # gpus')
    parser.add_argument('--epochs', default=400, type=int)
    parser.add_argument('--accum_iter', default=1, type=int,
                        help='Accumulate gradient iterations (for increasing the effective batch size under memory constraints)')

    # Model parameters
    parser.add_argument('--model', default='mae_vit_large_patch16', type=str, metavar='MODEL',
                        help='Name of model to train')

    parser.add_argument('--input_size', default=224, type=int,
                        help='images input size')

    parser.add_argument('--decoder_depth', default=8, type=int, 
                        help='depth of decoder')

    parser.add_argument('--mask_ratio', default=0.75, type=float,
                        help='Masking ratio (1 - percentage of remained patches).')

    parser.add_argument('--kept_mask_ratio', default=0.75, type=float,
                        help='Amongst the all tokens, the percentage of the mask that are kept')
    parser.add_argument('--inverse_lr', action='store_true', default=False, help='Use inverse lr scheduler')
    parser.add_argument('--no_lr_scale', action='store_true', default=False, help='Do not scale lr by mask_ratio')
    
    parser.add_argument('--norm_pix_loss', action='store_true',
                        help='Use (per-patch) normalized pixels as targets for computing loss')
    parser.set_defaults(norm_pix_loss=False)

    parser.add_argument(
        '--find_unused_parameters', action='store_true',
        help="distributed ddp find unused parameters")
    parser.set_defaults(find_unused_parameters=False)

    # Optimizer parameters
    parser.add_argument('--weight_decay', type=float, default=0.05,
                        help='weight decay (default: 0.05)')

    parser.add_argument('--lr', type=float, default=None, metavar='LR',
                        help='learning rate (absolute lr)')
    parser.add_argument('--blr', type=float, default=1e-3, metavar='LR',
                        help='base learning rate: absolute_lr = base_lr * total_batch_size / 256')
    parser.add_argument('--min_lr', type=float, default=0., metavar='LR',
                        help='lower lr bound for cyclic schedulers that hit 0')

    parser.add_argument('--warmup_epochs', type=int, default=40, metavar='N',
                        help='epochs to warmup LR')

    # Dataset parameters
    parser.add_argument('--data_path', default='/datasets01/imagenet_full_size/061417/', type=str,
                        help='dataset path')

    parser.add_argument('--output_dir', default='./output_dir',
                        help='path where to save, empty for no saving')
    parser.add_argument('--log_dir', default=None,
                        help='path where to tensorboard log')
    parser.add_argument('--device', default='cuda',
                        help='device to use for training / testing')
    parser.add_argument('--seed', default=0, type=int)
    parser.add_argument('--resume', default='',
                        help='resume from checkpoint')

    parser.add_argument('--start_epoch', default=0, type=int, metavar='N',
                        help='start epoch')
    parser.add_argument('--num_workers', default=10, type=int)
    parser.add_argument('--pin_mem', action='store_true',
                        help='Pin CPU memory in DataLoader for more efficient (sometimes) transfer to GPU.')
    parser.add_argument('--no_pin_mem', action='store_false', dest='pin_mem')
    parser.set_defaults(pin_mem=True)
    
    parser.add_argument('--multi_epochs_dataloader', action='store_true', help='Use MultiEpochsDataLoader to prevent reinitializing dataloader per epoch')

    # distributed training parameters
    parser.add_argument('--world_size', default=1, type=int,
                        help='number of distributed processes')
    parser.add_argument('--local_rank', default=-1, type=int)
    parser.add_argument('--dist_on_itp', action='store_true')
    parser.add_argument('--dist_url', default='env://',
                        help='url used to set up distributed training')

    # MAE or cross-MAE
    parser.add_argument('--cross_mae', action='store_true', default=False)
    parser.add_argument('--weight_fm', action='store_true', default=False,
                        help='Weight the feature maps for decoder when running cross-mae')
    parser.add_argument('--use_fm', nargs='+', type=int, default=[-1], 
                        help='Feature maps to use for decoder')
    parser.add_argument('--use_input', action='store_true', default=False,
                        help="use input as a feature map")
    parser.add_argument('--self_attn', action='store_true', default=False, help="use self attention in decoder")
    
    parser.add_argument('--enable_flash_attention2', action='store_true', default=False, help="Use flash attntion 2")

    return parser


###############

class TrainDataset(IterableDataset):
    def __init__(self, mini_batch_size=10, fixed_shape=(1, 128, 128), local_normalize=True, real_world_pos=False, n_workers=4):
        self.num_batches = int(100000/n_workers)
        self.mini_batch_size = mini_batch_size
        self.fixed_shape = fixed_shape
        self.local_normalize = local_normalize
        self.real_world_pos = real_world_pos
        self.series_metadata = pd.read_pickle('/cbica/home/gangarav/projects/rsna_lumbar/well_formed_axials_with_metadata.pkl')
    
    def __len__(self):
        return self.num_batches

    def __iter__(self):
        for _ in range(self.num_batches):
            z, x, y = self.fixed_shape
            batch = []

            for _ in range(self.mini_batch_size):

                zarr_ref = None
                enough_frames = False
                while not enough_frames:
                    # choose a random row from series_metadata
                    row = self.series_metadata.sample().iloc[0]
                    series_path = f"/cbica/home/gangarav/rsna24_preprocessed/{row['series']}.zarr"
                    enough_frames = self._enough_dims_in_series_shape(row["shape"], (z, x, y))

                    if not enough_frames:
                        continue

                    zarr_ref = self._get_zarr_reference(series_path)

                    slice_indices_1 = self._get_frame_indices_for_series_shape_and_slice_shape(row["shape"], (z, x, y))
                    slices_1 = self._get_frames_from_zarr_reference(zarr_ref, row["mean"], row["std"], slice_indices_1, local_normalize=self.local_normalize)
                    slices_1 = slices_1.squeeze(0)
                    
                if self.real_world_pos:
                    px_to_world = np.array([row["z_spacing"], row["x_spacing"], row["y_spacing"]])
                else:
                    px_to_world = np.array([1.0, 1.0, 1.0])

                batch.append((slices_1, px_to_world))

            yield tuple(np.stack(t) for t in zip(*batch))

    def _choose_three_numbers_sum_to_16(self):
        x = random.randint(7, 8)
        y = random.randint(7, 8)
        z = random.randint(0, 16 - x - y)
        b = 16 - x - y - z
        return 2**z, 2**x, 2**y, 2**b

    def _get_list_of_valid_studies(self):
        # Implement logic to get the list of valid studies
        pass

    def _get_zarr_reference(self, series_path):
        return zarr.open(series_path, mode='r')

    def _get_frames_from_zarr_reference(self, zarr, series_mean, series_std, slice_list=None, local_normalize=False):
        if slice_list is not None:
            slices = tuple(slice(start, end) for start, end in slice_list)
            z = zarr[slices]
        else:
            z = zarr[:]
        
        if local_normalize:
            try:
                std = np.std(z)
            except:
                std = 1.0
                print("stdev is 0, not dividing by stdev")
            z = (z - np.mean(z))/std
        else:
            z = (z - series_mean)/series_std

        return np.expand_dims(z, axis=0)

    def _get_frame_indices_for_series_shape_and_slice_shape(self, series_shape, slice_shape):
        slice_indices = []
        for axis, size in enumerate(series_shape):
            if size < slice_shape[axis]:
                return None
            else:
                index = random.randint(0, size - slice_shape[axis])
                slice_indices.append([index, index + slice_shape[axis]])
        return slice_indices

    def _enough_dims_in_series_shape(self, series_shape, slice_shape):
        for axis, size in enumerate(series_shape):
            if size < slice_shape[axis]:
                return False
        return True

###############


def main(args):
    misc.init_distributed_mode(args)

    print('job dir: {}'.format(os.path.dirname(os.path.realpath(__file__))))
    print("{}".format(args).replace(', ', ',\n'))

    device = torch.device(args.device)

    # fix the seed for reproducibility
    seed = args.seed + misc.get_rank()
    torch.manual_seed(seed)
    np.random.seed(seed)

    cudnn.benchmark = True
    
    handle_flash_attn(args)

    # simple augmentation
    # transform_train = transforms.Compose([
    #         transforms.RandomResizedCrop(args.input_size, scale=(0.2, 1.0), interpolation=3),  # 3 is bicubic
    #         transforms.RandomHorizontalFlip(),
    #         transforms.ToTensor(),
    #         transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])])
    # dataset_train = datasets.ImageFolder(os.path.join(args.data_path, 'train'), transform=transform_train)
    # print(dataset_train)
    dataset_train = TrainDataset(
        mini_batch_size=args.batch_size,
        fixed_shape=(1, 128, 128),
        local_normalize=True,
        real_world_pos=False,
        n_workers=args.num_workers
    )


    # if args.distributed:
    #     num_tasks = misc.get_world_size()
    #     global_rank = misc.get_rank()
    #     sampler_train = torch.utils.data.DistributedSampler(
    #         dataset_train, num_replicas=num_tasks, rank=global_rank, shuffle=True
    #     )
    #     print("Sampler_train = %s" % str(sampler_train))
    # else:
    #     sampler_train = torch.utils.data.RandomSampler(dataset_train)

    if args.log_dir is not None:
        os.makedirs(args.log_dir, exist_ok=True)
        log_writer = SummaryWriter(log_dir=args.log_dir)
    else:
        log_writer = None

    dataloader_cls = MultiEpochsDataLoader if args.multi_epochs_dataloader else torch.utils.data.DataLoader


    data_loader_train = DataLoader(dataset_train, batch_size=None, num_workers=4)
    


    # data_loader_train = dataloader_cls(
    #     dataset_train, sampler=sampler_train,
    #     batch_size=args.batch_size,
    #     num_workers=args.num_workers,
    #     pin_memory=args.pin_mem,
    #     drop_last=True,
    # )
    
    # define the model
    if args.cross_mae:
        model = models_cross.__dict__[args.model](
            norm_pix_loss=args.norm_pix_loss, 
            weight_fm=args.weight_fm, 
            decoder_depth=args.decoder_depth, 
            use_fm=args.use_fm,
            use_input=args.use_input,
            self_attn=args.self_attn,
            img_size=args.input_size,
        )
    else:
        model = models_mae.__dict__[args.model](
            norm_pix_loss=args.norm_pix_loss, 
            decoder_depth=args.decoder_depth,
        )

    model.to(device)

    model_without_ddp = model
    print("Model = %s" % str(model_without_ddp))

    eff_batch_size = args.batch_size * args.accum_iter * misc.get_world_size()

    if args.lr is None:  # only base_lr is specified
        base_ratio = args.kept_mask_ratio / args.mask_ratio # base ratio for MAE
        if args.no_lr_scale:
            scale_kmr = 1
        elif args.inverse_lr:
            scale_kmr = 1 / base_ratio
        else:
            scale_kmr = base_ratio
        args.lr = scale_kmr * args.blr * eff_batch_size / 256

    print("base lr: %.2e" % (args.lr * 256 / eff_batch_size))
    print("actual lr: %.2e" % args.lr)

    print("accumulate grad iterations: %d" % args.accum_iter)
    print("effective batch size: %d" % eff_batch_size)

    if args.distributed:
        model = torch.nn.parallel.DistributedDataParallel(
            model, device_ids=[args.gpu], 
            find_unused_parameters=args.find_unused_parameters
        )
        model_without_ddp = model.module
    
    # following timm: set wd as 0 for bias and norm layers
    param_groups = optim_factory.param_groups_weight_decay(model_without_ddp, args.weight_decay)
    optimizer = torch.optim.AdamW(param_groups, lr=args.lr, betas=(0.9, 0.95))
    print(optimizer)
    loss_scaler = NativeScaler()

    misc.load_model(args=args, model_without_ddp=model_without_ddp, optimizer=optimizer, loss_scaler=loss_scaler)

    print(f"Start training for {args.epochs} epochs")
    start_time = time.time()
    for epoch in range(args.start_epoch, args.epochs):
        if args.distributed:
            data_loader_train.sampler.set_epoch(epoch)
        train_stats = train_one_epoch(
            model, data_loader_train,
            optimizer, device, epoch, loss_scaler,
            log_writer=log_writer,
            args=args
        )
        if args.output_dir:
            if epoch % 200 == 0:
                misc.save_model(
                    args=args, model=model, model_without_ddp=model_without_ddp, optimizer=optimizer,
                    loss_scaler=loss_scaler, epoch=epoch, save_latest_model_only=False)
            elif epoch % 20 == 0:
                misc.save_model(
                    args=args, model=model, model_without_ddp=model_without_ddp, optimizer=optimizer,
                    loss_scaler=loss_scaler, epoch=epoch, save_latest_model_only=True)
            if epoch + 1 == args.epochs:
                misc.save_model(
                    args=args, model=model, model_without_ddp=model_without_ddp, optimizer=optimizer,
                    loss_scaler=loss_scaler, epoch=epoch, save_latest_model_only=True)

        log_stats = {**{f'train_{k}': v for k, v in train_stats.items()},
                        'epoch': epoch,}

        if args.output_dir and misc.is_main_process():
            if log_writer is not None:
                log_writer.flush()
            with open(os.path.join(args.output_dir, "log.txt"), mode="a", encoding="utf-8") as f:
                f.write(json.dumps(log_stats) + "\n")

    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    print('Training time {}'.format(total_time_str))


if __name__ == '__main__':
    args = get_args_parser()
    args = args.parse_args()
    assert args.kept_mask_ratio <= args.mask_ratio, "Cannot reconstruct more than what is masked"
    if args.log_dir is None:
        args.log_dir = args.output_dir
    if args.output_dir:
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    main(args)
