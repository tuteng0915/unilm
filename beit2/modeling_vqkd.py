# --------------------------------------------------------
# BEiT v2: Masked Image Modeling with Vector-Quantized Visual Tokenizers (https://arxiv.org/abs/2208.06366)
# Github source: https://github.com/microsoft/unilm/tree/master/beitv2
# Copyright (c) 2022 Microsoft
# Licensed under The MIT License [see LICENSE for details]
# By Zhiliang Peng
# Based on VQGAN code bases
# https://github.com/CompVis/taming-transformers
# --------------------------------------------------------'

import torch
import numpy as np
from torch import nn, einsum
import torch.nn.functional as F
import math
from collections import OrderedDict
from functools import partial, reduce
from einops import rearrange
from timm.models.layers import trunc_normal_
from timm.data.constants import IMAGENET_DEFAULT_MEAN, IMAGENET_DEFAULT_STD
from timm.models.registry import register_model
from data_utils import parse_resize, resize_fn

from modeling_finetune import VisionTransformer
from norm_ema_quantizer import NormEMAVectorQuantizer

import utils
from vqkd_teacher import clip, get_dino_vit_base
from vqkd_teacher.openclip_vemb import Pix2StructOpenClipVemb


class VQKD(nn.Module):
    def __init__(self,
                 encoder_config,
                 decoder_config,
                 n_embed=8192, 
                 embed_dim=32,
                 decay=0.99,
                 process_type='default',
                 quantize_kmeans_init=True,
                 decoder_out_dim=512,
                 rec_loss_type='cosine',
                 **kwargs
                 ):
        super().__init__()
        # print(kwargs)
        if decoder_config['in_chans'] != embed_dim:
            print(f"Rewrite the in_chans in decoder from {decoder_config['in_chans']} to {embed_dim}")
            decoder_config['in_chans'] = embed_dim
        
        # encoder & decode params
        print('Final encoder config', encoder_config)
        self.encoder = VisionTransformer(**encoder_config)

        print('Final decoder config', decoder_config)
        self.decoder = VisionTransformer(**decoder_config)
                
        self.quantize = NormEMAVectorQuantizer(
            n_embed=n_embed, embedding_dim=embed_dim, beta=1.0, kmeans_init=quantize_kmeans_init, decay=decay,
        )
        
        self.patch_size = encoder_config['patch_size']
        self.token_shape = (encoder_config['img_size'] // self.patch_size, encoder_config['img_size'] // self.patch_size)

        ## Teacher model setting
        # TODO hardcoding now
        self.teacher_model_type = 'open_clip_pix2struct'
        self.decoder_out_dim = decoder_out_dim
        if self.teacher_model_type == 'clip':
            self.scaling_layer = ScalingLayerForClip()
            self.teacher_model, _ = clip.load("ViT-B/16", device='cpu', jit=False, download_root="/zhangpai21/checkpoints/sdpm/")
            self.decoder_out_dim = 512
        elif self.teacher_model_type == 'dino':
            self.scaling_layer = ScalingLayerForIM()
            self.teacher_model = get_dino_vit_base()
            self.decoder_out_dim = 768
        elif self.teacher_model_type == 'open_clip_pix2struct':
            self.scaling_layer = ScalingLayerForBeit2()   # Data has already preprocessd as CLIP's type, so here we use
            self.teacher_model = Pix2StructOpenClipVemb()
            self.decoder_out_dim = 768
        else:
            self.teacher_model = None
        # print(self.teacher_model_type)

        # if self.teacher_model is not None:
        #     for param in self.teacher_model.parameters():
        #         param.requires_grad = False # fix teacher_model model

        #     self.teacher_model.eval()
        #     self.teacher_input_size = kwargs.get('teacher_input_size', 224)

        # task layer
        self.encode_task_layer = nn.Sequential(
            nn.Linear(encoder_config['embed_dim'], encoder_config['embed_dim']),
            nn.Tanh(),
            nn.Linear(encoder_config['embed_dim'], embed_dim) # for quantize
        )
        self.decode_task_layer = nn.Sequential(
            nn.Linear(decoder_config['embed_dim'], decoder_config['embed_dim']),
            nn.Tanh(),
            nn.Linear(decoder_config['embed_dim'], self.decoder_out_dim),
        )
        
        self.rec_loss_type = rec_loss_type

        print(f"process type for VQKD: {process_type}")
        self.process_type = process_type # in ['default', 'dall-e']
        self.logit_laplace_eps = 0.1
        self.kwargs = kwargs
        
        self.encode_task_layer.apply(self._init_weights)
        self.decode_task_layer.apply(self._init_weights)
    
    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
            
    # TODO hardcoding now
    # @torch.jit.ignore
    # def no_weight_decay(self):
    #     return {'quantize.embedding.weight', 'decoder.cls_token', 'decoder.pos_embed', 
    #             'encoder.cls_token', 'encoder.pos_embed'}

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'quantize.embedding.weight', 'decoder.cls_token', 'decoder.col_pos_emb', 'decoder.row_pos_emb', 
                'encoder.cls_token', 'encoder.col_pos_emb', 'encoder.row_pos_emb'}
    

    @property
    def device(self):
        return self.decoder.cls_token.device

    def pre_process(self, data):
        return data
        # TODO: modify data_fn
        if self.process_type == 'default':
            data = data.to(self.device)
            if data.max() <= 1.:
                data = data * 255.
            data = data / 127.5 - 1.0
        elif self.process_type == 'imagenet_norm':
            mean = torch.as_tensor(IMAGENET_DEFAULT_MEAN).to(self.device)[None, :, None, None]
            std = torch.as_tensor(IMAGENET_DEFAULT_STD).to(self.device)[None, :, None, None]
            data = (data - mean) / std
        return data
        
    def get_number_of_tokens(self):
        return self.quantize.n_e

    def get_tokens(self, data, **kwargs):
        
        data = self.pre_process(data)
        quantize, embed_ind, loss = self.encode(data)
        output = {}
        output['token'] = embed_ind.view(data.shape[0], -1)
        output['input_img'] = data

        return output

    def encode(self, x, position_ids, patch_shapes, seq_lens, pad_mask):
        encoder_features = self.encoder(x, position_ids, patch_shapes, seq_lens, pad_mask, return_patch_tokens=True)

        with torch.cuda.amp.autocast(enabled=False):
            to_quantizer_features = self.encode_task_layer(encoder_features.type_as(self.encode_task_layer[-1].weight))

        n = to_quantizer_features.shape[1]

        to_quantizer_features = rearrange(to_quantizer_features, 'b n c -> b c n', n=n) # reshape for quantizer
        quantize, loss, embed_ind = self.quantize(to_quantizer_features, position_ids)

        return quantize, embed_ind, loss
    
    def decode(self, quantize, position_ids, patch_shapes, seq_lens, pad_mask, **kwargs):
        # reshape tokens to feature maps for patch embed in decoder
        # quantize = rearrange(quantize, 'b (h w) c -> b c h w', h=self.token_shape[0], w=self.token_shape[1])
        decoder_features = self.decoder(quantize, position_ids, patch_shapes, seq_lens, pad_mask, return_patch_tokens=True)
        rec = self.decode_task_layer(decoder_features)

        return rec
    
    def get_codebook_indices(self, x, **kwargs):
        # for beit pre-training
        return self.get_tokens(x, **kwargs)['token']

    @torch.no_grad()
    def get_regress_target(self, x, position_ids=None, patch_shapes=None, seq_lens=None, pad_mask=None, **kwargs):

        if self.scaling_layer:
            norm_imgs = self.scaling_layer(x, pad_mask)
        else:
            norm_imgs = x

        if self.teacher_model_type == 'clip':
            target = self.teacher_model.encode_image(norm_imgs, return_all_tokens=True) @ self.teacher_model.visual.proj
        elif self.teacher_model_type == 'dino':
            target = self.teacher_model.forward(norm_imgs, return_patch_tokens=True)
        elif self.teacher_model_type == 'open_clip_pix2struct':
            target = self.teacher_model.dense_emb(norm_imgs, position_ids=position_ids, image_size=patch_shapes, seq_lens=seq_lens, pad_mask=pad_mask, format="normed_patch")
        else:
            raise NotImplementedError

        return target

    def calculate_rec_loss(self, rec, target, position_ids):  
        if self.rec_loss_type == 'cosine':
            pad_mask = torch.logical_not(torch.eq(position_ids[:, :, 0], -1))
            target = target / target.norm(dim=-1, keepdim=True)
            rec = rec / rec.norm(dim=-1, keepdim=True)
            loss = ((1 - (target * rec).sum(-1)) * pad_mask).sum(dim=-1)
            rec_loss = (loss / pad_mask.sum(dim=-1)).mean()
            # breakpoint()
            # rec_loss = ((1 - (target * rec).sum(-1)) * pad_mask).mean()
        else:
            raise NotImplementedError

        return rec_loss

    def forward(self, x, texts, position_ids, patch_shapes, seq_lens, pad_mask, **kwargs):
        """
        x: shape [B, npatch, 3*patch^2] in [0, 1]
        """
        # x = self.pre_process(x) # rescale to [-1, 1]
        
        # print(x.device)

        target = self.get_regress_target(x, position_ids=position_ids, patch_shapes=patch_shapes, seq_lens=seq_lens, pad_mask=pad_mask, **kwargs)

        quantize, embed_ind, emb_loss = self.encode(x, position_ids, patch_shapes, seq_lens, pad_mask)
        quantize = torch.permute(quantize, [0, 2, 1])
        # print(quantize.shape) # batch, codebook_dim, npatch
        xrec = self.decode(quantize, position_ids, patch_shapes, seq_lens, pad_mask)
        # print(xrec.shape)
        rec_loss = self.calculate_rec_loss(xrec, target, position_ids)
        loss = emb_loss + rec_loss

        log = {}
        split="train" if self.training else "val"
        log[f'{split}/quant_loss'] = emb_loss.detach().mean()
        log[f'{split}/rec_loss'] = rec_loss.detach().mean()
        log[f'{split}/total_loss'] = loss.detach().mean()

        return loss, log

