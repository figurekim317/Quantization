import argparse
import os
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))

import random
import shutil
import time
import warnings
import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim
import torch.utils.data
import torch.utils.data.distributed
import torchvision.models as models
import numpy as np
from utils.data import get_dataset
from utils.preprocess import get_transform
from quantization.quantizer import ModelQuantizer, OptimizerBridge
from pathlib import Path
from utils.mllog import MLlogger
from utils.meters import AverageMeter, ProgressMeter, accuracy
from torch.optim.lr_scheduler import StepLR
from models.resnet import resnet as custom_resnet
from models.inception import inception_v3 as custom_inception
from quantization.qat.module_wrapper import ActivationModuleWrapper, ParameterModuleWrapper
from utils.misc import normalize_module_name, arch2depth

home = str(Path.home())

model_names = sorted(name for name in models.__dict__
    if name.islower() and not name.startswith("__")
    and callable(models.__dict__[name]))

parser = argparse.ArgumentParser(description='PyTorch ImageNet Training')
parser.add_argument('-a', '--arch', metavar='ARCH', default='resnet18',
                    choices=model_names,
                    help='model architecture: ' +
                        ' | '.join(model_names) +
                        ' (default: resnet18)')
parser.add_argument('--dataset', metavar='DATASET', default='imagenet',
                    help='dataset name or folder')
parser.add_argument('--datapath', metavar='DATAPATH', type=str, default=None,
                    help='dataset folder')
parser.add_argument('-j', '--workers', default=25, type=int, metavar='N',
                    help='number of data loading workers (default: 4)')
parser.add_argument('-ep', '--epochs', default=90, type=int, metavar='N',
                    help='number of total epochs to run')
parser.add_argument('--start-epoch', default=0, type=int, metavar='N',
                    help='manual epoch number (useful on restarts)')
parser.add_argument('-b', '--batch-size', default=256, type=int,
                    metavar='N',
                    help='mini-batch size (default: 256), this is the total '
                         'batch size of all GPUs on the current node when '
                         'using Data Parallel or Distributed Data Parallel')
parser.add_argument('--lr', '--learning-rate', default=0.1, type=float,
                    metavar='LR', help='initial learning rate', dest='lr')
parser.add_argument('--lr_step', '--learning-rate-step', default=30, type=int,
                    help='learning rate reduction step')
parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                    help='momentum')
parser.add_argument('-wd', '--weight-decay', default=1e-4, type=float,
                    metavar='W', help='weight decay (default: 1e-4)',
                    dest='weight_decay')
parser.add_argument('-p', '--print-freq', default=10, type=int,
                    metavar='N', help='print frequency (default: 10)')
parser.add_argument('--resume', default='', type=str, metavar='PATH',
                    help='path to latest checkpoint (default: none)')
parser.add_argument('-e', '--evaluate', dest='evaluate', action='store_true',
                    help='evaluate model on validation set')
parser.add_argument('--pretrained', dest='pretrained', action='store_true', help='use pre-trained model')
parser.add_argument('--custom_resnet', action='store_true', help='use custom resnet implementation')
parser.add_argument('--custom_inception', action='store_true', help='use custom inception implementation')
parser.add_argument('--seed', default=0, type=int,
                    help='seed for initializing training. ')
parser.add_argument('--gpu_ids', default=[0], type=int, nargs='+',
                    help='GPU ids to use (e.g 0 1 2 3)')
parser.add_argument('--lr_freeze', action='store_true', help='Freeze learning rate', default=False)
parser.add_argument('--bn_folding', '-bnf', action='store_true', help='Apply Batch Norm folding', default=False)
parser.add_argument('--log_stats', '-ls', action='store_true', help='Log statistics', default=False)

parser.add_argument('--quantize', '-q', action='store_true', help='Enable quantization', default=False)
parser.add_argument('--experiment', '-exp', help='Name of the experiment', default='default')
parser.add_argument('--bit_weights', '-bw', type=int, help='Number of bits for weights', default=None)
parser.add_argument('--bit_act', '-ba', type=int, help='Number of bits for activations', default=None)
parser.add_argument('--model_freeze', '-mf', action='store_true', help='Freeze model parameters', default=False)
parser.add_argument('--temperature', '-t', type=float, help='Temperature parameter for sigmoid quantization', default=None)
parser.add_argument('--qtype', default='None', help='Type of quantization method')
parser.add_argument('--bcorr_w', '-bcw', action='store_true', help='Bias correction for weights', default=False)

