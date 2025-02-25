# ---------------------------------------------------------------
# Copyright (c) 2022, NVIDIA CORPORATION. All rights reserved.
#
# This work is licensed under the NVIDIA Source Code License
# for Denoising Diffusion GAN. To view a copy of this license, see the LICENSE file.
# ---------------------------------------------------------------

from glob import glob
import argparse
import torch
import numpy as np

import os

import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torchvision

import torchvision.transforms as transforms
from torchvision.datasets import CIFAR10, ImageFolder
from datasets_prep.lsun import LSUN
from datasets_prep.stackmnist_data import StackedMNIST, _data_transforms_stacked_mnist
from datasets_prep.lmdb_datasets import LMDBDataset


from torch.multiprocessing import Process
import torch.distributed as dist
import shutil
import logging
from encoder import build_encoder
from utils import ResampledShards2
from torch.utils.tensorboard import SummaryWriter

from transformers import CLIPFeatureExtractor, CLIPTextModel, CLIPTokenizer
from datasets import load_dataset


def log_and_continue(exn):
    logging.warning(f'Handling webdataset error ({repr(exn)}). Ignoring.')
    return True

def copy_source(file, output_dir):
    shutil.copyfile(file, os.path.join(output_dir, os.path.basename(file)))
            
def broadcast_params(params):
    for param in params:
        dist.broadcast(param.data, src=0)


#%% Diffusion coefficients 
def var_func_vp(t, beta_min, beta_max):
    log_mean_coeff = -0.25 * t ** 2 * (beta_max - beta_min) - 0.5 * t * beta_min
    var = 1. - torch.exp(2. * log_mean_coeff)
    return var

def var_func_geometric(t, beta_min, beta_max):
    return beta_min * ((beta_max / beta_min) ** t)

def extract(input, t, shape):
    out = torch.gather(input, 0, t)
    reshape = [shape[0]] + [1] * (len(shape) - 1)
    out = out.reshape(*reshape)

    return out

def get_time_schedule(args, device):
    n_timestep = args.num_timesteps
    eps_small = 1e-3
    t = np.arange(0, n_timestep + 1, dtype=np.float64)
    t = t / n_timestep
    t = torch.from_numpy(t) * (1. - eps_small)  + eps_small
    return t.to(device)

def get_sigma_schedule(args, device):
    n_timestep = args.num_timesteps
    beta_min = args.beta_min
    beta_max = args.beta_max
    eps_small = 1e-3
   
    t = np.arange(0, n_timestep + 1, dtype=np.float64)
    t = t / n_timestep
    t = torch.from_numpy(t) * (1. - eps_small) + eps_small
    
    if args.use_geometric:
        var = var_func_geometric(t, beta_min, beta_max)
    else:
        var = var_func_vp(t, beta_min, beta_max)
    alpha_bars = 1.0 - var
    betas = 1 - alpha_bars[1:] / alpha_bars[:-1]
    
    first = torch.tensor(1e-8)
    betas = torch.cat((first[None], betas)).to(device)
    betas = betas.type(torch.float32)
    sigmas = betas**0.5
    a_s = torch.sqrt(1-betas)
    return sigmas, a_s, betas

class Diffusion_Coefficients():
    def __init__(self, args, device):
                
        self.sigmas, self.a_s, _ = get_sigma_schedule(args, device=device)
        self.a_s_cum = np.cumprod(self.a_s.cpu())
        self.sigmas_cum = np.sqrt(1 - self.a_s_cum ** 2)
        self.a_s_prev = self.a_s.clone()
        self.a_s_prev[-1] = 1
        
        self.a_s_cum = self.a_s_cum.to(device)
        self.sigmas_cum = self.sigmas_cum.to(device)
        self.a_s_prev = self.a_s_prev.to(device)
    
def q_sample(coeff, x_start, t, *, noise=None):
    """
    Diffuse the data (t == 0 means diffused for t step)
    """
    if noise is None:
      noise = torch.randn_like(x_start)
      
    x_t = extract(coeff.a_s_cum, t, x_start.shape) * x_start + \
          extract(coeff.sigmas_cum, t, x_start.shape) * noise
    
    return x_t

