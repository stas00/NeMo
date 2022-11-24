# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import List, Optional, Tuple
from collections import OrderedDict

import torch
from torch.autograd import Variable
from torch.nn import functional as F
from nemo.core.classes import NeuralModule, adapter_mixins, typecheck
from nemo.core.neural_types.elements import (
    EncodedRepresentation,
    MelSpectrogramType,
    Index,
    TokenDurationType
)
from nemo.core.neural_types.neural_type import NeuralType


class PartialConv1d(torch.nn.Conv1d):
    """
    Zero padding creates a unique identifier for where the edge of the data is, such that the model can almost always identify
    exactly where it is relative to either edge given a sufficient receptive field. Partial padding goes to some lengths to remove 
    this affect.
    """

    def __init__(self, *args, **kwargs):
        super(PartialConv1d, self).__init__(*args, **kwargs)
        weight_maskUpdater = torch.ones(1, 1, self.kernel_size[0])
        self.register_buffer("weight_maskUpdater", weight_maskUpdater, persistent=False)
        slide_winsize = torch.tensor(self.weight_maskUpdater.shape[1] * self.weight_maskUpdater.shape[2])
        self.register_buffer("slide_winsize", slide_winsize, persistent=False)

        if self.bias is not None:
            bias_view = self.bias.view(1, self.out_channels, 1)
            self.register_buffer('bias_view', bias_view, persistent=False)
        # caching part
        self.last_size = (-1, -1, -1)

        update_mask = torch.ones(1, 1, 1)
        self.register_buffer('update_mask', update_mask, persistent=False)
        mask_ratio = torch.ones(1, 1, 1)
        self.register_buffer('mask_ratio', mask_ratio, persistent=False)
        self.partial: bool = True

    def calculate_mask(self, input: torch.Tensor, mask_in: Optional[torch.Tensor]):
        with torch.no_grad():
            if mask_in is None:
                mask = torch.ones(1, 1, input.shape[2], dtype=input.dtype, device=input.device)
            else:
                mask = mask_in
            update_mask = F.conv1d(
                mask,
                self.weight_maskUpdater,
                bias=None,
                stride=self.stride,
                padding=self.padding,
                dilation=self.dilation,
                groups=1,
            )
            # for mixed precision training, change 1e-8 to 1e-6
            mask_ratio = self.slide_winsize / (update_mask + 1e-6)
            update_mask = torch.clamp(update_mask, 0, 1)
            mask_ratio = torch.mul(mask_ratio.to(update_mask), update_mask)
            return torch.mul(input, mask), mask_ratio, update_mask

    def forward_aux(self, input: torch.Tensor, mask_ratio: torch.Tensor, update_mask: torch.Tensor) -> torch.Tensor:
        assert len(input.shape) == 3

        raw_out = self._conv_forward(input, self.weight, self.bias)

        if self.bias is not None:
            output = torch.mul(raw_out - self.bias_view, mask_ratio) + self.bias_view
            output = torch.mul(output, update_mask)
        else:
            output = torch.mul(raw_out, mask_ratio)

        return output

    @torch.jit.ignore
    def forward_with_cache(self, input: torch.Tensor, mask_in: Optional[torch.Tensor] = None) -> torch.Tensor:
        use_cache = not (torch.jit.is_tracing() or torch.onnx.is_in_onnx_export())
        cache_hit = use_cache and mask_in is None and self.last_size == input.shape
        if cache_hit:
            mask_ratio = self.mask_ratio
            update_mask = self.update_mask
        else:
            input, mask_ratio, update_mask = self.calculate_mask(input, mask_in)
            if use_cache:
                # if a mask is input, or tensor shape changed, update mask ratio
                self.last_size = tuple(input.shape)
                self.update_mask = update_mask
                self.mask_ratio = mask_ratio
        return self.forward_aux(input, mask_ratio, update_mask)

    def forward_no_cache(self, input: torch.Tensor, mask_in: Optional[torch.Tensor] = None) -> torch.Tensor:
        if self.partial:
            input, mask_ratio, update_mask = self.calculate_mask(input, mask_in)
            return self.forward_aux(input, mask_ratio, update_mask)
        else:
            if mask_in is not None:
                input = torch.mul(input, mask_in)
            return self._conv_forward(input, self.weight, self.bias)

    def forward(self, input: torch.Tensor, mask_in: Optional[torch.Tensor] = None) -> torch.Tensor:
        if self.partial:
            return self.forward_with_cache(input, mask_in)
        else:
            if mask_in is not None:
                input = torch.mul(input, mask_in)
            return self._conv_forward(input, self.weight, self.bias)


