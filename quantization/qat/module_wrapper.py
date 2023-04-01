import os
import torch.nn as nn
import torch
from quantization.methods.uniform import UniformQuantization, MinMaxQuantization
from quantization.methods.clipped_uniform import LearnedStepSizeQuantization, MaxAbsStaticQuantization
from quantization.methods.non_uniform import LearnableDifferentiableQuantization, KmeansQuantization, LearnedCentroidsQuantization
from quantization.methods.stochastic import Noise
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np


quantization_mapping = {'max_static': MaxAbsStaticQuantization,
                        'min_max': MinMaxQuantization,
                        'lsq': LearnedStepSizeQuantization
                        }


def plot_binning_hist(x, y, min, max):
    fig, ax = plt.subplots()
    x = np.array([float("{:.3f}".format(d)) for d in x])
    ax.errorbar(x, y, xerr=[x - min, max - x], fmt='o', ecolor='r', capsize=10, capthick=2)
    plt.bar(min, y, width=max - min, edgecolor='k', align='edge', alpha=0.5)
    ax.legend([str(x[x.argsort()]), str(y[x.argsort()])], fontsize=12, loc='upper center',
              bbox_to_anchor=(0.5, -0.05), shadow=True, ncol=2, fancybox=True)
    plt.yticks(size=12)
    plt.xlabel('binning', size=16)
    plt.ylabel('values in bin', size=16)

    return fig


def plot_centroids(centroids):
    fig, ax = plt.subplots()
    for i in range(centroids.shape[1]):
        plt.plot(np.arange(centroids.shape[0]), centroids[:, i])

    plt.xticks(size=14)
    plt.yticks(size=14)
    plt.xlabel('epoch', size=16)
    plt.ylabel('centroids', size=16)
    return fig


def plot_tensor_binning(tensor, B, v, name, step, ml_logger):
    x = v.cpu().numpy().flatten()
    y = torch.sum(B, dim=0).cpu().numpy()

    max = np.empty(B.shape[1])
    min = np.empty(B.shape[1])
    for i in range(B.shape[1]):
        b = tensor[B[:, i].type(torch.bool)]
        max[i] = b.max().cpu().item()
        min[i] = b.min().cpu().item()

    fig = plot_binning_hist(x, y, min, max)
    ml_logger.tf_logger.add_figure(name + '.binning', fig, step)


def is_positive(module):
    return isinstance(module, nn.ReLU) or isinstance(module, nn.ReLU6)