def q_sample_pairs(coeff, x_start, t):
    """
    Generate a pair of disturbed images for training
    :param x_start: x_0
    :param t: time step t
    :return: x_t, x_{t+1}
    """
    noise = torch.randn_like(x_start)
    x_t = q_sample(coeff, x_start, t)
    x_t_plus_one = extract(coeff.a_s, t+1, x_start.shape) * x_t + \
                   extract(coeff.sigmas, t+1, x_start.shape) * noise
    
    return x_t, x_t_plus_one
#%% posterior sampling
class Posterior_Coefficients():
    def __init__(self, args, device):
        
        _, _, self.betas = get_sigma_schedule(args, device=device)
        
        #we don't need the zeros
        self.betas = self.betas.type(torch.float32)[1:]
        
        self.alphas = 1 - self.betas
        self.alphas_cumprod = torch.cumprod(self.alphas, 0)
        self.alphas_cumprod_prev = torch.cat(
                                    (torch.tensor([1.], dtype=torch.float32,device=device), self.alphas_cumprod[:-1]), 0
                                        )               
        self.posterior_variance = self.betas * (1 - self.alphas_cumprod_prev) / (1 - self.alphas_cumprod)
        
        self.sqrt_alphas_cumprod = torch.sqrt(self.alphas_cumprod)
        self.sqrt_recip_alphas_cumprod = torch.rsqrt(self.alphas_cumprod)
        self.sqrt_recipm1_alphas_cumprod = torch.sqrt(1 / self.alphas_cumprod - 1)
        
        self.posterior_mean_coef1 = (self.betas * torch.sqrt(self.alphas_cumprod_prev) / (1 - self.alphas_cumprod))
        self.posterior_mean_coef2 = ((1 - self.alphas_cumprod_prev) * torch.sqrt(self.alphas) / (1 - self.alphas_cumprod))
        
        self.posterior_log_variance_clipped = torch.log(self.posterior_variance.clamp(min=1e-20))
        
def sample_posterior(coefficients, x_0,x_t, t):
    
    def q_posterior(x_0, x_t, t):
        mean = (
            extract(coefficients.posterior_mean_coef1, t, x_t.shape) * x_0
            + extract(coefficients.posterior_mean_coef2, t, x_t.shape) * x_t
        )
        var = extract(coefficients.posterior_variance, t, x_t.shape)
        log_var_clipped = extract(coefficients.posterior_log_variance_clipped, t, x_t.shape)
        return mean, var, log_var_clipped
    
  
    def p_sample(x_0, x_t, t):
        mean, _, log_var = q_posterior(x_0, x_t, t)
        
        noise = torch.randn_like(x_t)
        
        nonzero_mask = (1 - (t == 0).type(torch.float32))

        return mean + nonzero_mask[:,None,None,None] * torch.exp(0.5 * log_var) * noise
            
    sample_x_pos = p_sample(x_0, x_t, t)
    
    return sample_x_pos

def sample_from_model(coefficients, generator, n_time, x_init, T, opt, cond=None):
    x = x_init
    with torch.no_grad():
        for i in reversed(range(n_time)):
            t = torch.full((x.size(0),), i, dtype=torch.int64).to(x.device)
          
            t_time = t
            latent_z = torch.randn(x.size(0), opt.nz, device=x.device)
            x_0 = generator(x, t_time, latent_z, cond=cond)
            x_new = sample_posterior(coefficients, x_0, x, t)
            x = x_new.detach()
        
    return x

from contextlib import suppress

def filter_no_caption(sample):
    return 'txt' in sample

def get_autocast(precision):
    if precision == 'amp':
        return torch.cuda.amp.autocast
    elif precision == 'amp_bfloat16':
        return lambda: torch.cuda.amp.autocast(dtype=torch.bfloat16)
    else:
        return suppress 