class LinearNorm(torch.nn.Module):
    def __init__(self, in_dim, out_dim, bias=True, w_init_gain='linear'):
        super().__init__()
        self.linear_layer = torch.nn.Linear(in_dim, out_dim, bias=bias)

        torch.nn.init.xavier_uniform_(self.linear_layer.weight, gain=torch.nn.init.calculate_gain(w_init_gain))

    def forward(self, x):
        return self.linear_layer(x)


class ConvNorm(torch.nn.Module, adapter_mixins.AdapterModuleMixin):
    def __init__(
        self,
        in_channels,
        out_channels,
        kernel_size=1,
        stride=1,
        padding=None,
        dilation=1,
        bias=True,
        w_init_gain='linear',
        use_partial_padding: bool = False,
        use_weight_norm: bool = False,
        norm_fn=None,
    ):
        super(ConvNorm, self).__init__()
        if padding is None:
            assert kernel_size % 2 == 1
            padding = int(dilation * (kernel_size - 1) / 2)
        self.use_partial_padding: bool = use_partial_padding
        conv = PartialConv1d(
            in_channels,
            out_channels,
            kernel_size=kernel_size,
            stride=stride,
            padding=padding,
            dilation=dilation,
            bias=bias,
        )
        conv.partial = use_partial_padding
        torch.nn.init.xavier_uniform_(conv.weight, gain=torch.nn.init.calculate_gain(w_init_gain))
        if use_weight_norm:
            conv = torch.nn.utils.weight_norm(conv)
        if norm_fn is not None:
            self.norm = norm_fn(out_channels, affine=True)
        else:
            self.norm = None
        self.conv = conv

    def forward(self, input: torch.Tensor, mask_in: Optional[torch.Tensor] = None) -> torch.Tensor:
        ret = self.conv(input, mask_in)
        if self.norm is not None:
            ret = self.norm(ret)

        if self.is_adapter_available():
            ret = self.forward_enabled_adapters(ret.transpose(1,2)).transpose(1, 2)
        return ret


class LocationLayer(torch.nn.Module):
    def __init__(self, attention_n_filters, attention_kernel_size, attention_dim):
        super().__init__()
        padding = int((attention_kernel_size - 1) / 2)
        self.location_conv = ConvNorm(
            2,
            attention_n_filters,
            kernel_size=attention_kernel_size,
            padding=padding,
            bias=False,
            stride=1,
            dilation=1,
        )
        self.location_dense = LinearNorm(attention_n_filters, attention_dim, bias=False, w_init_gain='tanh')

    def forward(self, attention_weights_cat):
        processed_attention = self.location_conv(attention_weights_cat)
        processed_attention = processed_attention.transpose(1, 2)
        processed_attention = self.location_dense(processed_attention)
        return processed_attention


