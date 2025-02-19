import os

import logging
import json

import numpy as np
import torch

from utils.util import min_max_normalize


class PriorPool:
    def __init__(
            self,
            lookup_table,
            arch_param_nums,
            generator,
            model,
            loader,
            CONFIG,
            bias=1):
        self.arch_param_nums = arch_param_nums
        self.CONFIG = CONFIG

        logging.info("============================= Prior pool ================================")
        if os.path.exists(self.CONFIG.path_to_prior_pool):
            logging.info("Load prior pool from {}".format(self.CONFIG.path_to_prior_pool))
            self.prior_pool = self._load_prior_pool()
        else:
            logging.info("Generate prior pool")
            self.prior_pool = self._generate_prior_pool(generator, model, loader, lookup_table, bias)

    def get_prior(self, flops):
        prior_keys = np.array([int(k) for k in self.prior_pool.keys()])
        prior_diff = np.absolute(prior_keys - flops)

        prior_index = prior_diff.argmin()
        prior = self.prior_pool[str(prior_keys[prior_index])]

        return torch.Tensor(prior)

    def get_prior_keys(self):
        return self.prior_pool.keys()

    def _load_prior_pool(self):
        prior_pool = None

        with open(self.CONFIG.path_to_prior_pool) as f:
            prior_pool = json.load(f)
        return prior_pool

    def save_prior_pool(self, path_to_prior_pool, prior_pool=None):
        if prior_pool is None:
            prior_pool = self.prior_pool

        with open(path_to_prior_pool, "w") as f:
            json.dump(prior_pool, f)

    # 采样符合flops要求的结构的onehot编码
    def _generate_prior_pool(
            self,
            generator,
            model,
            loader,
            lookup_table,
            bias=5):
        prior_pool = {}

        low_flops = self.CONFIG.low_flops
        high_flops = self.CONFIG.high_flops
        pool_interval = (high_flops - low_flops) // (self.CONFIG.pool_size + 1)                 # 20 80~120在poolsize仅有一个即目标为100的情况下

        for flops in range(                                                                     # 仅遍历一个100, 产生该flops限制下的先验
                low_flops + pool_interval,
                high_flops - 1,
                pool_interval):
            gen_flops, arch_param = self.generate_arch_param(lookup_table)                      # 随机生成一个结构，产生对应的one-hot编码
            #编码再解码一次是为了纠正选中skip的操作
            layers_config = lookup_table.decode_arch_param(arch_param)                          # 根据onehot编码生成具体的每层的结构参数，包括[扩张率，输出通道数，[选用的kernels]，stride，split_blocks,se]
            arch_param = lookup_table.encode_arch_param(layers_config)

            while gen_flops > flops + bias or gen_flops < flops - bias or len(layers_config) < 19:  # 不符合要求，反复采样
                gen_flops, arch_param = self.generate_arch_param(lookup_table)

                layers_config = lookup_table.decode_arch_param(arch_param)
                arch_param = lookup_table.encode_arch_param(layers_config)

            prior_pool[str(flops)] = arch_param.tolist()
            logging.info("Target flops {} : Prior generate {}".format(flops, gen_flops))

        self.save_prior_pool(self.CONFIG.path_to_prior_pool, prior_pool=prior_pool)

        return prior_pool

    # target: 采样结构one hot编码，查表得到对应的FLOPS
    def generate_arch_param(self, lookup_table, p=False):
        layers_num = len(self.CONFIG.l_cfgs)
        arch_param = torch.empty(layers_num, self.arch_param_nums // len(self.CONFIG.l_cfgs))       # 等于(self.CONFIG.split_blocks * self.CONFIG.kernels_nums) tensor(19,24)
        layers_expansion = np.random.randint(                                                       # 19层的2~6之间的数 tensor(1*19)
            low=2, high=self.CONFIG.expansion + 1, size=(layers_num))

        for i in range(len(arch_param)):
            architecture = [
                0 for i in range(
                    self.CONFIG.kernels_nums - 1)] + [1]                                            # [0, 0, 0, 1] tensor(1,4)
            arch_param[i] = torch.tensor(
                architecture * self.CONFIG.split_blocks)                                            # [0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1] tensor(1,24) 为每层的arch_param赋值
            for e in range(layers_expansion[i]):
                expansion_param = [0 for i in range(self.CONFIG.kernels_nums)]                      # [0,0,0,0]
                expansion_param[np.random.randint(0, self.CONFIG.kernels_nums - 1)] = 1             # 上面的随机选一位变为1，即选中该序号的位置
                arch_param[i][e * self.CONFIG.kernels_nums : (e + 1) * self.CONFIG.kernels_nums] = torch.tensor(expansion_param)# 将19层每层的被选中的扩张率(每层24代表6个扩张率，替换掉对应的4个数值)替换为上述的参数
        
        arch_param = lookup_table.get_validation_arch_param(arch_param) \
            if not p else lookup_table.calculate_block_probability(arch_param, tau=5)

        flops = lookup_table.get_model_flops(arch_param.cuda())                                     # 根据上述的结构参数查表生成flops
        return flops, arch_param