class ScalingLayerForClip(nn.Module):
    def __init__(self):
        super(ScalingLayerForClip, self).__init__()
        self.register_buffer('shift', torch.Tensor([0.48145466, 0.4578275, 0.40821073])[None, :, None, None])
        self.register_buffer('scale', torch.Tensor([0.26862954, 0.26130258, 0.27577711])[None, :, None, None])

    def forward(self, inp):
        inp = ((inp + 1.) * 127.5).clamp(0, 255.) / 255. # rescale to [0, 1.]
        return (inp - self.shift) / self.scale

class ScalingLayerForIM(nn.Module):
    def __init__(self):
        super(ScalingLayerForIM, self).__init__()
        self.register_buffer('shift', torch.Tensor([0.485, 0.456, 0.406])[None, :, None, None]) # scale for tokenizer with default prosscess type \in [-1, 1]
        self.register_buffer('scale', torch.Tensor([0.229, 0.224, 0.225])[None, :, None, None])

    def forward(self, inp):
        inp = ((inp + 1.) * 127.5).clamp(0, 255.) / 255. # rescale to [0, 1.]
        return (inp - self.shift) / self.scale

class ScalingLayerForBeit2(nn.Module):
    def __init__(self):
        super(ScalingLayerForBeit2, self).__init__()
        # self.register_buffer('shift', torch.Tensor([0.485, 0.456, 0.406])[None, None, None, None, :]) # scale for tokenizer with default prosscess type \in [-1, 1]
        # self.register_buffer('scale', torch.Tensor([0.229, 0.224, 0.225])[None, None, None, None, :])
        self.register_buffer('shift', torch.Tensor([0.48145466, 0.4578275, 0.40821073])[None, None, None, None, :])
        self.register_buffer('scale', torch.Tensor([0.26862954, 0.26130258, 0.27577711])[None, None, None, None, :])

    def forward(self, inp, pad_mask):
        # inp [batch, npatch, 3*patch*patch]
        patch_size = int(math.sqrt(inp.shape[2] / 3))
        inp = inp.view(inp.shape[0], inp.shape[1], patch_size, patch_size, 3)
        inp = ((inp + 1.) * 127.5).clamp(0, 255.) / 255. # rescale to [0, 1.]
        eps = 1e-6
        # breakpoint()

        inp = (inp - self.shift) / self.scale
        inp = inp.view(inp.shape[0], inp.shape[1], patch_size ** 2 * 3)
        inp = inp * pad_mask.unsqueeze(-1)
        return inp

