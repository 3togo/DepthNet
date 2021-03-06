import argparse
import time
import csv
import os

import torch
import torch.backends.cudnn as cudnn
import torch.optim
import torch.utils.data
import torchvision.transforms as transforms
import co_transforms
import models
import datasets
from loss import depth_metric_reconstruction_loss as metric_loss
from terminal_logger import TermLogger
from tensorboardX import SummaryWriter

import util
from util import AverageMeter

import logging

logging.basicConfig(format='%(asctime)s,%(msecs)d %(levelname)-8s [%(filename)s:%(lineno)d] %(message)s',
    datefmt='%d-%m-%Y:%H:%M:%S',
    level=logging.DEBUG)
    
device_ids = [0,1,2,3,4,5]
    

parser = argparse.ArgumentParser(description='PyTorch DepthNet Training on Still Box dataset')
util.set_arguments(parser)

best_error = -1
n_iter = 0
device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")


def main():
    global args, best_error, viz
    args = util.set_params(parser)
    logging.info("[starting]"*10)
    train_writer = SummaryWriter(args.save_path/'train')
    val_writer = SummaryWriter(args.save_path/'val')
    output_writers = []
    if args.log_output:
        for i in range(3):
            output_writers.append(SummaryWriter(args.save_path/'val'/str(i)))
    torch.manual_seed(args.seed)

    # Data loading code
    mean = [0.5, 0.5, 0.5]
    std = [0.2, 0.2, 0.2]
    normalize = transforms.Normalize(mean=mean,
                                     std=std)
    input_transform = transforms.Compose([
        co_transforms.ArrayToTensor(),
        transforms.Normalize(mean=[0, 0, 0], std=[255, 255, 255]),
        normalize
    ])
    target_transform = transforms.Compose([
        co_transforms.Clip(0, 100),
        co_transforms.ArrayToTensor()
    ])
    co_transform = co_transforms.Compose([
        co_transforms.RandomVerticalFlip(),
        co_transforms.RandomHorizontalFlip()
    ])

    logging.info("=> fetching scenes in '{}'".format(args.data))
    train_set, val_set = datasets.still_box(
        args.data,
        transform=input_transform,
        target_transform=target_transform,
        co_transform=co_transform,
        split=args.split,
        seed=args.seed
    )
    logging.info('{} samples found, {} train scenes and {} validation samples '.format(len(val_set)+len(train_set),
                                                                                len(train_set),
                                                                                len(val_set)))
    train_loader = torch.utils.data.DataLoader(
        train_set, batch_size=args.batch_size, shuffle=True,
        num_workers=args.workers, pin_memory=True)
    val_loader = torch.utils.data.DataLoader(
        val_set, batch_size=args.batch_size,
        shuffle=False,
        num_workers=args.workers, pin_memory=True)
    if args.epoch_size == 0:
        args.epoch_size = len(train_loader)
    # create model
    if args.pretrained:
        data = torch.load(args.pretrained)
        assert(not data['with_confidence'])
        print("=> using pre-trained model '{}'".format(data['arch']))
        model = models.DepthNet(batch_norm=data['bn'], clamp=args.clamp, depth_activation=args.activation_function)
        model.load_state_dict(data['state_dict'])
    else:
        print("=> creating model '{}'".format(args.arch))
        model = models.DepthNet(batch_norm=args.bn, clamp=args.clamp, depth_activation=args.activation_function)
    model = model.to(device)
    logging.info("Model created")
    # if torch.cuda.device_count() > 1:
        # print("%"*100)
        # print("Let's use", torch.cuda.device_count(), "GPUs!")
        # # dim = 0 [30, xxx] -> [10, ...], [10, ...], [10, ...] on 3 GPUs
        # model = torch.nn.DataParallel(model, device_ids=device_ids)
 
    # if torch.cuda.is_available():
        # print("&"*100)
        # model.cuda()
        
    #model = torch.nn.DataParallel(model.cuda(1), device_ids=device_ids)
    cudnn.benchmark = True

    assert(args.solver in ['adam', 'sgd'])
    print('=> setting {} solver'.format(args.solver))
    if args.solver == 'adam':
        optimizer = torch.optim.Adam(model.parameters(), args.lr,
                                     betas=(args.momentum, args.beta),
                                     weight_decay=args.weight_decay)
    elif args.solver == 'sgd':
        optimizer = torch.optim.SGD(model.parameters(), args.lr,
                                    momentum=args.momentum,
                                    weight_decay=args.weight_decay,
                                    dampening=args.momentum)

    scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer,
                                                     milestones=[19,30,44,53],
                                                     gamma=0.3)
    logging.info("Optimizer created")
    
    with open(os.path.join(args.save_path, args.log_summary), 'w') as csvfile:
        writer = csv.writer(csvfile, delimiter='\t')
        writer.writerow(['train_loss', 'train_depth_error', 'normalized_train_depth_error', 'depth_error', 'normalized_depth_error'])

    with open(os.path.join(args.save_path, args.log_full), 'w') as csvfile:
        writer = csv.writer(csvfile, delimiter='\t')
        writer.writerow(['train_loss', 'train_depth_error'])

    term_logger = TermLogger(n_epochs=args.epochs, train_size=min(len(train_loader), args.epoch_size), test_size=len(val_loader))
    term_logger.epoch_bar.start()
    logging.info("Validate")
    if args.evaluate:
        depth_error, normalized = validate(val_loader, model, 0, term_logger, output_writers)
        term_logger.test_writer.write(' * Depth error : {:.3f}, normalized : {:.3f}'.format(depth_error, normalized))
        return
    logging.info("epoch loop for %d time"%args.epochs)
    for epoch in range(args.epochs):
        logging.info("<epoch>=%d :start"%epoch)
        term_logger.epoch_bar.update(epoch)
        #scheduler.module.step()
        scheduler.step()

        # train for one epoch
        logging.info("train for one epoch: start       ")
        term_logger.reset_train_bar()
        term_logger.train_bar.start()
        logging.info("it might take more than 3min     ")
        train_loss, train_error, train_normalized_error = train(train_loader, model, optimizer, args.epoch_size, term_logger, train_writer)
        logging.info("train for one epoch: done         ")
        
        term_logger.train_writer.write(' * Avg Loss : {:.3f}, Avg Depth error : {:.3f}, normalized : {:.3f}'
                                       .format(train_loss, train_error, train_normalized_error))
        train_writer.add_scalar('metric_error', train_error, epoch)
        train_writer.add_scalar('metric_normalized_error', train_normalized_error, epoch)

        # evaluate on validation set
        logging.info("evaluate on validation set")
        term_logger.reset_test_bar()
        term_logger.test_bar.start()
        depth_error, normalized = validate(val_loader, model, epoch, term_logger, output_writers)
        term_logger.test_writer.write(' * Depth error : {:.3f}, normalized : {:.3f}'.format(depth_error, normalized))
        val_writer.add_scalar('metric_error', depth_error, epoch)
        val_writer.add_scalar('metric_normalized_error', normalized, epoch)

        if best_error < 0:
            best_error = depth_error

        # remember lowest error and save checkpoint
        logging.info("remember lowest error and save checkpoint")
        is_best = depth_error < best_error
        best_error = min(depth_error, best_error)
        util.save_checkpoint(
            args.save_path, {
                'epoch': epoch + 1,
                'arch': args.arch,
                'state_dict': model.state_dict(),
                'best_error': best_error,
                'bn': args.bn,
                'with_confidence': False,
                'activation_function': args.activation_function,
                'clamp': args.clamp,
                'mean': mean,
                'std': std
            },
            is_best)

        with open(os.path.join(args.save_path, args.log_summary), 'a') as csvfile:
            writer = csv.writer(csvfile, delimiter='\t')
            writer.writerow([train_loss, train_error, depth_error])
        logging.info("epoch=%d done"%epoch)
    term_logger.epoch_bar.finish()


