# *****************************************************************************
#  Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are met:
#      * Redistributions of source code must retain the above copyright
#        notice, this list of conditions and the following disclaimer.
#      * Redistributions in binary form must reproduce the above copyright
#        notice, this list of conditions and the following disclaimer in the
#        documentation and/or other materials provided with the distribution.
#      * Neither the name of the NVIDIA CORPORATION nor the
#        names of its contributors may be used to endorse or promote products
#        derived from this software without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
#  ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
#  WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
#  DISCLAIMED. IN NO EVENT SHALL NVIDIA CORPORATION BE LIABLE FOR ANY
#  DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
#  (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
#  LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
#  ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
#  (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
#  SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# *****************************************************************************
import argparse
import json
import os
import torch
import warnings
warnings.filterwarnings('ignore')
import subprocess as sp
from tqdm import tqdm
import statistics

#=====START: ADDED FOR DISTRIBUTED======
from distributed import init_distributed, apply_gradient_allreduce, reduce_tensor
from torch.utils.data.distributed import DistributedSampler
#=====END:   ADDED FOR DISTRIBUTED======

from torch.utils.data import DataLoader
from glow import WaveGlow, WaveGlowLoss
from mel2samp import Mel2Samp

def load_checkpoint(checkpoint_path, model, optimizer):
    assert os.path.isfile(checkpoint_path)
    checkpoint_dict = torch.load(checkpoint_path, map_location='cpu')
    iteration = checkpoint_dict['iteration']
    optimizer.load_state_dict(checkpoint_dict['optimizer'])
    model_for_loading = checkpoint_dict['model']
    model.load_state_dict(model_for_loading.state_dict())
    print("Loaded checkpoint '{}' (iteration {})" .format(
          checkpoint_path, iteration))
    return model, optimizer, iteration

def save_checkpoint(model, optimizer, learning_rate, iteration, filepath):
    print("Saving model and optimizer state at iteration {} to {}".format(
          iteration, filepath))
    model_for_saving = WaveGlow(**waveglow_config).cuda()
    model_for_saving.load_state_dict(model.state_dict())
    torch.save({'model': model_for_saving,
                'iteration': iteration,
                'optimizer': optimizer.state_dict(),
                'learning_rate': learning_rate}, filepath)


def get_gpu_stats():
  _output_to_list = lambda x: x.decode('ascii').split('\n')[:-1]

  ACCEPTABLE_AVAILABLE_MEMORY = 1024
  COMMAND = "nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu --format=csv"
  info = _output_to_list(sp.check_output(COMMAND.split()))[1:][0].split()
  memory_used = int(info[0])
  memory_total = int(info[2])
  memory_used_pct = int((memory_used / memory_total) * 100)
  utilization = int(info[4])
  return memory_used_pct, utilization