def get_model_default_params():
    return dict(img_size=224, patch_size=14, in_chans=3, num_classes=1000, embed_dim=768, depth=12, num_heads=12,  
                             mlp_ratio=4., qkv_bias=True,  qk_scale=None, drop_rate=0., attn_drop_rate=0., drop_path_rate=0., 
                             norm_layer=partial(nn.LayerNorm, eps=1e-6), init_values=0., use_abs_pos_emb=True, 
                             use_rel_pos_bias=False, use_shared_rel_pos_bias=False, use_mean_pooling=True, init_scale=0.001)

@register_model
def vqkd_encoder_base_decoder_1x768x12_clip(pretrained=False, pretrained_weight=None, as_tokenzer=False, img_size=224, 
                                            n_code=8192, code_dim=32, **kwargs):
    encoder_config, decoder_config = get_model_default_params(), get_model_default_params()
    
    # encoder settings
    encoder_config['img_size'] = img_size
    encoder_config['num_classes'] = 0
    # decoder settings
    decoder_config['img_size'] = img_size // decoder_config['patch_size']
    decoder_config['patch_size'] = 1
    decoder_config['in_chans'] = code_dim
    decoder_config['num_classes'] = 0
    decoder_config['depth'] = 1
    # teacher settings
    _ = kwargs.pop("teacher_model_type", "clip")

    teacher_model_type = 'clip' if not as_tokenzer else 'None'
    decoder_out_dim = 512

    model = VQKD(encoder_config, decoder_config, n_code, code_dim, teacher_model_type=teacher_model_type, 
                 decoder_out_dim=decoder_out_dim, **kwargs)

    if as_tokenzer:
        assert pretrained
        assert pretrained_weight is not None

        if pretrained_weight.startswith('https'):
            weights = torch.hub.load_state_dict_from_url(pretrained_weight, map_location='cpu', check_hash=True)
        else:
            weights = torch.load(pretrained_weight, map_location='cpu')
            
        if 'model' in weights:
            weights = weights['model']
        else:
            weights = weights["state_dict"]
        keys = list(weights.keys())
        
        for k in keys:
            if k.startswith("loss") or k.startswith("teacher") or k.startswith("scaling"):
                del weights[k]
        model.load_state_dict(weights)
    return model

