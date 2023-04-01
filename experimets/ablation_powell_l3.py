from subprocess import run
import mlflow
import numpy as np

models_set = [
    # {'model': 'vgg16', 'bs': 128, 'dev': [5]},
    # {'model': 'vgg16_bn', 'bs': 128, 'dev': [5]},
    # {'model': 'inception_v3', 'bs': 256, 'dev': [5]},
    # {'model': 'mobilenet_v2', 'bs': 128, 'dev': [5]},
    # {'model': 'resnet18', 'bs': 512, 'dev': [0]},
    {'model': 'resnet50', 'bs': 128, 'dev': [0]},
    # {'model': 'resnet101', 'bs': 512, 'dev': [5]}
]

exp_name = 'powell_res50_l3'
qtypes = ['l3_norm']
# qtypes = ['l2_norm', 'aciq_laplace', 'l3_norm']
maxiter = 1

for mset in models_set:
    for qt in qtypes:
        for bits in [2, 3, 4]:
            run(["python", "quantization/posttraining/layer_scale_optimization.py"] + ['-a', mset['model']] + ['-b', str(mset['bs'])]
                + ['--dataset', 'imagenet'] + ['--gpu_ids'] + " ".join(map(str, mset['dev'])).split(" ")
                + "--pretrained --custom_resnet".split(" ") + ['-exp', exp_name] + ['-maxi', str(maxiter)]
                + ['-ba', str(bits)] + ['--qtype', qt] + "--min_method Powell --init_method dynamic -cs 512".split(" ")
                )

        for bits in [3, 4]:
            run(["python", "quantization/posttraining/layer_scale_optimization.py"] + ['-a', mset['model']] + ['-b', str(
                mset['bs'])]
                + ['--dataset', 'imagenet'] + ['--gpu_ids'] + " ".join(map(str, mset['dev'])).split(" ")
                + "--pretrained --custom_resnet".split(" ") + ['-exp', exp_name] + ['-maxi', str(maxiter)]
                + ['-bw', str(bits)] + ['--qtype', qt] + "--min_method Powell --init_method dynamic -cs 512".split(
                " ")
                )

        for bits in [4]:
            run(["python", "quantization/posttraining/layer_scale_optimization.py"] + ['-a', mset['model']] + ['-b', str(
                mset['bs'])]
                + ['--dataset', 'imagenet'] + ['--gpu_ids'] + " ".join(map(str, mset['dev'])).split(" ")
                + "--pretrained --custom_resnet".split(" ") + ['-exp', exp_name] + ['-maxi', str(maxiter)]
                + ['-ba', str(bits)] + ['-bw', str(bits)] + ['--qtype', qt] + "--min_method Powell --init_method dynamic -cs 512".split(
                " ")
                )
