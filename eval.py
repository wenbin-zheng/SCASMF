#coding=utf-8
import argparse
import logging
import os
import random
import time
from collections import OrderedDict
import csv
import numpy as np
import torch
import torch.backends.cudnn as cudnn
import torch.nn.functional as F
import torch.optim
from data_utils import init_fn
from datasets_nii import (Brats_loadall_nii, Brats_loadall_test_nii,
                               Brats_loadall_val_nii)
from transforms import *
# from visualizer import get_local
# get_local.activate()
import  SCATrans
# from predict import AverageMeter, test_softmax, test_softmax_visualize
from predict import AverageMeter, test_softmax, test_dice_hd95_softmax
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
import Parser, criterions
from lr_scheduler import LR_Scheduler, MultiEpochsDataLoader, record_loss
from parser import setup
from visualize import visualize_heads

parser = argparse.ArgumentParser()

parser.add_argument('--model', default='SCATrans', type=str)
parser.add_argument('-batch_size', '--batch_size', default=1, type=int, help='Batch size')
parser.add_argument('--lr', default=1e-4, type=float)
parser.add_argument('--weight_decay', default=1e-4, type=float)
parser.add_argument('--num_epochs', default=1000, type=int)
parser.add_argument('--iter_per_epoch', default=150, type=int)
parser.add_argument('--dataname', default='/BraTS/MICCAI_BraTS_2018_Data_Training', type=str)
parser.add_argument('--datapath', default='/BraTS/BRATS2018_Training_none_npy', type=str)
parser.add_argument('--savepath', default='/model/bra18eval/', type=str)
parser.add_argument('--resume', default=None, type=str)
parser.add_argument('--pretrain', default=None, type=str)
parser.add_argument('--region_fusion_start_epoch', default=0, type=int)
parser.add_argument('--seed', default=1037, type=int)
parser.add_argument('--needvalid', default=False, type=bool)
parser.add_argument('--csvpath', default='/csv/', type=str)
path = os.path.dirname(__file__)

## parse arguments
args = parser.parse_args()
setup(args, 'testing')
args.train_transforms = 'Compose([RandCrop3D((80,80,80)), RandomRotion(10), RandomIntensityChange((0.1,0.1)), RandomFlip(0), NumpyType((np.float32, np.int64)),])'
args.test_transforms = 'Compose([NumpyType((np.float32, np.int64)),])'

ckpts = args.savepath
os.makedirs(ckpts, exist_ok=True)

csvpath = args.csvpath
os.makedirs(csvpath, exist_ok=True)

###tensorboard writer
writer = SummaryWriter(os.path.join(args.savepath, 'summary'))

###modality missing mask
masks_test = [[False, False, False, True], [False, True, False, False], [False, False, True, False], [True, False, False, False],
         [False, True, False, True], [False, True, True, False], [True, False, True, False], [False, False, True, True], [True, False, False, True], [True, True, False, False],
         [True, True, True, False], [True, False, True, True], [True, True, False, True], [False, True, True, True],
         [True, True, True, True]]
masks_torch = torch.from_numpy(np.array(masks_test))
mask_name = ['t2', 't1c', 't1', 'flair',
            't1cet2', 't1cet1', 'flairt1', 't1t2', 'flairt2', 'flairt1ce',
            'flairt1cet1', 'flairt1t2', 'flairt1cet2', 't1cet1t2',
            'flairt1cet1t2']
print (masks_torch.int())

# masks_valid = [[False, False, True, False],
#             [False, True, True, False],
#             [True, True, False, True],
#             [True, True, True, True]]
masks_valid = [[False, False, False, True], [False, True, False, False], [False, False, True, False], [True, False, False, False],
         [False, True, False, True], [False, True, True, False], [True, False, True, False], [False, False, True, True], [True, False, False, True], [True, True, False, False],
         [True, True, True, False], [True, False, True, True], [True, True, False, True], [False, True, True, True],
         [True, True, True, True]]
# t1,t1cet1,flairticet2,flairt1cet1t2
masks_valid_torch = torch.from_numpy(np.array(masks_valid))
masks_valid_array = np.array(masks_valid)
masks_all = [True, True, True, True]
masks_all_torch = torch.from_numpy(np.array(masks_all))
# mask_name_valid = ['t1',
#                 't1cet1',
#                 'flairt1cet2',
#                 'flairt1cet1t2']
mask_name_valid = ['t2', 't1c', 't1', 'flair',
            't1cet2', 't1cet1', 'flairt1', 't1t2', 'flairt2', 'flairt1ce',
            'flairt1cet1', 'flairt1t2', 'flairt1cet2', 't1cet1t2',
            'flairt1cet1t2']