class Attention(torch.nn.Module):
    def __init__(
        self,
        attention_rnn_dim,
        embedding_dim,
        attention_dim,
        attention_location_n_filters,
        attention_location_kernel_size,
    ):
        super().__init__()
        self.query_layer = LinearNorm(attention_rnn_dim, attention_dim, bias=False, w_init_gain='tanh')
        self.memory_layer = LinearNorm(embedding_dim, attention_dim, bias=False, w_init_gain='tanh')
        self.v = LinearNorm(attention_dim, 1, bias=False)
        self.location_layer = LocationLayer(
            attention_location_n_filters, attention_location_kernel_size, attention_dim,
        )
        self.score_mask_value = -float("inf")

    def get_alignment_energies(self, query, processed_memory, attention_weights_cat):
        """
        PARAMS
        ------
        query: decoder output (batch, n_mel_channels * n_frames_per_step)
        processed_memory: processed encoder outputs (B, T_in, attention_dim)
        attention_weights_cat: cumulative and prev. att weights (B, 2, max_time)
        RETURNS
        -------
        alignment (batch, max_time)
        """

        processed_query = self.query_layer(query.unsqueeze(1))
        processed_attention_weights = self.location_layer(attention_weights_cat)
        energies = self.v(torch.tanh(processed_query + processed_attention_weights + processed_memory))

        energies = energies.squeeze(-1)
        return energies

    def forward(
        self, attention_hidden_state, memory, processed_memory, attention_weights_cat, mask,
    ):
        """
        PARAMS
        ------
        attention_hidden_state: attention rnn last output
        memory: encoder outputs
        processed_memory: processed encoder outputs
        attention_weights_cat: previous and cummulative attention weights
        mask: binary mask for padded data
        """
        alignment = self.get_alignment_energies(attention_hidden_state, processed_memory, attention_weights_cat)

        if mask is not None:
            alignment.data.masked_fill_(mask, self.score_mask_value)

        attention_weights = F.softmax(alignment, dim=1)
        attention_context = torch.bmm(attention_weights.unsqueeze(1), memory)
        attention_context = attention_context.squeeze(1)

        return attention_context, attention_weights


class Prenet(torch.nn.Module):
    def __init__(self, in_dim, sizes, p_dropout=0.5):
        super().__init__()
        in_sizes = [in_dim] + sizes[:-1]
        self.p_dropout = p_dropout
        self.layers = torch.nn.ModuleList(
            [LinearNorm(in_size, out_size, bias=False) for (in_size, out_size) in zip(in_sizes, sizes)]
        )

    def forward(self, x, inference=False):
        if inference:
            for linear in self.layers:
                x = F.relu(linear(x))
                x0 = x[0].unsqueeze(0)
                mask = torch.autograd.Variable(torch.bernoulli(x0.data.new(x0.data.size()).fill_(1 - self.p_dropout)))
                mask = mask.expand(x.size(0), x.size(1))
                x = x * mask * 1 / (1 - self.p_dropout)
        else:
            for linear in self.layers:
                x = F.dropout(F.relu(linear(x)), p=self.p_dropout, training=True)
        return x


def fused_add_tanh_sigmoid_multiply(input_a, input_b, n_channels_int):
    in_act = input_a + input_b
    t_act = torch.tanh(in_act[:, :n_channels_int, :])
    s_act = torch.sigmoid(in_act[:, n_channels_int:, :])
    acts = t_act * s_act
    return acts


class Invertible1x1Conv(torch.nn.Module):
    """
    The layer outputs both the convolution, and the log determinant
    of its weight matrix.  If reverse=True it does convolution with
    inverse
    """

    def __init__(self, c):
        super().__init__()
        self.conv = torch.nn.Conv1d(c, c, kernel_size=1, stride=1, padding=0, bias=False)

        # Sample a random orthonormal matrix to initialize weights
        W = torch.linalg.qr(torch.FloatTensor(c, c).normal_())[0]

        # Ensure determinant is 1.0 not -1.0
        if torch.det(W) < 0:
            W[:, 0] = -1 * W[:, 0]
        W = W.view(c, c, 1)
        self.conv.weight.data = W
        self.inv_conv = None

    def forward(self, z, reverse: bool = False):
        if reverse:
            if self.inv_conv is None:
                # Inverse convolution - initialized here only for backwards
                # compatibility with weights from existing checkpoints.
                # Should be moved to init() with next incompatible change.
                self.inv_conv = torch.nn.Conv1d(
                    self.conv.in_channels, self.conv.out_channels, kernel_size=1, stride=1, padding=0, bias=False
                )
                W_inverse = self.conv.weight.squeeze().data.float().inverse()
                W_inverse = Variable(W_inverse[..., None])
                self.inv_conv.weight.data = W_inverse
                self.inv_conv.to(device=self.conv.weight.device, dtype=self.conv.weight.dtype)
            return self.inv_conv(z)
        else:
            # Forward computation
            # shape
            W = self.conv.weight.squeeze()
            batch_size, group_size, n_of_groups = z.size()
            log_det_W = batch_size * n_of_groups * torch.logdet(W.float())
            z = self.conv(z)
            return (
                z,
                log_det_W,
            )