class ActivationModuleWrapper(nn.Module):
    def __init__(self, name, wrapped_module, **kwargs):
        super(ActivationModuleWrapper, self).__init__()
        self.name = name
        self.wrapped_module = wrapped_module
        self.optimizer_bridge = kwargs['optim_bridge']
        self.bits_out = kwargs['bits_out']
        self.qtype = kwargs['qtype']
        self.enabled = True
        self.active = True
        self.truck_stats = kwargs['log_stats']

        if self.bits_out is not None:
            self.out_quantization = self.out_quantization_default = None

            def __init_out_quantization__(tensor):
                self.out_quantization_default = quantization_mapping[self.qtype](self, tensor, self.bits_out,
                                                                                  symmetric=(not is_positive(wrapped_module)),
                                                                                  uint=True, kwargs=kwargs)
                self.out_quantization = self.out_quantization_default
                self.optimizer_bridge.add_quantization_params(self.out_quantization.optim_parameters())

                print("ActivationModuleWrapperPost - {} | {} | {}".format(self.name, str(self.out_quantization),
                                                                          str(tensor.device)))

            self.out_quantization_init_fn = __init_out_quantization__

    def load_state_dict(self, state_dict):
        if hasattr(self, 'out_quantization'):
            t = state_dict[list(state_dict.keys())[0]].new_tensor([0.], dtype=torch.float)
            q = LearnedStepSizeQuantization(self, t, self.bits_out, symmetric=(not is_positive(self.wrapped_module)),
                                                                              uint=True, kwargs=None)

            for lp in q.learned_parameters():
                pname = self.name + '.' + lp
                if pname in state_dict:
                    if self.out_quantization is None:
                        self.out_quantization_default = self.out_quantization = q
                    getattr(self.out_quantization, lp).data = state_dict[pname]

    def __enabled__(self):
        return self.enabled and self.active and self.bits_out is not None

    def forward(self, *input):
        out = self.wrapped_module(*input)

        # Quantize output
        if self.__enabled__():
            self.verify_initialized(self.out_quantization, out, self.out_quantization_init_fn)
            out = self.out_quantization(out)

        if self.truck_stats:
            from utils.stats_trucker import StatsTrucker as ST
            st = ST()
            x = out.transpose(0, 1).contiguous().view(out.shape[1], -1)
            mu = x.mean(1)
            sigma = x.std(1)
            st.add('relu_mean', self.name, mu)
            st.add('relu_var', self.name, sigma ** 2)
            st.add('relu_skewness', self.name, torch.mean(((x - mu.view(-1, 1)) / sigma.view(-1, 1)) ** 3, dim=1))
            st.add('relu_kurtosis', self.name, torch.mean(((x - mu.view(-1, 1)) / sigma.view(-1, 1)) ** 4, dim=1))
            st.add('relu_m5', self.name, torch.mean(((x - mu.view(-1, 1)) / sigma.view(-1, 1)) ** 5, dim=1))
            st.add('relu_m6', self.name, torch.mean(((x - mu.view(-1, 1)) / sigma.view(-1, 1)) ** 6, dim=1))
            st.add('relu_m7', self.name, torch.mean(((x - mu.view(-1, 1)) / sigma.view(-1, 1)) ** 7, dim=1))
            st.add('relu_m8', self.name, torch.mean(((x - mu.view(-1, 1)) / sigma.view(-1, 1)) ** 8, dim=1))

        return out

    def set_quant_method(self, method=None):
        if self.bits_out is not None:
            if method == 'kmeans':
                self.out_quantization_outer = KmeansQuantization(self.bits_out, max_iter=3)
            else:
                self.out_quantization_outer = self.out_quantization_outer_default

    def verify_initialized(self, quantization_handle, tensor, init_fn):
        if quantization_handle is None:
            init_fn(tensor)

    def log_state(self, step, ml_logger):
        if self.__enabled__():
            if self.out_quantization is not None:
                for n, p in self.out_quantization.named_parameters():
                    if p.numel() == 1:
                        ml_logger.log_metric(self.name + '.' + n, p.item(),  step='auto')
                    else:
                        for i, e in enumerate(p):
                            ml_logger.log_metric(self.name + '.' + n + '.' + str(i), e.item(),  step='auto')