print (masks_valid_torch.int())

def main():
    ##########setting seed
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    random.seed(args.seed)
    np.random.seed(args.seed)
    cudnn.benchmark = True
    cudnn.deterministic = True

    ##########setting models
    if args.dataname in ['/BraTS/BRATS2021_Training_Data', '/BraTS/BraTS2020_Training_Data', '/BraTS/MICCAI_BraTS_2018_Data_Training']:
        num_cls = 4
    else:
        print ('dataset is error')
        exit(0)

    if args.model == 'fusiontrans':
        model = SCATrans.Model(num_cls=num_cls)
    # elif args.model == 'rfnet':
    #     model = rfnet.Model(num_cls=num_cls)
    # elif args.model == 'mmformer':
    #     model = mmformer.Model(num_cls=num_cls)



    print (model)
    model = torch.nn.DataParallel(model).cuda()

    ##########Setting learning schedule and optimizer
    lr_schedule = LR_Scheduler(args.lr, args.num_epochs)
    train_params = [{'params': model.parameters(), 'lr': args.lr, 'weight_decay':args.weight_decay}]
    optimizer = torch.optim.Adam(train_params,  betas=(0.9, 0.999), eps=1e-08, amsgrad=True)

    ##########Setting data
        ####BRATS2020
    if args.dataname == '/SCATrans/BraTS/BraTS2020_Training_Data':
        train_file = '/SCATrans/BraTS/BRATS2020_Training_none_npy/train.txt'
        test_file = '/SCATrans/BraTS/BRATS2020_Training_none_npy/test.txt'
        # valid_file = '/BraTS/BRATS2020_Training_none_npy/val.txt'
    elif args.dataname == '/SCATrans/BraTS/BRATS2021_Training_Data':
        ####BRATS2021
        train_file = '/SCATrans/BraTS/BRATS2021_Training_none_npy/train.txt'
        test_file = '/SCATrans/BraTS/BRATS2021_Training_none_npy/test.txt'
        # valid_file = '/BraTS/BRATS2021_Training_none_npy/val.txt'
    elif args.dataname == '/SCATrans/BraTS/MICCAI_BraTS_2018_Data_Training':
        ####BRATS2018 contains three splits (1,2,3)
        train_file = '/SCATrans/BraTS/BRATS2018_Training_none_npy/train1.txt'
        test_file = '/SCATrans/BraTS/BRATS2018_Training_none_npy/test1.txt'
        # valid_file = '/BraTS/BRATS2018_Training_none_npy/val.txt'

    logging.info(str(args))
    # train_set = Brats_loadall_nii(transforms=args.train_transforms, root=args.datapath, num_cls=num_cls, train_file=train_file)
    test_set = Brats_loadall_test_nii(transforms=args.test_transforms, root=args.datapath, test_file=test_file)
    # valid_set = Brats_loadall_val_nii(transforms=args.train_transforms, root=args.datapath, num_cls=num_cls, train_file=valid_file)
    # train_loader = MultiEpochsDataLoader(
    #     dataset=train_set,
    #     batch_size=args.batch_size,
    #     num_workers=8,
    #     pin_memory=True,
    #     shuffle=True,
    #     worker_init_fn=init_fn)
    test_loader = MultiEpochsDataLoader(
        dataset=test_set,
        batch_size=1,
        shuffle=False,
        num_workers=0,
        pin_memory=True)
    # valid_loader = MultiEpochsDataLoader(
    #     dataset=valid_set,
    #     batch_size=args.batch_size,
    #     num_workers=8,
    #     pin_memory=True,
    #     shuffle=True,
    #     worker_init_fn=init_fn)


    #########Evaluate
    ##########Evaluate last epoch
    # if args.resume is not None:
    #     checkpoint = torch.load(args.resume)
    #     logging.info('last epoch: {}'.format(checkpoint['epoch']+1))
    #     model.load_state_dict(checkpoint['state_dict'])
    #
    # test_dice_score = AverageMeter()
    # test_hd95_score = AverageMeter()
    # csv_name = os.path.join(csvpath, '{}_eval.csv'.format(args.model))
    # with torch.no_grad():
    #     logging.info('###########test last epoch model###########')
    #     file = open(csv_name, "a+")
    #     csv_writer = csv.writer(file)
    #     csv_writer.writerow(
    #         ['WT Dice', 'TC Dice', 'ET Dice', 'ETPro Dice', 'WT HD95', 'TC HD95', 'ET HD95' 'ETPro HD95'])
    #     file.close()
    #     for i, mask in enumerate(masks_test[::-1]):
    #         logging.info('{}'.format(mask_name[::-1][i]))
    #         file = open(csv_name, "a+")
    #         csv_writer = csv.writer(file)
    #         csv_writer.writerow([mask_name[::-1][i]])
    #         file.close()
    #         dice_score, hd95_score = test_dice_hd95_softmax(
    #             test_loader,
    #             model,
    #             dataname=args.dataname,
    #             feature_mask=mask,
    #             mask_name=mask_name[::-1][i],
    #             csv_name=csv_name,
    #         )
    #         test_dice_score.update(dice_score)
    #         test_hd95_score.update(hd95_score)
    #
    #     logging.info('Avg Dice scores: {}'.format(test_dice_score.avg))
    #     logging.info('Avg HD95 scores: {}'.format(test_hd95_score.avg))
    #     exit(0)



    # #########Visualize Evaluate
    # if args.resume is not None:
    #     checkpoint = torch.load(args.resume)
    #     logging.info('best epoch: {}'.format(checkpoint['epoch']+1))
    #     model.load_state_dict(checkpoint['state_dict'])
    #     test_score = AverageMeter()
    #     writer_visualize = SummaryWriter(log_dir="visualize/result")
    #     visualize_step = 0
    #     with torch.no_grad():
    #         logging.info('###########visualize model###########')
    #         for i, mask in enumerate(masks_test[::-1]):
    #             logging.info('{}'.format(mask_name[::-1][i]))
    #             dice_score, visualize_step = test_softmax_visualize(
    #                             test_loader,
    #                             model,
    #                             dataname = args.dataname,
    #                             feature_mask = mask,
    #                             mask_name = mask_name[::-1][i],
    #                             writer = writer_visualize,
    #                             step = visualize_step)
    #             test_score.update(dice_score)
    #         logging.info('Avg scores: {}'.format(test_score.avg))
    #         exit(0)


    if args.resume is not None:
        checkpoint = torch.load(args.resume)
        pretrained_dict = checkpoint['state_dict']
        model_dict = model.state_dict()
        pretrained_dict = {k: v for k, v in pretrained_dict.items() if k in model_dict}
        logging.info('pretrained_dict: {}'.format(pretrained_dict))
        model_dict.update(pretrained_dict)
        model.load_state_dict(model_dict)
        logging.info('load ok')