def train(train_loader, model, optimizer, epoch_size, term_logger, train_writer):
    global n_iter, args
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    depth2_metric_errors = AverageMeter()
    depth2_normalized_errors = AverageMeter()

    # switch to train mode
    logging.info("switch to train mode : start")
    model.train()
    logging.info("switch to train mode : end")
    end = time.time()

    for i, (input, target, _) in enumerate(train_loader):
        # measure data loading time
        logging.info("train_loader=%d"%i)
        data_time.update(time.time() - end)
        target = target.to(device)
        input = torch.cat(input,1).to(device)
        input, target = input.cuda(), target.cuda()
        # compute output
        if torch.cuda.device_count() > 1:
            logging.info("%s %s"%("*"*100,"data_parallel line 207"))
            input=input.cuda()
            output = torch.nn.parallel.data_parallel(model, input, device_ids=device_ids)
        else:
            output = model(input)
        
        loss = metric_loss(output, target, weights=(0.32, 0.08, 0.02, 0.01, 0.005), loss=args.loss)
        depth2_norm_error = metric_loss(output[0], target, normalize=True)
        depth2_metric_error = metric_loss(output[0], target, normalize=False)
        # record loss and EPE
        losses.update(loss.item(), target.size(0))
        train_writer.add_scalar('train_loss', loss.item(), n_iter)
        depth2_metric_errors.update(depth2_metric_error.item(), target.size(0))
        depth2_normalized_errors.update(depth2_norm_error.item(), target.size(0))

        # compute gradient and do SGD step
        optimizer.zero_grad()
        loss.backward()
        #optimizer.module.step()
        optimizer.step()
        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        with open(os.path.join(args.save_path, args.log_full), 'a') as csvfile:
            writer = csv.writer(csvfile, delimiter='\t')
            writer.writerow([loss.item(), depth2_metric_error.item()])
        term_logger.train_bar.update(i+1)
        if i % args.print_freq == 0:
            term_logger.train_writer.write(
                'Train: Time {batch_time.val:.3f} ({batch_time.avg:.3f}) '
                'Data {data_time.val:.3f} ({data_time.avg:.3f}) '
                'Loss {loss.val:.4f} ({loss.avg:.4f}) '
                'Depth error {depth2_error.val:.3f} ({depth2_error.avg:.3f})\r'
                .format(batch_time=batch_time, data_time=data_time,
                        loss=losses, depth2_error=depth2_metric_errors))
        if i >= epoch_size - 1:
            break
        n_iter += 1
    logging.info("leave train function")
    return losses.avg, depth2_metric_errors.avg, depth2_normalized_errors.avg