best_acc1 = 0


def main():
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)
        torch.manual_seed(args.seed)
        cudnn.deterministic = True
        warnings.warn('You have chosen to seed training. '
                      'This will turn on the CUDNN deterministic setting, '
                      'which can slow down your training considerably! '
                      'You may see unexpected behavior when restarting '
                      'from checkpoints.')

    if args.seed is not None:
        random.seed(args.seed)
        np.random.seed(args.seed)
        torch.manual_seed(args.seed)
        torch.cuda.manual_seed_all(args.seed)
        cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False

    with MLlogger(os.path.join(home, 'mxt-sim/mllog_runs'), args.experiment, args,
                  name_args=[args.arch, args.dataset, "W{}A{}".format(args.bit_weights, args.bit_act)]) as ml_logger:
        main_worker(args, ml_logger)


def main_worker(args, ml_logger):
    global best_acc1

    if args.gpu_ids is not None:
        print("Use GPU: {} for training".format(args.gpu_ids))

    if args.log_stats:
        from utils.stats_trucker import StatsTrucker as ST
        ST("W{}A{}".format(args.bit_weights, args.bit_act))

    if 'resnet' in args.arch and args.custom_resnet:
        model = custom_resnet(arch=args.arch, pretrained=args.pretrained, depth=arch2depth(args.arch), dataset=args.dataset)
    elif 'inception_v3' in args.arch and args.custom_inception:
        model = custom_inception(pretrained=args.pretrained)
    else:
        print("=> using pre-trained model '{}'".format(args.arch))
        model = models.__dict__[args.arch](pretrained=args.pretrained)

    device = torch.device('cuda:{}'.format(args.gpu_ids[0]))
    cudnn.benchmark = True

    torch.cuda.set_device(args.gpu_ids[0])
    model = model.to(device)

    # optionally resume from a checkpoint
    if args.resume:
        if os.path.isfile(args.resume):
            print("=> loading checkpoint '{}'".format(args.resume))
            checkpoint = torch.load(args.resume, device)
            args.start_epoch = checkpoint['epoch']
            # best_acc1 = checkpoint['best_acc1']
            # best_acc1 may be from a checkpoint from a different GPU
            # best_acc1 = best_acc1.to(device)
            checkpoint['state_dict'] = {normalize_module_name(k): v for k, v in checkpoint['state_dict'].items()}
            model.load_state_dict(checkpoint['state_dict'], strict=False)
            # optimizer.load_state_dict(checkpoint['optimizer'])
            print("=> loaded checkpoint '{}' (epoch {})"
                  .format(args.resume, checkpoint['epoch']))
        else:
            print("=> no checkpoint found at '{}'".format(args.resume))

    if len(args.gpu_ids) > 1:
        # DataParallel will divide and allocate batch_size to all available GPUs
        if args.arch.startswith('alexnet') or args.arch.startswith('vgg'):
            model.features = torch.nn.DataParallel(model.features, args.gpu_ids)
        else:
            model = torch.nn.DataParallel(model, args.gpu_ids)

    default_transform = {
        'train': get_transform(args.dataset, augment=True),
        'eval': get_transform(args.dataset, augment=False)
    }

    val_data = get_dataset(args.dataset, 'val', default_transform['eval'])
    val_loader = torch.utils.data.DataLoader(
        val_data,
        batch_size=args.batch_size, shuffle=True,
        num_workers=args.workers, pin_memory=True)

    # define loss function (criterion) and optimizer
    criterion = nn.CrossEntropyLoss().to(device)

    train_data = get_dataset(args.dataset, 'train', default_transform['train'])
    train_loader = torch.utils.data.DataLoader(
        train_data,
        batch_size=args.batch_size, shuffle=True,
        num_workers=args.workers, pin_memory=True, drop_last=True)

    # TODO: replace this call by initialization on small subset of training data
    # TODO: enable for activations
    # validate(val_loader, model, criterion, args, device)

    optimizer = torch.optim.SGD(model.parameters(), args.lr, momentum=args.momentum, weight_decay=args.weight_decay)
    # optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
    lr_scheduler = StepLR(optimizer, step_size=args.lr_step, gamma=0.1)

    mq = None
    if args.quantize:
        if args.bn_folding:
            print("Applying batch-norm folding ahead of post-training quantization")
            from utils.absorb_bn import search_absorbe_bn
            search_absorbe_bn(model)

        all_convs = [n for n, m in model.named_modules() if isinstance(m, nn.Conv2d)]
        # all_convs = [l for l in all_convs if 'downsample' not in l]
        all_relu = [n for n, m in model.named_modules() if isinstance(m, nn.ReLU)]
        all_relu6 = [n for n, m in model.named_modules() if isinstance(m, nn.ReLU6)]
        layers = all_relu[1:-1] + all_relu6[1:-1] + all_convs[1:]
        replacement_factory = {nn.ReLU: ActivationModuleWrapper,
                               nn.ReLU6: ActivationModuleWrapper,
                               nn.Conv2d: ParameterModuleWrapper}
        mq = ModelQuantizer(model, args, layers, replacement_factory,
                            OptimizerBridge(optimizer, settings={'algo': 'SGD', 'dataset': args.dataset}))

        if args.resume:
            # Load quantization parameters from state dict
            mq.load_state_dict(checkpoint['state_dict'])

        mq.log_quantizer_state(ml_logger, -1)

        if args.model_freeze:
            mq.freeze()

    if args.evaluate:
        if args.log_stats:
            mean = []
            var = []
            skew = []
            kurt = []
            for n, p in model.named_parameters():
                if n.replace('.weight', '') in all_convs[1:]:
                    mu = p.mean()
                    std = p.std()
                    mean.append((n, mu.item()))
                    var.append((n, (std**2).item()))
                    skew.append((n, torch.mean(((p - mu)/std)**3).item()))
                    kurt.append((n, torch.mean(((p - mu)/std)**4).item()))
            for i in range(len(mean)):
                ml_logger.log_metric(mean[i][0]+'.mean', mean[i][1])
                ml_logger.log_metric(var[i][0] + '.var', var[i][1])
                ml_logger.log_metric(skew[i][0] + '.skewness', skew[i][1])
                ml_logger.log_metric(kurt[i][0] + '.kurtosis', kurt[i][1])

            ml_logger.log_metric('weight_mean', np.mean([s[1] for s in mean]))
            ml_logger.log_metric('weight_var', np.mean([s[1] for s in var]))
            ml_logger.log_metric('weight_skewness', np.mean([s[1] for s in skew]))
            ml_logger.log_metric('weight_kurtosis', np.mean([s[1] for s in kurt]))


        acc = validate(val_loader, model, criterion, args, device)
        ml_logger.log_metric('Val Acc1', acc)
        if args.log_stats:
            stats = ST().get_stats()
            for s in stats:
                ml_logger.log_metric(s, np.mean(stats[s]))
        return

    # evaluate on validation set
    acc1 = validate(val_loader, model, criterion, args, device)
    ml_logger.log_metric('Val Acc1', acc1, -1)

    # evaluate with k-means quantization
    # if args.model_freeze:
        # with mq.disable():
        #     acc1_nq = validate(val_loader, model, criterion, args, device)
        #     ml_logger.log_metric('Val Acc1 fp32', acc1_nq, -1)


    for epoch in range(0, args.epochs):
        # train for one epoch
        train(train_loader, model, criterion, optimizer, epoch, args, device, ml_logger, val_loader, mq)

        if not args.lr_freeze:
            lr_scheduler.step()

        # evaluate on validation set
        acc1 = validate(val_loader, model, criterion, args, device)
        ml_logger.log_metric('Val Acc1', acc1,  step='auto')

        # evaluate with k-means quantization
        # if args.model_freeze:
            # with mq.quantization_method('kmeans'):
            #     acc1_kmeans = validate(val_loader, model, criterion, args, device)
            #     ml_logger.log_metric('Val Acc1 kmeans', acc1_kmeans, epoch)

            # with mq.disable():
            #     acc1_nq = validate(val_loader, model, criterion, args, device)
            #     ml_logger.log_metric('Val Acc1 fp32', acc1_nq,  step='auto')

        if args.quantize:
            mq.log_quantizer_state(ml_logger, epoch)

        # remember best acc@1 and save checkpoint
        is_best = acc1 > best_acc1
        best_acc1 = max(acc1, best_acc1)

        save_checkpoint({
            'epoch': epoch + 1,
            'arch': args.arch,
            'state_dict': model.state_dict() if len(args.gpu_ids) == 1 else model.module.state_dict(),
            'best_acc1': best_acc1,
            'optimizer': optimizer.state_dict(),
        }, is_best)


