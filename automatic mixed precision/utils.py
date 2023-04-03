import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from sklearn.model_selection import train_test_split

import torch
import torch.optim as optim
import torch.optim.lr_scheduler as lrs
from torch.utils.data.sampler import SubsetRandomSampler
import torchvision
from torchvision import transforms as T

from network.resnet import *
from learning.lr_scheduler import GradualWarmupScheduler

def get_model(args, shape, num_classes):
    model = eval(args.model)(
        shape,
        num_classes,
        checkpoint_dir=args.checkpoint_dir,
        checkpoint_name=args.checkpoint_name,
        pretrained=args.pretrained,
        pretrained_path=args.pretrained_path,
        norm=args.norm,
    )#.cuda(args.gpu)
    return model


def make_optimizer(args, model):
    trainable = filter(lambda x: x.requires_grad, model.parameters())

    if args.optimizer == 'SGD':
        optimizer_function = optim.SGD
        kwargs = {'momentum': 0.9}
    elif args.optimizer == 'ADAM':
        optimizer_function = optim.Adam
        kwargs = {
            'betas': (0.9, 0.999),
            'eps': 1e-08
        }
    else:
        raise NameError('Not Supportes Optimizer')

    kwargs['lr'] = args.learning_rate
    kwargs['weight_decay'] = args.weight_decay

    return optimizer_function(trainable, **kwargs)


def make_scheduler(args, optimizer):
    if args.decay_type == 'step':
        scheduler = lrs.MultiStepLR(
            optimizer,
            milestones=[30, 60, 90],
            gamma=0.1
        )
    elif args.decay_type == 'step_warmup':
        scheduler = lrs.MultiStepLR(
            optimizer,
            milestones=[30, 60, 90],
            gamma=0.1
        )
        scheduler = GradualWarmupScheduler(
            optimizer,
            multiplier=1,
            total_epoch=5,
            after_scheduler=scheduler
        )
    elif args.decay_type == 'cosine_warmup':
        cosine_scheduler = lrs.CosineAnnealingLR(
            optimizer,
            T_max=args.epochs
        )
        scheduler = GradualWarmupScheduler(
            optimizer,
            multiplier=1,
            total_epoch=args.epochs//10,
            after_scheduler=cosine_scheduler
        )
    else:
        raise Exception('unknown lr scheduler: {}'.format(args.decay_type))
    
    return scheduler

def make_dataloader(args):
    
    train_trans = T.Compose([
        T.Resize((256, 256)),
        T.RandomHorizontalFlip(),
        T.ToTensor(),
    ])
    
    valid_trans = T.Compose([
        T.Resize((256, 256)),
        T.ToTensor(),
        ])
    
    test_trans = T.Compose([
        T.Resize((256, 256)),
        T.ToTensor(),
        ])

    trainset = torchvision.datasets.ImageFolder(root="data/seg_train/seg_train", transform=train_trans)
    validset = torchvision.datasets.ImageFolder(root="data/seg_train/seg_train", transform=valid_trans)
    testset = torchvision.datasets.ImageFolder(root="data/seg_test/seg_test", transform=test_trans)

    np.random.seed(args.seed)
    targets = trainset.targets
    train_idx, valid_idx = train_test_split(np.arange(len(targets)), test_size=0.2, shuffle=True, stratify=targets)
    train_sampler = SubsetRandomSampler(train_idx)
    valid_sampler = SubsetRandomSampler(valid_idx)

    train_loader = torch.utils.data.DataLoader(
        trainset, batch_size=args.batch_size, sampler=train_sampler, num_workers=args.num_workers
    )

    valid_loader = torch.utils.data.DataLoader(
        validset, batch_size=args.batch_size, sampler=valid_sampler, num_workers=args.num_workers
    )

    test_loader = torch.utils.data.DataLoader(
        testset, batch_size=args.batch_size, shuffle=False, num_workers=args.num_workers
    )

    return train_loader, valid_loader, test_loader


def plot_learning_curves(metrics, cur_epoch, args):
    x = np.arange(cur_epoch+1)
    fig, ax1 = plt.subplots()
    ax1.set_xlabel('epochs')
    ax1.set_ylabel('loss')
    ln1 = ax1.plot(x, metrics['train_loss'], color='tab:red')
    ln2 = ax1.plot(x, metrics['val_loss'], color='tab:red', linestyle='dashed')
    ax1.grid()
    ax2 = ax1.twinx()
    ax2.set_ylabel('accuracy')
    ln3 = ax2.plot(x, metrics['train_acc'], color='tab:blue')
    ln4 = ax2.plot(x, metrics['val_acc'], color='tab:blue', linestyle='dashed')
    lns = ln1+ln2+ln3+ln4
    plt.legend(lns, ['Train loss', 'Validation loss', 'Train accuracy','Validation accuracy'])
    plt.tight_layout()
    plt.savefig('{}/{}/learning_curve.png'.format(args.checkpoint_dir, args.checkpoint_name), bbox_inches='tight')
    plt.close('all')

class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0
        self.max = 0
        self.min = 1e5

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count
        if val > self.max:
            self.max = val
        if val < self.min:
            self.min = val

def accuracy(output, target, topk=(1,)):
    """Computes the precision@k for the specified values of k"""
    maxk = max(topk)
    batch_size = target.size(0)

    _, pred = output.topk(maxk, 1, True, True)
    pred = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))

    res = []
    for k in topk:
        correct_k = correct[:k].view(-1).float().sum(0)
        res.append(correct_k.mul_(100.0 / batch_size))
    return res