class WaveNet(torch.nn.Module):
    """
    This is the WaveNet like layer for the affine coupling.  The primary
    difference from WaveNet is the convolutions need not be causal.  There is
    also no dilation size reset.  The dilation only doubles on each layer
    """

    def __init__(self, n_in_channels, n_mel_channels, n_layers, n_channels, kernel_size):
        super().__init__()
        assert kernel_size % 2 == 1
        assert n_channels % 2 == 0
        self.n_layers = n_layers
        self.n_channels = n_channels
        self.in_layers = torch.nn.ModuleList()
        self.res_skip_layers = torch.nn.ModuleList()

        start = torch.nn.Conv1d(n_in_channels, n_channels, 1)
        start = torch.nn.utils.weight_norm(start, name='weight')
        self.start = start

        # Initializing last layer to 0 makes the affine coupling layers
        # do nothing at first.  This helps with training stability
        end = torch.nn.Conv1d(n_channels, 2 * n_in_channels, 1)
        end.weight.data.zero_()
        end.bias.data.zero_()
        self.end = end

        cond_layer = torch.nn.Conv1d(n_mel_channels, 2 * n_channels * n_layers, 1)
        self.cond_layer = torch.nn.utils.weight_norm(cond_layer, name='weight')

        for i in range(n_layers):
            dilation = 2 ** i
            padding = int((kernel_size * dilation - dilation) / 2)
            in_layer = torch.nn.Conv1d(n_channels, 2 * n_channels, kernel_size, dilation=dilation, padding=padding,)
            in_layer = torch.nn.utils.weight_norm(in_layer, name='weight')
            self.in_layers.append(in_layer)

            # last one is not necessary
            if i < n_layers - 1:
                res_skip_channels = 2 * n_channels
            else:
                res_skip_channels = n_channels
            res_skip_layer = torch.nn.Conv1d(n_channels, res_skip_channels, 1)
            res_skip_layer = torch.nn.utils.weight_norm(res_skip_layer, name='weight')
            self.res_skip_layers.append(res_skip_layer)

    def forward(self, forward_input: Tuple[torch.Tensor, torch.Tensor]):
        audio, spect = forward_input[0], forward_input[1]
        audio = self.start(audio)
        output = torch.zeros_like(audio)

        spect = self.cond_layer(spect)

        for i in range(self.n_layers):
            spect_offset = i * 2 * self.n_channels
            acts = fused_add_tanh_sigmoid_multiply(
                self.in_layers[i](audio),
                spect[:, spect_offset : spect_offset + 2 * self.n_channels, :],
                self.n_channels,
            )

            res_skip_acts = self.res_skip_layers[i](acts)
            if i < self.n_layers - 1:
                audio = audio + res_skip_acts[:, : self.n_channels, :]
                output = output + res_skip_acts[:, self.n_channels :, :]
            else:
                output = output + res_skip_acts

        return self.end(output)