class ParameterModuleWrapper(nn.Module):
    def __init__(self, name, wrapped_module, **kwargs):
        super(ParameterModuleWrapper, self).__init__()
        self.name = name
        self.wrapped_module = wrapped_module
        self.optimizer_bridge = kwargs['optim_bridge']
        self.forward_functor = kwargs['forward_functor']
        self.bit_weights = kwargs['bits_weight']
        self.bits_out = kwargs['bits_out']
        self.qtype = kwargs['qtype']
        self.enabled = True
        self.active = True
        self.centroids_hist = {}
        self.log_weight_hist = False
        self.log_mse = False
        self.log_clustering = False
        self.bn = kwargs['bn'] if 'bn' in kwargs else None
        self.truck_stats = False

        setattr(self, 'weight', wrapped_module.weight)
        setattr(self, 'bias', wrapped_module.bias)
        delattr(wrapped_module, 'weight')
        delattr(wrapped_module, 'bias')

        if self.bit_weights is not None:
            self.weight_quantization_default = quantization_mapping[self.qtype](self, self.weight,
                                                                           self.bit_weights, symmetric=True,
                                                                           uint=True, kwargs=kwargs)

            self.weight_quantization = self.weight_quantization_default
            if hasattr(self.weight_quantization, 'optim_parameters'):
                self.optimizer_bridge.add_quantization_params(self.weight_quantization.optim_parameters())

            print("ParameterModuleWrapperPost - {} | {} | {}".format(self.name, str(self.weight_quantization),
                                                                      str(self.weight.device)))

    def load_state_dict(self, state_dict):
        if hasattr(self, 'weight_quantization'):
            for lp in self.weight_quantization.learned_parameters():
                pname = self.name + '.' + lp
                if pname in state_dict:
                    getattr(self.weight_quantization, lp).data = state_dict[pname]

    def __enabled__(self):
        return self.enabled and self.active and self.bit_weights is not None

    def forward(self, *input):
        w = self.weight
        if self.__enabled__():
            # Quantize weights
            w = self.weight_quantization(w)

        out = self.forward_functor(*input, weight=w, bias=(self.bias if hasattr(self, 'bias') else None))

        if self.truck_stats:
            from utils.stats_trucker import StatsTrucker as ST
            st = ST()
            x = out.transpose(0, 1).contiguous().view(out.shape[1], -1)
            mu = x.mean(1)
            sigma = x.std(1)
            st.add('mean', self.name, mu)
            st.add('var', self.name, sigma ** 2)
            st.add('skewness', self.name, torch.mean(((x - mu.view(-1, 1)) / sigma.view(-1, 1)) ** 3, dim=1))
            st.add('kurtosis', self.name, torch.mean(((x - mu.view(-1, 1)) / sigma.view(-1, 1)) ** 4, dim=1))
            st.add('m5', self.name, torch.mean(((x - mu.view(-1, 1)) / sigma.view(-1, 1)) ** 5, dim=1))
            st.add('m6', self.name, torch.mean(((x - mu.view(-1, 1)) / sigma.view(-1, 1)) ** 6, dim=1))
            st.add('m7', self.name, torch.mean(((x - mu.view(-1, 1)) / sigma.view(-1, 1)) ** 7, dim=1))
            st.add('m8', self.name, torch.mean(((x - mu.view(-1, 1)) / sigma.view(-1, 1)) ** 8, dim=1))
            st.add('cv', self.name, sigma / mu)

        return out

    def set_quant_method(self, method=None):
        if self.bit_weights is not None:
            if method is None:
                self.weight_quantization = self.weight_quantization_default
            elif method == 'kmeans':
                self.weight_quantization = KmeansQuantization(self.bit_weights)
            else:
                self.weight_quantization = self.weight_quantization_default

    # TODO: make it more generic
    def set_quant_mode(self, mode=None):
        if self.bit_weights is not None:
            if mode is not None:
                self.soft = self.weight_quantization.soft_quant
                self.hard = self.weight_quantization.hard_quant
            if mode is None:
                self.weight_quantization.soft_quant = self.soft
                self.weight_quantization.hard_quant = self.hard
            elif mode == 'soft':
                self.weight_quantization.soft_quant = True
                self.weight_quantization.hard_quant = False
            elif mode == 'hard':
                self.weight_quantization.soft_quant = False
                self.weight_quantization.hard_quant = True

    def log_state(self, step, ml_logger):
        if self.__enabled__():
            if self.weight_quantization is not None:
                for n, p in self.weight_quantization.loggable_parameters():
                    if p.numel() == 1:
                        ml_logger.log_metric(self.name + '.' + n, p.item(),  step='auto')
                    else:
                        for i, e in enumerate(p):
                            ml_logger.log_metric(self.name + '.' + n + '.' + str(i), e.item(),  step='auto')

            # plot weights binning
            if self.log_clustering:
                weight = self.weight.flatten()
                B, v = self.weight_quantization.clustering(weight)
                plot_tensor_binning(weight, B, v, self.name, step, ml_logger)

            if self.log_weight_hist:
                ml_logger.tf_logger.add_histogram(self.name + '.weight', self.weight.cpu().flatten(),  step='auto')

            if self.log_mse:
                weight_q = self.weight_quantization(self.weight.flatten())
                mse_q = torch.nn.MSELoss()(self.weight.flatten(), weight_q)
                ml_logger.log_metric(self.name + '.mse_q', mse_q.cpu().item(),  step='auto')

                weight_kmeans = KmeansQuantization(self.bit_weights)(self.weight.flatten())
                mse_kmeans = torch.nn.MSELoss()(self.weight.flatten(), weight_kmeans)
                ml_logger.log_metric(self.name + '.mse_kmeans', mse_kmeans.cpu().item(),  step='auto')