def train(rank, gpu, args):
    from score_sde.models.discriminator import Discriminator_small, Discriminator_large, CondAttnDiscriminator, SmallCondAttnDiscriminator
    from score_sde.models.ncsnpp_generator_adagn import NCSNpp
    from EMA import EMA
    
    #torch.manual_seed(args.seed + rank)
    #torch.cuda.manual_seed(args.seed + rank)
    #torch.cuda.manual_seed_all(args.seed + rank)
    device = "cuda"
    autocast = get_autocast(args.precision)
    batch_size = args.batch_size
    
    nz = args.nz #latent dimension

    tokenizer = CLIPTokenizer.from_pretrained(args.text_encoder)
    CLIPTextModel.from_pretrained(args.text_encoder).to(device)

    def tokenize_captions(examples):
        #leave 10% blank to get better results from classifier free guidance
        captions = [caption if random.random() > 0.1 else "" for caption in examples[args.caption_column]]
        # captions = [caption for caption in examples[caption_column]]
        # print(captions)
        text_inputs = tokenizer(captions, max_length=args.max_seq_length, padding="max_length", truncation=True)
        examples["input_ids"] = text_inputs.input_ids
        examples["attention_mask"] = text_inputs.attention_mask
        return examples

    def transform_images(examples):
        images = [augmentations(image.convert("RGB")) for image in examples["image"]]
        return {"input": images}

    dataset = load_dataset(
            args.dataset,
            cache_dir="./cache",
            split="train",
    )

    augmentations = transforms.Compose(
        [
            transforms.Resize(args.image_size, interpolation=transforms.InterpolationMode.BILINEAR),
            transforms.CenterCrop(args.image_size),
            transforms.ToTensor(),
            transforms.Normalize((0.5,0.5,0.5), (0.5,0.5,0.5)),
        ]
    )

    
    
    dataset.set_transform(transform_images)

    dataset = dataset["train"]

    dataset = dataset.map(
            function=tokenize_captions,
            batched=True,
            batch_size=32,
            remove_columns=[col for col in column_names if col != image_column],
            num_proc=args.preprocessing_num_workers,
            load_from_cache_file=not args.overwrite_cache,
            desc="Running tokenizer on train dataset",
        )
    
    train_sampler = torch.utils.data.distributed.DistributedSampler(dataset,
                                                                    num_replicas=args.world_size,
                                                                    rank=rank)
    data_loader = torch.utils.data.DataLoader(dataset,
                                               batch_size=batch_size,
                                               shuffle=False,
                                               num_workers=4,
                                               pin_memory=True,
                                               sampler=train_sampler,
                                               drop_last = True)
        
    

    # args.cond_size = text_encoder.output_size
    netG = NCSNpp(args).to(device)
    nb_params = 0
    for param in netG.parameters():
        nb_params += param.flatten().shape[0]
    print("Number of generator parameters:", nb_params)
    
    if args.discr_type == "small":    
        netD = Discriminator_small(nc = 2*args.num_channels, ngf = args.ngf,
                               t_emb_dim = args.t_emb_dim,
                               cond_size=args.cond_size,
                               act=nn.LeakyReLU(0.2)).to(device)
    elif args.discr_type == "small_cond_attn":    
        netD = SmallCondAttnDiscriminator(nc = 2*args.num_channels, ngf = args.ngf,
                               t_emb_dim = args.t_emb_dim,
                               cond_size=args.cond_size,
                               act=nn.LeakyReLU(0.2)).to(device)

    elif args.discr_type == "large":
        netD = Discriminator_large(nc = 2*args.num_channels, ngf = args.ngf, 
                                t_emb_dim = args.t_emb_dim,
                                cond_size=args.cond_size,
                                act=nn.LeakyReLU(0.2)).to(device)
    elif args.discr_type == "large_attn_pool":
        netD = Discriminator_large(nc = 2*args.num_channels, ngf = args.ngf, 
                                t_emb_dim = args.t_emb_dim,
                                cond_size=args.cond_size,
                                attn_pool=True,
                                act=nn.LeakyReLU(0.2)).to(device)

    elif args.discr_type == "large_cond_attn":
        netD = CondAttnDiscriminator(
            nc = 2*args.num_channels, 
            ngf = args.ngf, 
            t_emb_dim = args.t_emb_dim,
            cond_size=args.cond_size,
            act=nn.LeakyReLU(0.2)).to(device)

    broadcast_params(netG.parameters())
    broadcast_params(netD.parameters())
    
    if args.fsdp:
        from fairscale.nn.checkpoint.checkpoint_activations import checkpoint_wrapper
        from fairscale.nn.data_parallel import FullyShardedDataParallel as FSDP
        netG = FSDP(
            netG,
            flatten_parameters=True,
            verbose=True,
        )

    optimizerD = optim.Adam(netD.parameters(), lr=args.lr_d, betas = (args.beta1, args.beta2))
    optimizerG = optim.Adam(netG.parameters(), lr=args.lr_g, betas = (args.beta1, args.beta2))
    
    if args.use_ema:
        optimizerG = EMA(optimizerG, ema_decay=args.ema_decay)
    
    schedulerG = torch.optim.lr_scheduler.CosineAnnealingLR(optimizerG, args.num_epoch, eta_min=1e-5)
    schedulerD = torch.optim.lr_scheduler.CosineAnnealingLR(optimizerD, args.num_epoch, eta_min=1e-5)

    if args.fsdp:   
        netD = nn.parallel.DistributedDataParallel(netD, device_ids=[gpu])
    else:
        netG = nn.parallel.DistributedDataParallel(netG, device_ids=[gpu])
        netD = nn.parallel.DistributedDataParallel(netD, device_ids=[gpu])
    
    if args.grad_checkpointing:
        from fairscale.nn.checkpoint.checkpoint_activations import checkpoint_wrapper
        netG = checkpoint_wrapper(netG)

    exp = args.exp
    parent_dir = "./saved_info/dd_gan/{}".format(args.dataset)

    exp_path = os.path.join(parent_dir,exp)
    if rank == 0:
        if not os.path.exists(exp_path):
            os.makedirs(exp_path)
            copy_source(__file__, exp_path)
            shutil.copytree('score_sde/models', os.path.join(exp_path, 'score_sde/models'))
    
    coeff = Diffusion_Coefficients(args, device)
    pos_coeff = Posterior_Coefficients(args, device)
    T = get_time_schedule(args, device)
    
    checkpoint_file = os.path.join(exp_path, 'content.pth')
    
    if rank == 0:
        log_writer = SummaryWriter(exp_path)

    if args.resume and os.path.exists(checkpoint_file):
        checkpoint = torch.load(checkpoint_file, map_location="cpu")
        init_epoch = checkpoint['epoch']
        epoch = init_epoch
        netG.load_state_dict(checkpoint['netG_dict'])
        # load G
        
        optimizerG.load_state_dict(checkpoint['optimizerG'])
        schedulerG.load_state_dict(checkpoint['schedulerG'])
        # load D
        netD.load_state_dict(checkpoint['netD_dict'])
        optimizerD.load_state_dict(checkpoint['optimizerD'])
        schedulerD.load_state_dict(checkpoint['schedulerD'])
        global_step = checkpoint['global_step']
        print("=> loaded checkpoint (epoch {})"
                  .format(checkpoint['epoch']))
    else:
        global_step, epoch, init_epoch = 0, 0, 0
    use_cond_attn_discr = args.discr_type in ("large_cond_attn", "small_cond_attn", "large_attn_pool")
    for epoch in range(init_epoch, args.num_epoch+1):
        train_sampler.set_epoch(epoch)
       
        for iteration, batch in enumerate(data_loader):
            #print(x.shape)
            # if args.dataset != "wds":
            #     y = [str(yi) for yi in y.tolist()]
            
            # if args.classifier_free_guidance_proba:
            #     u = (np.random.uniform(size=len(y)) <= args.classifier_free_guidance_proba).tolist()
            #     y = ["" if ui else yi for yi,ui in zip(y, u)]

            with torch.no_grad():
                cond = text_encoder(batch["input_ids"].to(device))[0]
                cond_mask = batch["attention_mask"].to(device).bool()

            for p in netD.parameters():  
                p.requires_grad = True  
            
            netD.zero_grad()
            
            #sample from p(x_0)
            real_data = batch["input"].to(device, non_blocking=True)
            
            #sample t
            t = torch.randint(0, args.num_timesteps, (real_data.size(0),), device=device)
            
            x_t, x_tp1 = q_sample_pairs(coeff, real_data, t)
            x_t.requires_grad = True
            
            cond_for_discr = (cond, cond_mask)
            if args.grad_penalty_cond:
                if use_cond_attn_discr:
                    #cond_pooled.requires_grad = True
                    cond.requires_grad = True
                    #cond_mask.requires_grad = True
                else:
                    cond_for_discr.requires_grad = True

            # train with real
            with autocast():
                D_real = netD(x_t, t, x_tp1.detach(), cond=cond_for_discr).view(-1)
                errD_real = F.softplus(-D_real)
                errD_real = errD_real.mean()

            
            errD_real.backward(retain_graph=True)
            
            grad_penalty = None
            if args.lazy_reg is None:
                if args.grad_penalty_cond:
                    inputs = (x_t,) + (cond,) if use_cond_attn_discr else (cond_for_discr,)
                    grad_real = torch.autograd.grad(
                                outputs=D_real.sum(), inputs=inputs, create_graph=True
                                )[0]
                    grad_real = torch.cat([g.view(g.size(0), -1) for g in grad_real])
                    grad_penalty = (grad_real.norm(2, dim=1) ** 2).mean()
                    grad_penalty = args.r1_gamma / 2 * grad_penalty
                    grad_penalty.backward()
                else:
                    grad_real = torch.autograd.grad(
                                outputs=D_real.sum(), inputs=x_t, create_graph=True
                                )[0]
                    grad_penalty = (
                                    grad_real.view(grad_real.size(0), -1).norm(2, dim=1) ** 2
                                    ).mean()
                    
                    
                    grad_penalty = args.r1_gamma / 2 * grad_penalty
                    grad_penalty.backward()
            else:
                if global_step % args.lazy_reg == 0:
                    if args.grad_penalty_cond:
                        inputs = (x_t,) + (cond,) if use_cond_attn_discr else (cond_for_discr,)
                        grad_real = torch.autograd.grad(
                                    outputs=D_real.sum(), inputs=inputs, create_graph=True
                                    )[0]
                        grad_real = torch.cat([g.view(g.size(0), -1) for g in grad_real])
                        grad_penalty = (grad_real.norm(2, dim=1) ** 2).mean()
                        grad_penalty = args.r1_gamma / 2 * grad_penalty
                        grad_penalty.backward()
                    else:
                        grad_real = torch.autograd.grad(
                                outputs=D_real.sum(), inputs=x_t, create_graph=True
                                )[0]
                        grad_penalty = (
                                    grad_real.view(grad_real.size(0), -1).norm(2, dim=1) ** 2
                                    ).mean()
                    
                        grad_penalty = args.r1_gamma / 2 * grad_penalty
                        grad_penalty.backward()

            # train with fake
            latent_z = torch.randn(batch_size, nz, device=device)
            with autocast():
                if args.grad_checkpointing:
                    ginp  = x_tp1.detach()
                    ginp.requires_grad = True
                    latent_z.requires_grad = True
                    # cond_pooled.requires_grad = True
                    cond.requires_grad = True
                    #cond_mask.requires_grad = True
                    x_0_predict = netG(ginp, t, latent_z, cond=(cond, cond_mask))
                else:
                    x_0_predict = netG(x_tp1.detach(), t, latent_z, cond=(cond, cond_mask))
                x_pos_sample = sample_posterior(pos_coeff, x_0_predict, x_tp1, t)
                
                output = netD(x_pos_sample, t, x_tp1.detach(), cond=cond_for_discr).view(-1)
                    
                
                errD_fake = F.softplus(output)
                errD_fake = errD_fake.mean()

            if args.mismatch_loss:
                # following https://github.com/tobran/DF-GAN/blob/bc38a4f795c294b09b4ef5579cd4ff78807e5b96/code/lib/modules.py,
                # we add a discr loss for (real image, non matching text)
                #inds = torch.flip(torch.arange(len(x_t)), dims=(0,))
                with autocast():
                    inds = torch.cat([torch.arange(1,len(x_t)),torch.arange(1)])
                    cond_for_discr_mis =  (cond[inds], cond_mask[inds])
                    D_real_mis = netD(x_t, t, x_tp1.detach(), cond=cond_for_discr_mis).view(-1)
                    errD_real_mis = F.softplus(D_real_mis)
                    errD_real_mis = errD_real_mis.mean()
                    errD_fake = errD_fake * 0.5 + errD_real_mis * 0.5
        
            errD_fake.backward()
    
            
            errD = errD_real + errD_fake
            # Update D
            optimizerD.step()
            
        
            #update G
            for p in netD.parameters():
                p.requires_grad = False
            netG.zero_grad()
            
            
            t = torch.randint(0, args.num_timesteps, (real_data.size(0),), device=device)
            
            
            x_t, x_tp1 = q_sample_pairs(coeff, real_data, t)
                
            
            latent_z = torch.randn(batch_size, nz,device=device)
            
            with autocast():
                if args.grad_checkpointing:
                    ginp  = x_tp1.detach()
                    ginp.requires_grad = True
                    latent_z.requires_grad = True
                    # cond_pooled.requires_grad = True
                    cond.requires_grad = True
                    #cond_mask.requires_grad = True
                    x_0_predict = netG(ginp, t, latent_z, cond=(cond, cond_mask))
                else:
                    x_0_predict = netG(x_tp1.detach(), t, latent_z, cond=(cond, cond_mask))
                x_pos_sample = sample_posterior(pos_coeff, x_0_predict, x_tp1, t)
                
                output = netD(x_pos_sample, t, x_tp1.detach(), cond=cond_for_discr).view(-1)
                
                
                errG = F.softplus(-output)
                errG = errG.mean()
            
            errG.backward()
            optimizerG.step()
                
            if (iteration % 10 == 0) and (rank == 0):
                log_writer.add_scalar('g_loss', errG.item(), global_step)
                log_writer.add_scalar('d_loss', errD.item(), global_step)
                if grad_penalty is not None:
                    log_writer.add_scalar('grad_penalty', grad_penalty.item(), global_step)
            
            global_step += 1


            if iteration % 100 == 0:
                if rank == 0:
                    print('epoch {} iteration{}, G Loss: {}, D Loss: {}'.format(epoch,iteration, errG.item(), errD.item()))
                    print('Global step:', global_step)
            if iteration % 1000 == 0:
                x_t_1 = torch.randn_like(real_data)
                with autocast():
                    fake_sample = sample_from_model(pos_coeff, netG, args.num_timesteps, x_t_1, T, args, cond=(cond, cond_mask))
                if rank == 0:
                    torchvision.utils.save_image(fake_sample, os.path.join(exp_path, 'sample_discrete_epoch_{}_iteration_{}.png'.format(epoch, iteration)), normalize=True)
                
                if args.save_content:
                    dist.barrier()
                    print('Saving content.')
                    def to_cpu(d):
                        for k, v in d.items():
                            d[k] = v.cpu()
                        return d
                    
                    if args.fsdp:
                        netG_state_dict = to_cpu(netG.state_dict())
                        netD_state_dict = to_cpu(netD.state_dict())
                        #netG_optim_state_dict = (netG.gather_full_optim_state_dict(optimizerG))
                        netG_optim_state_dict = optimizerG.state_dict()
                        #print(netG_optim_state_dict)
                        netD_optim_state_dict = (optimizerD.state_dict())
                        content = {'epoch': epoch + 1, 'global_step': global_step, 'args': args,
                                'netG_dict': netG_state_dict, 'optimizerG': netG_optim_state_dict,
                                'schedulerG': schedulerG.state_dict(), 'netD_dict': netD_state_dict,
                                'optimizerD': netD_optim_state_dict, 'schedulerD': schedulerD.state_dict()}
                        if rank == 0:
                            torch.save(content, os.path.join(exp_path, 'content.pth'))
                            torch.save(content, os.path.join(exp_path, 'content_backup.pth'))
                        if args.use_ema:
                            optimizerG.swap_parameters_with_ema(store_params_in_ema=True)                        
                        if args.use_ema and rank == 0:
                            torch.save(netG.state_dict(), os.path.join(exp_path, 'netG_{}.pth'.format(epoch)))
                        if args.use_ema:
                            optimizerG.swap_parameters_with_ema(store_params_in_ema=True)
                        #if args.use_ema:
                        #    dist.barrier()
                        print("Saved content")
                    else:
                        if rank == 0:
                            content = {'epoch': epoch + 1, 'global_step': global_step, 'args': args,
                                    'netG_dict': netG.state_dict(), 'optimizerG': optimizerG.state_dict(),
                                    'schedulerG': schedulerG.state_dict(), 'netD_dict': netD.state_dict(),
                                    'optimizerD': optimizerD.state_dict(), 'schedulerD': schedulerD.state_dict()}                    
                            torch.save(content, os.path.join(exp_path, 'content.pth'))
                            torch.save(content, os.path.join(exp_path, 'content_backup.pth'))
                            if args.use_ema:
                                optimizerG.swap_parameters_with_ema(store_params_in_ema=True)                        
                            torch.save(netG.state_dict(), os.path.join(exp_path, 'netG_{}.pth'.format(epoch)))
                            if args.use_ema:
                                optimizerG.swap_parameters_with_ema(store_params_in_ema=True)

            
        if not args.no_lr_decay:
            
            schedulerG.step()
            schedulerD.step()
        """
        if rank == 0:
            if epoch % 10 == 0:
                torchvision.utils.save_image(x_pos_sample, os.path.join(exp_path, 'xpos_epoch_{}.png'.format(epoch)), normalize=True)
            
            x_t_1 = torch.randn_like(real_data)
            with autocast():
                fake_sample = sample_from_model(pos_coeff, netG, args.num_timesteps, x_t_1, T, args, cond=(cond_pooled, cond, cond_mask))
            torchvision.utils.save_image(fake_sample, os.path.join(exp_path, 'sample_discrete_epoch_{}.png'.format(epoch)), normalize=True)
            
            if args.save_content:
                if epoch % args.save_content_every == 0:
                    print('Saving content.')
                    content = {'epoch': epoch + 1, 'global_step': global_step, 'args': args,
                               'netG_dict': netG.state_dict(), 'optimizerG': optimizerG.state_dict(),
                               'schedulerG': schedulerG.state_dict(), 'netD_dict': netD.state_dict(),
                               'optimizerD': optimizerD.state_dict(), 'schedulerD': schedulerD.state_dict()}
                    
                    torch.save(content, os.path.join(exp_path, 'content.pth'))
                    torch.save(content, os.path.join(exp_path, 'content_backup.pth'))
                
            if epoch % args.save_ckpt_every == 0:
                if args.use_ema:
                    optimizerG.swap_parameters_with_ema(store_params_in_ema=True)
                    
                torch.save(netG.state_dict(), os.path.join(exp_path, 'netG_{}.pth'.format(epoch)))
                if args.use_ema:
                    optimizerG.swap_parameters_with_ema(store_params_in_ema=True)
        dist.barrier()
        """