class WeightedSpeakerEmbedding(torch.nn.Module):
    def __init__(self, pretrained_embedding, speaker_list=[]):
        super().__init__()
        if len(speaker_list) > 0:
            self.pretrained_embedding = torch.nn.Parameter(pretrained_embedding.weight[speaker_list].detach().clone())
        else:
            self.pretrained_embedding = torch.nn.Parameter(pretrained_embedding.weight.detach().clone())
        self.pretrained_embedding.requires_grad = False
        self.num_embeddings = self.pretrained_embedding.size()[0]
        self.embedding_weight = torch.nn.Parameter(torch.ones(1, self.num_embeddings))

    def forward(self, speaker):
        weight = self.embedding_weight.repeat(len(speaker), 1)
        weight = torch.nn.functional.softmax(weight, dim=-1)
        speaker_emb = weight @ self.pretrained_embedding
        return speaker_emb


"""
Global Style Token based Speaker Embedding
"""
class GlobalStyleToken(NeuralModule):
    def __init__(self, 
                 cnn_filters=[32, 32, 64, 64, 128, 128], 
                 dropout=0.2, 
                 gru_hidden=128,
                 gst_size=128, 
                 n_style_token=10, 
                 n_style_attn_head=4):
        super(GlobalStyleToken, self).__init__()    
        self.reference_encoder = ReferenceEncoderUtteranceLevel(cnn_filters=list(cnn_filters), dropout=dropout, gru_hidden=gru_hidden)
        self.style_attention = StyleAttention(gru_hidden=gru_hidden, gst_size=gst_size, n_style_token=n_style_token, n_style_attn_head=n_style_attn_head)

    @property
    def input_types(self):
        return {
            "inp":NeuralType(('B', 'D', 'T_spec'), MelSpectrogramType()),
            "inp_mask":NeuralType(('B', 'T_spec', 1), TokenDurationType()),
        }

    @property
    def output_types(self):
        return {
            "gst": NeuralType(('B', 'D'), EncodedRepresentation()),
        }

    def forward(self, inp, inp_mask):
        style_embedding = self.reference_encoder(inp, inp_mask)
        gst = self.style_attention(style_embedding)
        return gst


class ReferenceEncoderUtteranceLevel(NeuralModule):
    def __init__(self, cnn_filters=[32, 32, 64, 64, 128, 128], dropout=0.2, gru_hidden=128):
        super(ReferenceEncoderUtteranceLevel, self).__init__()
        self.filter_size = [1] + cnn_filters
        self.dropout = dropout
        self.conv = torch.nn.Sequential(
            OrderedDict(
                [
                    module
                    for i in range(len(cnn_filters))
                    for module in (
                        (
                            "conv2d_{}".format(i + 1),
                            Conv2d(
                                in_channels=int(self.filter_size[i]),
                                out_channels=int(self.filter_size[i + 1]),
                                kernel_size=(3, 3),
                                stride=(2, 2),
                                padding=(1, 1),
                            ),
                        ),
                        ("relu_{}".format(i + 1), torch.nn.ReLU()),
                        (
                            "layer_norm_{}".format(i + 1),
                            torch.nn.LayerNorm(self.filter_size[i + 1]),
                        ),
                        ("dropout_{}".format(i + 1), torch.nn.Dropout(self.dropout)),
                    )
                ]
            )
        )

        self.gru = torch.nn.GRU(
            input_size=cnn_filters[-1] * 2,
            hidden_size=gru_hidden,
            batch_first=True,
        )

    @property
    def input_types(self):
        return {
            "inputs": NeuralType(('B', 'D', 'T_spec'), MelSpectrogramType()),
            "inputs_masks": NeuralType(('B', 'T_spec', 1), TokenDurationType()),
        }

    @property
    def output_types(self):
        return {
            "out": NeuralType(('B', 'D'), EncodedRepresentation()),
        }

    def forward(self, inputs, inputs_masks):
        inputs = inputs.transpose(1,2)

        inputs = inputs * inputs_masks
        out = inputs.unsqueeze(3)
        out = self.conv(out)
        out = out.view(out.shape[0], out.shape[1], -1).contiguous()
        self.gru.flatten_parameters()
        _, out = self.gru(out)

        return out.squeeze(0)


