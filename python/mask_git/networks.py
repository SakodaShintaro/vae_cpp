# Copyright 2022 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# from https://github.com/google-research/maskgit/blob/main/maskgit/nets/vqgan_tokenizer.py

r"""MaskGIT Tokenizer based on VQGAN.

This tokenizer is a reimplementation of VQGAN [https://arxiv.org/abs/2012.09841]
with several modifications. The non-local layers are removed from VQGAN for
faster speed.
"""
from typing import Any

import flax.linen as nn
import jax
import jax.numpy as jnp
import losses
import layers
from quantizers import FSQ, FSQ_Level2


class ResBlock(nn.Module):
    """Basic Residual Block."""
    filters: int
    norm_fn: Any
    conv_fn: Any
    dtype: int = jnp.float32
    activation_fn: Any = nn.relu
    use_conv_shortcut: bool = False

    @nn.compact
    def __call__(self, x):
        input_dim = x.shape[-1]
        residual = x
        x = self.norm_fn()(x)
        x = self.activation_fn(x)
        x = self.conv_fn(self.filters, kernel_size=(3, 3), use_bias=False)(x)
        x = self.norm_fn()(x)
        x = self.activation_fn(x)
        x = self.conv_fn(self.filters, kernel_size=(3, 3), use_bias=False)(x)

        if input_dim != self.filters:
            if self.use_conv_shortcut:
                residual = self.conv_fn(
                    self.filters, kernel_size=(3, 3), use_bias=False)(
                        x)
            else:
                residual = self.conv_fn(
                    self.filters, kernel_size=(1, 1), use_bias=False)(
                        x)
        return x + residual


class Encoder(nn.Module):
    """Encoder Blocks."""

    train: bool
    dtype: int = jnp.float32

    def setup(self):
        self.filters = 128
        self.num_res_blocks = 2
        self.channel_multipliers = [1, 1, 2, 2, 4]
        self.embedding_dim = 10
        self.conv_downsample = False
        self.norm_type = "GN"
        self.activation_fn = nn.swish

    @nn.compact
    def __call__(self, x):
        conv_fn = nn.Conv
        norm_fn = layers.get_norm_layer(
            train=self.train, dtype=self.dtype, norm_type=self.norm_type)
        block_args = dict(
            norm_fn=norm_fn,
            conv_fn=conv_fn,
            dtype=self.dtype,
            activation_fn=self.activation_fn,
            use_conv_shortcut=False,
        )
        x = conv_fn(self.filters, kernel_size=(3, 3), use_bias=False)(x)
        num_blocks = len(self.channel_multipliers)
        for i in range(num_blocks):
            filters = self.filters * self.channel_multipliers[i]
            for _ in range(self.num_res_blocks):
                x = ResBlock(filters, **block_args)(x)
            if i < num_blocks - 1:
                if self.conv_downsample:
                    x = conv_fn(filters, kernel_size=(4, 4), strides=(2, 2))(x)
                else:
                    x = layers.dsample(x)
        for _ in range(self.num_res_blocks):
            x = ResBlock(filters, **block_args)(x)
        x = norm_fn()(x)
        x = self.activation_fn(x)
        x = conv_fn(self.embedding_dim, kernel_size=(1, 1))(x)
        return x


class Decoder(nn.Module):
    """Decoder Blocks."""

    train: bool
    output_dim: int = 3
    dtype: Any = jnp.float32

    def setup(self):
        self.filters = 128
        self.num_res_blocks = 2
        self.channel_multipliers = [1, 1, 2, 2, 4]
        self.norm_type = "GN"
        self.activation_fn = nn.swish

    @nn.compact
    def __call__(self, x):
        conv_fn = nn.Conv
        norm_fn = layers.get_norm_layer(
            train=self.train, dtype=self.dtype, norm_type=self.norm_type)
        block_args = dict(
            norm_fn=norm_fn,
            conv_fn=conv_fn,
            dtype=self.dtype,
            activation_fn=self.activation_fn,
            use_conv_shortcut=False,
        )
        num_blocks = len(self.channel_multipliers)
        filters = self.filters * self.channel_multipliers[-1]
        x = conv_fn(filters, kernel_size=(3, 3), use_bias=True)(x)
        for _ in range(self.num_res_blocks):
            x = ResBlock(filters, **block_args)(x)
        for i in reversed(range(num_blocks)):
            filters = self.filters * self.channel_multipliers[i]
            for _ in range(self.num_res_blocks):
                x = ResBlock(filters, **block_args)(x)
            if i > 0:
                x = layers.upsample(x, 2)
                x = conv_fn(filters, kernel_size=(3, 3))(x)
        x = norm_fn()(x)
        x = self.activation_fn(x)
        x = conv_fn(self.output_dim, kernel_size=(3, 3))(x)
        x = jax.nn.sigmoid(x)
        return x