def train(train_loader, model, criterion, optimizer, epoch, args, device, ml_logger, val_loader, mq=None):
    batch_time = AverageMeter('Time', ':6.3f')
    data_time = AverageMeter('Data', ':6.3f')
    losses = AverageMeter('Loss', ':.4e')
    top1 = AverageMeter('Acc@1', ':6.2f')
    top5 = AverageMeter('Acc@5', ':6.2f')
    progress = ProgressMeter(len(train_loader), batch_time, data_time, losses, top1,
                             top5, prefix="Epoch: [{}]".format(epoch))

    # switch to train mode
    model.train()
    best_acc1 = -1
    end = time.time()
    for i, (images, target) in enumerate(train_loader):
        # measure data loading time
        data_time.update(time.time() - end)

        images = images.to(device, non_blocking=True)
        target = target.to(device, non_blocking=True)

        # compute output
        output = model(images)
        loss = criterion(output, target)

        # measure accuracy and record loss
        acc1, acc5 = accuracy(output, target, topk=(1, 5))
        losses.update(loss.item(), images.size(0))
        top1.update(acc1.item(), images.size(0))
        top5.update(acc5.item(), images.size(0))

        # compute gradient and do SGD step
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            progress.print(i)
            ml_logger.log_metric('Train Acc1', top1.avg,  step='auto', log_to_tfboard=False)
            ml_logger.log_metric('Train Loss', losses.avg,  step='auto', log_to_tfboard=False)