class StyleAttention(NeuralModule):
    def __init__(self, gru_hidden=128, gst_size=128, n_style_token=10, n_style_attn_head=4):
        super(StyleAttention, self).__init__()
        self.input_size = gru_hidden
        self.output_size = gst_size
        self.n_token = n_style_token
        self.n_head = n_style_attn_head
        self.token_size = self.output_size // self.n_head

        self.tokens = torch.nn.Parameter(torch.FloatTensor(self.n_token, self.token_size))

        self.q_linear = torch.nn.Linear(self.input_size, self.output_size)
        self.k_linear = torch.nn.Linear(self.token_size, self.output_size)
        self.v_linear = torch.nn.Linear(self.token_size, self.output_size)

        self.tanh = torch.nn.Tanh()
        self.softmax = torch.nn.Softmax(dim=2)
        self.temperature = (self.output_size // self.n_head) ** 0.5
        torch.nn.init.normal_(self.tokens)

    @property
    def input_types(self):
        return {
            "inputs": NeuralType(('B', 'D'), EncodedRepresentation()),
            "token_id": NeuralType(('B'), Index(), optional=True),
        }

    @property
    def output_types(self):
        return {
            "style_emb": NeuralType(('B', 'D'), EncodedRepresentation()),
        }

    def forward(self, inputs, token_id=None):
        bs = inputs.size(0)
        q = self.q_linear(inputs.unsqueeze(1))
        k = self.k_linear(self.tanh(self.tokens).unsqueeze(0).expand(bs, -1, -1))
        v = self.v_linear(self.tanh(self.tokens).unsqueeze(0).expand(bs, -1, -1))

        q = q.view(bs, q.shape[1], self.n_head, self.token_size)
        k = k.view(bs, k.shape[1], self.n_head, self.token_size)
        v = v.view(bs, v.shape[1], self.n_head, self.token_size)

        q = q.permute(2, 0, 1, 3).contiguous().view(-1, q.shape[1], q.shape[3])
        k = k.permute(2, 0, 3, 1).contiguous().view(-1, k.shape[3], k.shape[1])
        v = v.permute(2, 0, 1, 3).contiguous().view(-1, v.shape[1], v.shape[3])

        scores = torch.bmm(q, k) / self.temperature
        scores = self.softmax(scores)
        if token_id is not None:
            scores = torch.zeros_like(scores)
            scores[:, :, token_id] = 1

        style_emb = torch.bmm(scores, v).squeeze(1)
        style_emb = style_emb.contiguous().view(self.n_head, bs, self.token_size)
        style_emb = style_emb.permute(1, 0, 2).contiguous().view(bs, -1)

        return style_emb


class Conv2d(torch.nn.Module):
    """
    Convolution 2D Module
    """

    def __init__(
        self,
        in_channels,
        out_channels,
        kernel_size=(1, 1),
        stride=(1, 1),
        padding=(0, 0),
        dilation=(1, 1),
        bias=True,
        w_init="linear",
    ):
        """
        :param in_channels: dimension of input
        :param out_channels: dimension of output
        :param kernel_size: size of kernel
        :param stride: size of stride
        :param padding: size of padding
        :param dilation: dilation rate
        :param bias: boolean. if True, bias is included.
        :param w_init: str. weight inits with xavier initialization.
        """
        super(Conv2d, self).__init__()

        self.conv = torch.nn.Conv2d(
            in_channels,
            out_channels,
            kernel_size=kernel_size,
            stride=stride,
            padding=padding,
            dilation=dilation,
            bias=bias,
        )

    def forward(self, x):
        x = x.contiguous().transpose(1, 3)
        x = x.contiguous().transpose(2, 3)
        x = self.conv(x)
        x = x.contiguous().transpose(2, 3)
        x = x.contiguous().transpose(1, 3)
        return x