class VectorQuantizer(nn.Module):
    """Basic vector quantizer."""
    train: bool
    dtype: int = jnp.float32

    @nn.compact
    def __call__(self, x):
        codebook_size = 1024
        codebook = self.param(
            "codebook",
            jax.nn.initializers.variance_scaling(
                scale=1.0, mode="fan_in", distribution="uniform"),
            (codebook_size, x.shape[-1]))
        codebook = jnp.asarray(codebook, dtype=self.dtype)
        distances = jnp.reshape(
            losses.squared_euclidean_distance(
                jnp.reshape(x, (-1, x.shape[-1])), codebook),
            x.shape[:-1] + (codebook_size,))
        encoding_indices = jnp.argmin(distances, axis=-1)
        encodings = jax.nn.one_hot(
            encoding_indices, codebook_size, dtype=self.dtype)
        quantized = self.quantize(encodings)
        result_dict = dict()
        if self.train:
            commitment_cost = 0.25
            e_latent_loss = jnp.mean((jax.lax.stop_gradient(quantized) - x) **
                                     2) * commitment_cost
            q_latent_loss = jnp.mean((quantized - jax.lax.stop_gradient(x))**2)
            entropy_loss = 0.0
            entropy_loss_ratio = 0.1
            entropy_temperature = 0.01
            entropy_loss_type = "softmax"
            if entropy_loss_ratio != 0:
                entropy_loss = losses.entropy_loss(
                    -distances,
                    loss_type=entropy_loss_type,
                    temperature=entropy_temperature
                ) * entropy_loss_ratio
            e_latent_loss = jnp.asarray(e_latent_loss, jnp.float32)
            q_latent_loss = jnp.asarray(q_latent_loss, jnp.float32)
            entropy_loss = jnp.asarray(entropy_loss, jnp.float32)
            loss = e_latent_loss + q_latent_loss + entropy_loss
            result_dict = dict(
                quantizer_loss=loss,
                e_latent_loss=e_latent_loss,
                q_latent_loss=q_latent_loss,
                entropy_loss=entropy_loss)
            quantized = x + jax.lax.stop_gradient(quantized - x)

        result_dict.update({
            "encodings": encodings,
            "encoding_indices": encoding_indices,
            "raw": x,
        })
        return quantized, result_dict

    def quantize(self, z: jnp.ndarray) -> jnp.ndarray:
        codebook = jnp.asarray(
            self.variables["params"]["codebook"], dtype=self.dtype)
        return jnp.dot(z, codebook)

    def get_codebook(self) -> jnp.ndarray:
        return jnp.asarray(self.variables["params"]["codebook"], dtype=self.dtype)

    def decode_ids(self, ids: jnp.ndarray) -> jnp.ndarray:
        codebook = self.variables["params"]["codebook"]
        return jnp.take(codebook, ids, axis=0)


class VQVAE(nn.Module):
    """VQVAE model."""
    train: bool
    dtype: int = jnp.float32
    activation_fn: Any = nn.relu

    def setup(self):
        """VQVAE setup."""
        # self.quantizer = VectorQuantizer(
        #     train=self.train, dtype=self.dtype)
        # self.quantizer = FSQ(
        #     levels=[3 for _ in range(10)]
        # )
        self.quantizer = FSQ_Level2(dim=10)

        output_dim = 3
        self.encoder = Encoder(train=self.train, dtype=self.dtype)
        self.decoder = Decoder(
            train=self.train,
            output_dim=output_dim,
            dtype=self.dtype)

    def encode(self, image):
        encoded_feature = self.encoder(image)
        quantized, result_dict = self.quantizer(encoded_feature)
        return quantized, result_dict

    def decode(self, x: jnp.ndarray) -> jnp.ndarray:
        reconstructed = self.decoder(x)
        return reconstructed

    def get_codebook(self):
        return self.quantizer.get_codebook()

    def decode_from_indices(self, inputs):
        if isinstance(inputs, dict):
            ids = inputs["encoding_indices"]
        else:
            ids = inputs
        features = self.quantizer.decode_ids(ids)
        reconstructed_image = self.decode(features)
        return reconstructed_image

    def encode_to_indices(self, inputs):
        if isinstance(inputs, dict):
            image = inputs["image"]
        else:
            image = inputs
        encoded_feature = self.encoder(image)
        _, result_dict = self.quantizer(encoded_feature)
        ids = result_dict["encoding_indices"]
        return ids

    def __call__(self, image):
        quantized, result_dict = self.encode(image)
        outputs = self.decoder(quantized)
        return outputs