@register_model
def vqkd_encoder_base_decoder_3x768x12_clip(pretrained=False, pretrained_weight=None, as_tokenzer=False, img_size=224, 
                                            n_code=8192, code_dim=32, **kwargs):
    encoder_config, decoder_config = get_model_default_params(), get_model_default_params()
    
    # encoder settings
    encoder_config['img_size'] = img_size
    encoder_config['num_classes'] = 0
    # decoder settings
    decoder_config['img_size'] = img_size // decoder_config['patch_size']
    decoder_config['patch_size'] = 1
    decoder_config['in_chans'] = code_dim
    decoder_config['num_classes'] = 0
    decoder_config['depth'] = 3
    # teacher settings
    _ = kwargs.pop("teacher_model_type", "clip")
    
    teacher_model_type = 'clip' if not as_tokenzer else 'None'
    decoder_out_dim = 512

    model = VQKD(encoder_config, decoder_config, n_code, code_dim, teacher_model_type=teacher_model_type, 
                 decoder_out_dim=decoder_out_dim, **kwargs)

    if as_tokenzer:
        assert pretrained
        assert pretrained_weight is not None

        if pretrained_weight.startswith('https'):
            weights = torch.hub.load_state_dict_from_url(pretrained_weight, map_location='cpu', check_hash=True)
        else:
            weights = torch.load(pretrained_weight, map_location='cpu')
        
        if 'model' in weights:
            weights = weights['model']
        else:
            weights = weights["state_dict"]
        keys = list(weights.keys())
        
        for k in keys:
            if k.startswith("loss") or k.startswith("teacher") or k.startswith("scaling"):
                del weights[k]
        model.load_state_dict(weights)
    return model


@register_model
def vqkd_encoder_base_decoder_1x768x12_dino(pretrained=False, pretrained_weight=None, as_tokenzer=False, img_size=224, 
                                            n_code=8192, code_dim=32, **kwargs):
    encoder_config, decoder_config = get_model_default_params(), get_model_default_params()
    
    # encoder settings
    encoder_config['img_size'] = img_size
    encoder_config['num_classes'] = 0
    # decoder settings
    decoder_config['img_size'] = img_size // decoder_config['patch_size']
    decoder_config['patch_size'] = 1
    decoder_config['in_chans'] = code_dim
    decoder_config['num_classes'] = 0
    decoder_config['depth'] = 1
    # teacher settings
    _ = kwargs.pop("teacher_model_type", "dino")

    teacher_model_type = 'dino' if not as_tokenzer else 'None'
    decoder_out_dim = 768

    model = VQKD(encoder_config, decoder_config, n_code, code_dim, teacher_model_type=teacher_model_type, 
                 decoder_out_dim=decoder_out_dim, **kwargs)

    if as_tokenzer:
        assert pretrained
        assert pretrained_weight is not None
        
        if pretrained_weight.startswith('https'):
            weights = torch.hub.load_state_dict_from_url(pretrained_weight, map_location='cpu', check_hash=True)
        else:
            weights = torch.load(pretrained_weight, map_location='cpu')
        
        if 'model' in weights:
            weights = weights['model']
        else:
            weights = weights["state_dict"]
        keys = list(weights.keys())
        
        for k in keys:
            if k.startswith("loss") or k.startswith("teacher") or k.startswith("scaling"):
                del weights[k]
        model.load_state_dict(weights)
    return model


if __name__ == '__main__':
    pass