def train(num_gpus, rank, group_name, output_directory, epochs, learning_rate,
          sigma, iters_per_checkpoint, batch_size, seed, fp16_run,
          checkpoint_path, with_tensorboard, num_workers=2):
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    #=====START: ADDED FOR DISTRIBUTED======
    if num_gpus > 1:
        init_distributed(rank, num_gpus, group_name, **dist_config)
    #=====END:   ADDED FOR DISTRIBUTED======

    criterion = WaveGlowLoss(sigma)
    model = WaveGlow(**waveglow_config).cuda()

    #=====START: ADDED FOR DISTRIBUTED======
    if num_gpus > 1:
        model = apply_gradient_allreduce(model)
    #=====END:   ADDED FOR DISTRIBUTED======

    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)

    if fp16_run:
        from apex import amp
        model, optimizer = amp.initialize(model, optimizer, opt_level='O1')

    # Load checkpoint if one exists
    iteration = 0
    if checkpoint_path != "":
        model, optimizer, iteration = load_checkpoint(checkpoint_path, model,
                                                      optimizer)
        iteration += 1  # next iteration is iteration + 1

    # HACK: setup separate training and eval sets
    training_files = data_config['training_files']
    eval_files = data_config['eval_files']
    del data_config['training_files']
    del data_config['eval_files']
    data_config['audio_files'] = training_files
    trainset = Mel2Samp(**data_config)
    data_config['audio_files'] = eval_files
    evalset = Mel2Samp(**data_config)

    # =====START: ADDED FOR DISTRIBUTED======
    train_sampler = DistributedSampler(trainset) if num_gpus > 1 else None
    eval_sampler = DistributedSampler(evalset) if num_gpus > 1 else None
    # =====END:   ADDED FOR DISTRIBUTED======

    print("Creating dataloaders with " + str(num_workers) + " workers")
    train_loader = DataLoader(trainset, num_workers=num_workers, shuffle=True,
                              sampler=train_sampler,
                              batch_size=batch_size,
                              pin_memory=False,
                              drop_last=True)
    eval_loader = DataLoader(evalset, num_workers=num_workers, shuffle=True,
                              sampler=eval_sampler,
                              batch_size=batch_size,
                              pin_memory=False,
                              drop_last=True)

    # Get shared output_directory ready
    if rank == 0:
        if not os.path.isdir(output_directory):
            os.makedirs(output_directory)
            os.chmod(output_directory, 0o775)
        print("output directory", output_directory)

    if with_tensorboard and rank == 0:
        from tensorboardX import SummaryWriter
        logger_train = SummaryWriter(os.path.join(output_directory, 'logs', 'train'))
        logger_eval = SummaryWriter(os.path.join(output_directory, 'logs', 'eval'))

    epoch_offset = max(0, int(iteration / len(train_loader)))
    # ================ MAIN TRAINNIG LOOP! ===================
    for epoch in range(epoch_offset, epochs):
        model.train()
        with tqdm(total=len(train_loader)) as train_pbar:
            for i, batch in enumerate(train_loader):
                model.zero_grad()

                mel, audio = batch
                mel = torch.autograd.Variable(mel.cuda())
                audio = torch.autograd.Variable(audio.cuda())
                outputs = model((mel, audio))

                loss = criterion(outputs)
                if num_gpus > 1:
                    reduced_loss = reduce_tensor(loss.data, num_gpus).item()
                else:
                    reduced_loss = loss.item()

                if fp16_run:
                    with amp.scale_loss(loss, optimizer) as scaled_loss:
                        scaled_loss.backward()
                else:
                    loss.backward()

                optimizer.step()

                train_pbar.set_description("Epoch {} Iter {} Loss {:.3f}".format(epoch, iteration, reduced_loss))
                if with_tensorboard and rank == 0 and iteration % 10 == 0:
                    logger_train.add_scalar('loss', reduced_loss, i + len(train_loader) * epoch)
                    # adding logging for GPU utilization and memory usage
                    gpu_memory_used, gpu_utilization = get_gpu_stats()
                    k = 'gpu' + str(0)
                    logger_train.add_scalar(k + '/memory', gpu_memory_used, iteration)
                    logger_train.add_scalar(k + '/load', gpu_utilization, iteration)
                    logger_train.flush()

                if (iteration % iters_per_checkpoint == 0):
                    if rank == 0:
                        checkpoint_path = "{}/waveglow_{}".format(
                            output_directory, iteration)
                        save_checkpoint(model, optimizer, learning_rate, iteration,
                                        checkpoint_path)

                iteration += 1
                train_pbar.update(1)

        # Eval
        model.eval()
        torch.cuda.empty_cache()

        with torch.no_grad():
            tensorboard_mel, tensorboard_audio = None, None
            loss_accum = []
            with tqdm(total=len(eval_loader)) as eval_pbar:
                for i, batch in enumerate(eval_loader):
                    model.zero_grad()
                    mel, audio = batch
                    mel = torch.autograd.Variable(mel.cuda())
                    audio = torch.autograd.Variable(audio.cuda())
                    outputs = model((mel, audio))
                    loss = criterion(outputs).item()
                    loss_accum.append(loss)
                    eval_pbar.set_description("Epoch {} Eval {:.3f}".format(epoch, loss))
                    outputs = None

                    # use the first batch for tensorboard audio samples
                    if i == 0:
                        tensorboard_mel = mel
                        tensorboard_audio = audio
                    eval_pbar.update(1)

            if with_tensorboard and rank == 0:
                loss_avg = statistics.mean(loss_accum)
                tqdm.write("Epoch {} Eval AVG {}".format(epoch, loss_avg))
                logger_eval.add_scalar('loss', loss_avg, iteration)

            # log audio samples to tensorboard
            tensorboard_audio_generated = model.infer(tensorboard_mel)
            for i in range(0, 5):
                ta = tensorboard_audio[i].cpu().numpy()
                tag = tensorboard_audio_generated[i].cpu().numpy()
                logger_eval.add_audio("sample " + str(i) + "/orig", ta, epoch, sample_rate=data_config['sampling_rate'])
                logger_eval.add_audio("sample " + str(i) + "/gen", tag, epoch, sample_rate=data_config['sampling_rate'])
            logger_eval.flush()



if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config', type=str,
                        help='JSON file for configuration')
    parser.add_argument('-r', '--rank', type=int, default=0,
                        help='rank of process for distributed')
    parser.add_argument('-g', '--group_name', type=str, default='',
                        help='name of group for distributed')
    args = parser.parse_args()

    # Parse configs.  Globals nicer in this case
    with open(args.config) as f:
        data = f.read()
    config = json.loads(data)
    train_config = config["train_config"]
    global data_config
    data_config = config["data_config"]
    global dist_config
    dist_config = config["dist_config"]
    global waveglow_config
    waveglow_config = config["waveglow_config"]

    num_gpus = torch.cuda.device_count()
    if num_gpus > 1:
        if args.group_name == '':
            print("WARNING: Multiple GPUs detected but no distributed group set")
            print("Only running 1 GPU.  Use distributed.py for multiple GPUs")
            num_gpus = 1

    if num_gpus == 1 and args.rank != 0:
        raise Exception("Doing single GPU training on rank > 0")

    torch.backends.cudnn.enabled = True
    torch.backends.cudnn.benchmark = False
    train(num_gpus, args.rank, args.group_name, **train_config)


def repl_test():
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config', type=str,
                        help='JSON file for configuration')
    parser.add_argument('-r', '--rank', type=int, default=0,
                        help='rank of process for distributed')
    parser.add_argument('-g', '--group_name', type=str, default='',
                        help='name of group for distributed')
    argv = ["-c", "config.json"]
    args = parser.parse_args(argv)

    # Parse configs.  Globals nicer in this case
    with open(args.config) as f:
        data = f.read()
    config = json.loads(data)
    train_config = config["train_config"]
    global data_config
    data_config = config["data_config"]
    global dist_config
    dist_config = config["dist_config"]
    global waveglow_config
    waveglow_config = config["waveglow_config"]

    num_gpus = torch.cuda.device_count()
    if num_gpus > 1:
        if args.group_name == '':
            print("WARNING: Multiple GPUs detected but no distributed group set")
            print("Only running 1 GPU.  Use distributed.py for multiple GPUs")
            num_gpus = 1

    if num_gpus == 1 and args.rank != 0:
        raise Exception("Doing single GPU training on rank > 0")

    torch.backends.cudnn.enabled = True
    torch.backends.cudnn.benchmark = False
    rank = args.rank
    group_name = args.group_name
    locals().update(train_config)