##########Test the last epoch model
    writer_visualize = SummaryWriter(log_dir="visualize/result")
    visualize_step = 0
    test_dice_score = AverageMeter()
    test_hd95_score = AverageMeter()
    csv_name = os.path.join(csvpath, '{}.csv'.format(args.model))
    with torch.no_grad():
        logging.info('###########test last epoch model###########')
        file = open(csv_name, "a+")
        csv_writer = csv.writer(file)
        csv_writer.writerow(['WT Dice', 'TC Dice', 'ET Dice','ETPro Dice', 'WT HD95', 'TC HD95', 'ET HD95' 'ETPro HD95'])
        file.close()
        for i, mask in enumerate(masks_test[::-1]):
            logging.info('{}'.format(mask_name[::-1][i]))
            file = open(csv_name, "a+")
            csv_writer = csv.writer(file)
            csv_writer.writerow([mask_name[::-1][i]])
            file.close()
            dice_score, hd95_score = test_dice_hd95_softmax(
                            test_loader,
                            model,
                            dataname = args.dataname,
                            feature_mask = mask,
                            mask_name = mask_name[::-1][i],
                            csv_name = csv_name,
                            )
            test_dice_score.update(dice_score)
            test_hd95_score.update(hd95_score)

        logging.info('Avg Dice scores: {}'.format(test_dice_score.avg))
        logging.info('Avg HD95 scores: {}'.format(test_hd95_score.avg))


    # ##########Test the best epoch model
    # file_name = os.path.join(ckpts, 'model_best.pth')
    # checkpoint = torch.load(file_name)
    # logging.info('best epoch: {}'.format(checkpoint['epoch']+1))
    # model.load_state_dict(checkpoint['state_dict'])
    # test_best_score = AverageMeter()
    # with torch.no_grad():
    #     logging.info('###########test validate best model###########')
    #     for i, mask in enumerate(masks_test[::-1]):
    #         logging.info('{}'.format(mask_name[::-1][i]))
    #         dice_best_score = test_softmax(
    #                         test_loader,
    #                         model,
    #                         dataname = args.dataname,
    #                         feature_mask = mask,
    #                         mask_name = mask_name[::-1][i])
    #         test_best_score.update(dice_best_score)
    #     logging.info('Avg scores: {}'.format(test_best_score.avg))


if __name__ == '__main__':
    main()