@torch.no_grad()
def validate(val_loader, model, epoch, logger, output_writers=[]):
    batch_time = AverageMeter()
    depth2_metric_errors = AverageMeter()
    depth2_norm_errors = AverageMeter()
    log_outputs = len(output_writers) > 0
    # switch to evaluate mode
    model.eval()

    end = time.time()

    for i, (input, target, _) in enumerate(val_loader):
        logging.info("val_loader loop = %d"%i)
        target = target.to(device)
        input = torch.cat(input, 1).to(device)
        input = input.cuda()
        # compute output
        output = model(input)
        if log_outputs and i < len(output_writers):  # log first output of 3 first batches
            if epoch == 0:
                output_writers[i].add_image('GroundTruth', util.tensor2array(target[0], max_value=100), 0)
                output_writers[i].add_image('Inputs', util.tensor2array(input[0,:3]), 0)
                output_writers[i].add_image('Inputs', util.tensor2array(input[0,3:]), 1)
            output_writers[i].add_image('DepthNet Outputs', util.tensor2array(output[0], max_value=100), epoch)
        depth2_norm_error = metric_loss(output, target, normalize=True)
        depth2_metric_error = metric_loss(output, target, normalize=False)
        # record depth error
        depth2_norm_errors.update(depth2_norm_error.item(), target.size(0))
        depth2_metric_errors.update(depth2_metric_error.item(), target.size(0))

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()
        logger.test_bar.update(i+1)
        if i % args.print_freq == 0:
            logger.test_writer.write(
                'Validation: '
                'Time {batch_time.val:.3f} ({batch_time.avg:.3f}) '
                'Depth error {depth2_error.val:.3f} ({depth2_error.avg:.3f})'
                .format(batch_time=batch_time,
                        depth2_error=depth2_metric_errors))

    return depth2_metric_errors.avg, depth2_norm_errors.avg


if __name__ == '__main__':
    main()