def validate(val_loader, model, criterion, args, device):
    batch_time = AverageMeter('Time', ':6.3f')
    losses = AverageMeter('Loss', ':.4e')
    top1 = AverageMeter('Acc@1', ':6.2f')
    top5 = AverageMeter('Acc@5', ':6.2f')
    progress = ProgressMeter(len(val_loader), batch_time, losses, top1, top5,
                             prefix='Test: ')

    # switch to evaluate mode
    model.eval()

    with torch.no_grad():
        end = time.time()
        for i, (images, target) in enumerate(val_loader):
            images = images.to(device, non_blocking=True)
            target = target.to(device, non_blocking=True)

            # compute output
            output = model(images)
            loss = criterion(output, target)

            # measure accuracy and record loss
            acc1, acc5 = accuracy(output, target, topk=(1, 5))
            losses.update(loss.item(), images.size(0))
            top1.update(acc1.item(), images.size(0))
            top5.update(acc5.item(), images.size(0))

            # measure elapsed time
            batch_time.update(time.time() - end)
            end = time.time()

            if i % args.print_freq == 0:
                progress.print(i)

        # TODO: this should also be done with the ProgressMeter
        print(' * Acc@1 {top1.avg:.3f} Acc@5 {top5.avg:.3f}'
              .format(top1=top1, top5=top5))

    return top1.avg


def save_checkpoint(state, is_best, filename='last_checkpoint.pth.tar'):
    ckpt_dir = os.path.join(home, 'mxt-sim', 'ckpt', state['arch'])
    if not os.path.exists(ckpt_dir):
        os.makedirs(ckpt_dir)
    ckpt_path = os.path.join(ckpt_dir, filename)
    torch.save(state, ckpt_path)
    if is_best:
        shutil.copyfile(ckpt_path, os.path.join(ckpt_dir, 'model_best.pth.tar'))


if __name__ == '__main__':
    main()