def init_processes(rank, size, fn, args):
    """ Initialize the distributed environment. """

    import os

    args.rank = int(os.environ['SLURM_PROCID'])
    args.world_size =  int(os.getenv("SLURM_NTASKS"))
    args.local_rank = int(os.environ['SLURM_LOCALID'])
    print(args.rank, args.world_size)
    args.master_address = os.getenv("SLURM_LAUNCH_NODE_IPADDR")
    os.environ['MASTER_ADDR'] = args.master_address
    os.environ['MASTER_PORT'] = "12345"
    torch.cuda.set_device(args.local_rank)
    gpu = args.local_rank
    dist.init_process_group(backend='nccl', init_method='env://', rank=rank, world_size=args.world_size)
    fn(rank, gpu, args)
    dist.barrier()
    cleanup()  

def cleanup():
    dist.destroy_process_group()    
#%%
if __name__ == '__main__':
    parser = argparse.ArgumentParser('ddgan parameters')
    parser.add_argument('--seed', type=int, default=1024,
                        help='seed used for initialization')
    
    parser.add_argument('--resume', action='store_true',default=False)
    parser.add_argument('--masked_mean', action='store_true',default=False)
    parser.add_argument('--mismatch_loss', action='store_true',default=False)
    parser.add_argument('--text_encoder', type=str, default="openai/clip-vit-large-patch14")
    parser.add_argument('--cross_attention', action='store_true',default=False)
    parser.add_argument('--fsdp', action='store_true',default=False)
    parser.add_argument('--grad_checkpointing', action='store_true',default=False)

    parser.add_argument('--image_size', type=int, default=32,
                            help='size of image')
    parser.add_argument('--caption_column', type=str, default="text")
    parser.add_argument('--preprocessing_num_workers', type=int, default=32)
    parser.add_argument('--num_channels', type=int, default=3,
                            help='channel of image')
    parser.add_argument('--centered', action='store_false', default=True,
                            help='-1,1 scale')
    parser.add_argument('--use_geometric', action='store_true',default=False)
    parser.add_argument('--beta_min', type=float, default= 0.1,
                            help='beta_min for diffusion')
    parser.add_argument('--beta_max', type=float, default=20.,
                            help='beta_max for diffusion')
    parser.add_argument('--classifier_free_guidance_proba', type=float, default=0.0)
    
    parser.add_argument('--num_channels_dae', type=int, default=128,
                            help='number of initial channels in denosing model')
    parser.add_argument('--n_mlp', type=int, default=3,
                            help='number of mlp layers for z')
    parser.add_argument('--ch_mult', nargs='+', type=int,
                            help='channel multiplier')
    parser.add_argument('--num_res_blocks', type=int, default=2,
                            help='number of resnet blocks per scale')
    parser.add_argument('--attn_resolutions', default=(16,), nargs='+', type=int,
                            help='resolution of applying attention')
    parser.add_argument('--dropout', type=float, default=0.,
                            help='drop-out rate')
    parser.add_argument('--resamp_with_conv', action='store_false', default=True,
                            help='always up/down sampling with conv')
    parser.add_argument('--conditional', action='store_false', default=True,
                            help='noise conditional')
    parser.add_argument('--fir', action='store_false', default=True,
                            help='FIR')
    parser.add_argument('--fir_kernel', default=[1, 3, 3, 1],
                            help='FIR kernel')
    parser.add_argument('--skip_rescale', action='store_false', default=True,
                            help='skip rescale')
    parser.add_argument('--resblock_type', default='biggan',
                            help='tyle of resnet block, choice in biggan and ddpm')
    parser.add_argument('--progressive', type=str, default='none', choices=['none', 'output_skip', 'residual'],
                            help='progressive type for output')
    parser.add_argument('--progressive_input', type=str, default='residual', choices=['none', 'input_skip', 'residual'],
                        help='progressive type for input')
    parser.add_argument('--progressive_combine', type=str, default='sum', choices=['sum', 'cat'],
                        help='progressive combine method.')
    
    parser.add_argument('--embedding_type', type=str, default='positional', choices=['positional', 'fourier'],
                        help='type of time embedding')
    parser.add_argument('--fourier_scale', type=float, default=16.,
                            help='scale of fourier transform')
    parser.add_argument('--not_use_tanh', action='store_true',default=False)
    
    #geenrator and training
    parser.add_argument('--exp', default='experiment_cifar_default', help='name of experiment')
    parser.add_argument('--dataset', default='cifar10', help='name of dataset')
    parser.add_argument('--dataset_root', default='', help='name of dataset')
    parser.add_argument('--nz', type=int, default=100)
    parser.add_argument('--num_timesteps', type=int, default=4)

    parser.add_argument('--cond_size', type=int, default=768)
    parser.add_argument('--z_emb_dim', type=int, default=256)
    parser.add_argument('--t_emb_dim', type=int, default=256)
    parser.add_argument('--batch_size', type=int, default=128, help='input batch size')
    parser.add_argument('--num_epoch', type=int, default=1200)
    parser.add_argument('--ngf', type=int, default=64)

    parser.add_argument('--lr_g', type=float, default=1.5e-4, help='learning rate g')
    parser.add_argument('--lr_d', type=float, default=1e-4, help='learning rate d')
    parser.add_argument('--beta1', type=float, default=0.5,
                            help='beta1 for adam')
    parser.add_argument('--beta2', type=float, default=0.9,
                            help='beta2 for adam')
    parser.add_argument('--no_lr_decay',action='store_true', default=False)
    parser.add_argument('--grad_penalty_cond', action='store_true',default=False)

    parser.add_argument('--use_ema', action='store_true', default=False,
                            help='use EMA or not')
    parser.add_argument('--ema_decay', type=float, default=0.9999, help='decay rate for EMA')
    
    parser.add_argument('--r1_gamma', type=float, default=0.05, help='coef for r1 reg')

    parser.add_argument('--lazy_reg', type=int, default=None,
                        help='lazy regulariation.')

    parser.add_argument('--save_content', action='store_true',default=False)
    parser.add_argument('--save_content_every', type=int, default=50, help='save content for resuming every x epochs')
    parser.add_argument('--save_ckpt_every', type=int, default=25, help='save ckpt every x epochs')
    parser.add_argument('--discr_type', type=str, default="large_cond_attn")
    parser.add_argument('--preprocessing', type=str, default="resize")
    parser.add_argument('--precision', type=str, default="fp32")

    ###ddp
    parser.add_argument('--num_proc_node', type=int, default=1,
                        help='The number of nodes in multi node env.')
    parser.add_argument('--num_process_per_node', type=int, default=1,
                        help='number of gpus')
    parser.add_argument('--node_rank', type=int, default=0,
                        help='The index of node.')
    parser.add_argument('--local_rank', type=int, default=0,
                        help='rank of process in the node')
    parser.add_argument('--master_address', type=str, default='127.0.0.1',
                        help='address for master')

    args = parser.parse_args()
    # args.world_size = args.num_proc_node * args.num_process_per_node
    args.world_size =  int(os.getenv("SLURM_NTASKS"))
    args.rank = int(os.environ['SLURM_PROCID'])
    # size = args.num_process_per_node
    init_processes(args.rank, args.world_size, train, args)
