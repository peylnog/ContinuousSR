# modified from: https://github.com/thstkdgus35/EDSR-PyTorch
import math
from argparse import Namespace
import torch
import torch.nn as nn
import torch.nn.functional as F

from models import register


def default_conv(in_channels, out_channels, kernel_size, bias=True):
    return nn.Conv2d(
        in_channels, out_channels, kernel_size,
        padding=(kernel_size // 2), bias=bias)


class ResBlock(nn.Module):
    def __init__(
            self, conv, n_feats, kernel_size,
            bias=True, bn=False, act=nn.ReLU(True), res_scale=1):

        super(ResBlock, self).__init__()
        m = []
        for i in range(2):
            m.append(conv(n_feats, n_feats, kernel_size, bias=bias))
            if bn:
                m.append(nn.BatchNorm2d(n_feats))
            if i == 0:
                m.append(act)

        self.body = nn.Sequential(*m)
        self.res_scale = res_scale

    def forward(self, x):
        res = self.body(x).mul(self.res_scale)
        res += x

        return res


class Upsampler(nn.Sequential):
    def __init__(self, conv, scale, n_feats, bn=False, act=False, bias=True):

        m = []
        if (scale & (scale - 1)) == 0:
            for _ in range(int(math.log(scale, 2))):
                m.append(conv(n_feats, 4 * n_feats, 3, bias))
                m.append(nn.PixelShuffle(2))
                if bn:
                    m.append(nn.BatchNorm2d(n_feats))
                if act == 'relu':
                    m.append(nn.ReLU(True))
                elif act == 'prelu':
                    m.append(nn.PReLU(n_feats))

        elif scale == 3:
            m.append(conv(n_feats, 9 * n_feats, 3, bias))
            m.append(nn.PixelShuffle(3))
            if bn:
                m.append(nn.BatchNorm2d(n_feats))
            if act == 'relu':
                m.append(nn.ReLU(True))
            elif act == 'prelu':
                m.append(nn.PReLU(n_feats))
        else:
            raise NotImplementedError

        super(Upsampler, self).__init__(*m)


class MeanShift(nn.Conv2d):
    def __init__(
            self, rgb_range,
            rgb_mean=(0.4488, 0.4371, 0.4040), rgb_std=(1.0, 1.0, 1.0), sign=-1):
        super(MeanShift, self).__init__(3, 3, kernel_size=1)
        std = torch.Tensor(rgb_std)
        self.weight.data = torch.eye(3).view(3, 3, 1, 1) / std.view(3, 1, 1, 1)
        self.bias.data = sign * rgb_range * torch.Tensor(rgb_mean) / std
        for p in self.parameters():
            p.requires_grad = False


class EDSR(nn.Module):
    def __init__(self, args, conv=default_conv):
        super(EDSR, self).__init__()
        self.args = args
        n_resblocks = args.n_resblocks
        n_feats = args.n_feats
        kernel_size = 3
        scale = args.scale[0]
        act = nn.ReLU(True)

        # define head module
        m_head = [conv(args.n_colors, n_feats, kernel_size)]

        # define body module
        m_body = [
            ResBlock(
                conv, n_feats, kernel_size, act=act, res_scale=args.res_scale
            ) for _ in range(n_resblocks)
        ]
        m_body.append(conv(n_feats, n_feats, kernel_size))

        self.head = nn.Sequential(*m_head)
        self.body = nn.Sequential(*m_body)

        if args.no_upsampling:
            self.out_dim = n_feats
        else:
            self.out_dim = args.n_colors
            # define tail module
            m_tail = [
                Upsampler(conv, scale, n_feats, act=False),
                conv(n_feats, args.n_colors, kernel_size)
            ]
            self.tail = nn.Sequential(*m_tail)

        self.sub_mean = MeanShift(args.rgb_range)
        self.add_mean = MeanShift(args.rgb_range, sign=1)

        if args.pretrained_path:
            try:
                pretrained_dict = torch.load(args.pretrained_path)
                self.load_state_dict(pretrained_dict)
                print("Pretrained model loaded successfully.")
            except FileNotFoundError:
                print(f"File not found: {args.pretrained_path}")
            except RuntimeError as e:
                print(f"Error loading model: {e}")

    def forward(self, x):
        x = self.head(x)
        res = self.body(x)
        res += x

        if self.args.no_upsampling:
            x = res
        else:
            x = self.tail(res)
        return x

    def load_state_dict(self, state_dict, strict=True):
        own_state = self.state_dict()
        for name, param in state_dict.items():
            if name in own_state:
                if isinstance(param, nn.Parameter):
                    param = param.data
                try:
                    own_state[name].copy_(param)
                except RuntimeError:
                    if name.find('tail') == -1:
                        raise RuntimeError('While copying the parameter named {}, '
                                           'whose dimensions in the model are {} and '
                                           'whose dimensions in the checkpoint are {}.'
                                           .format(name, own_state[name].size(), param.size()))
            elif strict:
                if name.find('tail') == -1:
                    raise KeyError('unexpected key "{}" in state_dict'
                                   .format(name))



class Encoder(nn.Module):
    def __init__(self, args):
        super(Encoder, self).__init__()
        self.args = args
        self.edsr = EDSR(args)
        self.out_dim = args.n_feats

    def forward(self, x):
        x = self.edsr(x)
        return x


@register('edsr-baseline')
def make_encoder_baseline(n_resblocks=16, n_feats=64, res_scale=1, scale=2, no_upsampling=True, rgb_range=1, n_class=100):
    url = {
        'r16f64x2': 'models/weights/edsr_baseline_x2-1bc95232.pt',
        'r16f64x3': 'models/weights/edsr_baseline_x3-abf2a44e.pt',
        'r16f64x4': 'models/weights/edsr_baseline_x4-6b446fab.pt',
        'r32f256x2': 'models/weights/edsr_x2-0edfb8a3.pt',
        'r32f256x3': 'models/weights/edsr_x3-ea3ef2c6.pt',
        'r32f256x4': 'models/weights/edsr_x4-4f62e9ef.pt'
    }
    url_name = 'r{}f{}x{}'.format(n_resblocks, n_feats, scale)
    if url_name in url:
        url = url[url_name]
    else:
        url = None
    args = Namespace()
    args.n_resblocks = n_resblocks
    args.n_feats = n_feats
    args.res_scale = res_scale
    args.scale = [scale]
    args.no_upsampling = no_upsampling
    args.rgb_range = rgb_range
    args.n_colors = 3
    args.pretrained_path = url
    return Encoder(args)


@register('edsr-large')
def make_encoder_large(n_resblocks=32, n_feats=256, res_scale=0.1, scale=2, no_upsampling=True, rgb_range=1, n_class=100):
    url = {
        'r16f64x2': 'models/weights/edsr_baseline_x2-1bc95232.pt',
        'r16f64x3': 'models/weights/edsr_baseline_x3-abf2a44e.pt',
        'r16f64x4': 'models/weights/edsr_baseline_x4-6b446fab.pt',
        'r32f256x2': 'models/weights/edsr_x2-0edfb8a3.pt',
        'r32f256x3': 'models/weights/edsr_x3-ea3ef2c6.pt',
        'r32f256x4': 'models/weights/edsr_x4-4f62e9ef.pt'
    }
    url_name = 'r{}f{}x{}'.format(n_resblocks, n_feats, scale)
    if url_name in url:
        url = url[url_name]
    else:
        url = None
    args = Namespace()
    args.n_resblocks = n_resblocks
    args.n_feats = n_feats
    args.res_scale = res_scale
    args.scale = [scale]
    args.no_upsampling = no_upsampling
    args.rgb_range = rgb_range
    args.n_colors = 3
    args.pretrained_path = url
    return Encoder(args, n_class)


if __name__ == '__main__':
    network = make_encoder_baseline()
    a = torch.rand(1, 3, 48, 48)
    print(network(a)[0].shape, network(a)[1].shape)
