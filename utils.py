#!/usr/bin/env python3
# -*- coding: utf-8 -*-

__all__ = [
            'BatchGenerator',
            'TripletDataset',
            'choice_accuracy',
            'cross_entropy_loss',
            'encode_as_onehot',
            'get_digits',
            'get_nneg_dims',
            'get_results_files',
            'load_batches',
            'load_data',
            'load_model',
            'merge_dicts',
            'softmax',
            'trinomial_loss',
            'trinomial_probs',
            'tripletize_data',
            'validation',
            ]

import json
import logging
import os
import re
import torch
import warnings

import numpy as np
import torch.nn.functional as F

from torch.utils.data.distributed import DistributedSampler
from torch.utils.data import Dataset, DataLoader, SequentialSampler
from typing import Tuple, Iterator

class TripletDataset(Dataset):

    def __init__(self, I:torch.tensor, dataset:torch.Tensor):
        self.I = I
        self.dataset = dataset

    def __len__(self) -> int:
        return len(self.dataset)

    def __getitem__(self, idx:int) -> torch.Tensor:
        sample = encode_as_onehot(self.I, self.dataset[idx])
        return sample

class BatchGenerator(object):

    def __init__(
                self,
                I:torch.tensor,
                dataset:torch.Tensor,
                batch_size:int,
                sampling_method:str='normal',
                p=None,
):
        self.I = I
        self.dataset = dataset
        self.batch_size = batch_size
        self.sampling_method = sampling_method
        self.p = p

        if sampling_method == 'soft':
            assert isinstance(self.p, float)
            self.n_batches = int(len(self.dataset) * self.p) // self.batch_size
        else:
            self.n_batches = len(self.dataset) // self.batch_size

    def __len__(self) -> int:
        return self.n_batches

    def __iter__(self):
        return self.get_batches(self.I, self.dataset)

    def sampling(self, triplets:torch.Tensor) -> torch.Tensor:
        """randomly sample training data during each epoch"""
        rnd_perm = torch.randperm(len(triplets))
        if self.sampling_method == 'soft':
            rnd_perm = rnd_perm[:int(len(rnd_perm) * self.p)]
        return triplets[rnd_perm]

    def get_batches(self, I:torch.Tensor, triplets:torch.Tensor):
        if not isinstance(self.sampling_method, type(None)):
            triplets = self.sampling(triplets)
        for i in range(self.n_batches):
            batch = encode_as_onehot(I, triplets[i*self.batch_size: (i+1)*self.batch_size])
            yield batch

def remove_nans(E:np.ndarray) -> np.ndarray:
    E_cp = E[:, :]
    nan_indices = np.isnan(E_cp).any(axis=1) #return indices for rows that contain NaN values
    E_cp = E_cp[~nan_indices]
    return E_cp

def assert_nneg(X:np.ndarray, thresh:float=1e-5) -> np.ndarray:
    """if data matrix X contains negative real numbers, transform matrix into R+ (i.e., positive real number(s) space)"""
    if np.any(X < 0):
        X -= np.amin(X, axis=0)
        return X + thresh
    return X

def load_features(PATH:str) -> np.ndarray:
    if re.search(r'text', PATH):
        E = np.loadtxt(PATH, delimiter=',')
        E = remove_nans(E) #remove all objects that contain NaN values
    else:
        with open(PATH, 'rb') as f:
            E = np.load(f)
    return E

def tripletize_data(
                    PATH:str,
                    method:str,
                    n_samples:float,
                    sampling_constant:float,
                    folder:str,
                    dir:str='triplets/',
                    device:torch.device=torch.device('cpu'),
                    beta=None,
) -> Tuple[np.ndarray, np.ndarray]:
    """create triplets of object embedding similarities, and for each triplet find the odd-one-out"""
    #some word embeddings contain NaN values
    if re.search(r'text', PATH):
        E = np.loadtxt(PATH, delimiter=',')
        E = remove_nans(E) #remove all objects that contain NaN values
    else:
        E = np.loadtxt(PATH)

    #create similarity matrix
    #TODO: figure out whether an affinity matrix might be more reasonable (i.e., informative) than a simple similarity matrix
    S = E @ E.T
    N = S.shape[0]

    def filter_triplets(rnd_samples:np.ndarray, n_samples:float) -> np.ndarray:
        """filter for unique triplets (i, j, k have to be different indices)"""
        rnd_samples = np.asarray(list(filter(lambda triplet: len(np.unique(triplet)) == len(triplet), rnd_samples)))
        #remove all duplicates from our sample
        rnd_samples = np.unique(rnd_samples, axis=0)[:int(n_samples)]
        return rnd_samples

    #draw random samples of triplets of concepts
    rnd_samples = np.random.randint(N, size=(int(n_samples + sampling_constant), 3))
    #filter for unique triplets and remove all duplicates
    rnd_samples = filter_triplets(rnd_samples, n_samples)

    if method == 'probabilistic':
        assert isinstance(beta, float), 'beta value to determine softmax temperature is required'
        max_probas = np.zeros(int(n_samples))
        def softmax(x:np.ndarray, beta:float) -> np.ndarray:
            return np.exp(beta * x)/np.sum(np.exp(beta * x))

        def sample_choices(odd_one_outs:np.ndarray, sims:np.ndarray, beta:float) -> np.ndarray:
            """probabilistically sample triplet choices (conditioned on PMF obtained through softmax over similarity values)"""
            probas = softmax(sims, beta)
            choices = np.random.choice(odd_one_outs, size=len(probas), replace=False, p=probas)
            choices = choices[::-1]
            return choices, max(probas)

    triplets = np.zeros((int(n_samples), 3), dtype=int)
    for idx, [i, j, k] in enumerate(rnd_samples):
        odd_one_outs = np.asarray([k, j, i])
        sims = np.array([S[i, j], S[i, k], S[j, k]])
        if method == 'probabilistic':
            choices, max_p = sample_choices(odd_one_outs, sims, beta)
            max_probas[idx] += max_p
        else:
            #simply use the argmax to (deterministically) find the odd-one-out choice
            choices = odd_one_outs[np.argsort(sims)]
        triplets[idx] = choices

    PATH = os.path.join(dir, folder)
    if not os.path.exists(PATH):
        os.makedirs(PATH)

    rnd_indices = np.random.permutation(len(triplets))
    train_triplets = triplets[rnd_indices[:int(len(rnd_indices)*.9)]]
    test_triplets = triplets[rnd_indices[int(len(rnd_indices)*.9):]]

    with open(os.path.join(PATH, 'train_90.npy'), 'wb') as train_file:
        np.save(train_file, train_triplets)

    with open(os.path.join(PATH, 'test_10.npy'), 'wb') as test_file:
        np.save(test_file, test_triplets)

    if method == 'probabilistic':
        avg_p = np.mean(max_probas)
        print('==================================================================================')
        print(f'===== Average maximum probability value (ceiling model performance): {avg_p:.2f} =====')
        print('==================================================================================')
        print()

    train_triplets = torch.from_numpy(train_triplets).to(device).type(torch.LongTensor)
    test_triplets = torch.from_numpy(test_triplets).to(device).type(torch.LongTensor)

    return train_triplets, test_triplets

def load_data(device:torch.device, triplets_dir:str) -> Tuple[torch.Tensor, torch.Tensor]:
    """load train and test triplet datasets into memory"""
    try:
        with open(os.path.join(triplets_dir, 'train_90.npy'), 'rb') as train_file:
            train_triplets = torch.from_numpy(np.load(train_file)).to(device).type(torch.LongTensor)

        with open(os.path.join(triplets_dir, 'test_10.npy'), 'rb') as test_file:
            test_triplets = torch.from_numpy(np.load(test_file)).to(device).type(torch.LongTensor)
    except FileNotFoundError:
        print('...Could not find any .npy files for current modality.')
        print('...Now searching for .txt files.')
        print()
        train_triplets = torch.from_numpy(np.loadtxt(os.path.join(triplets_dir, 'train_90.txt'))).to(device).type(torch.LongTensor)
        if re.search(r'synthetic', triplets_dir):
            triplets_dir = os.path.join('./triplets', 'behavioral')
        test_triplets = torch.from_numpy(np.loadtxt(os.path.join(triplets_dir, 'test_10.txt'))).to(device).type(torch.LongTensor)
    return train_triplets, test_triplets

def load_batches(
                 train_triplets:torch.Tensor,
                 test_triplets:torch.Tensor,
                 I:torch.Tensor,
                 multi_proc:bool,
                 n_gpus:int,
                 batch_size:int,
                 sampling_method:str,
                 rnd_seed:int,
                 p=None,
                 ):
    if (multi_proc and n_gpus > 1):
        if sampling_method == 'soft':
            warnings.warn(f'...Soft sampling cannot be used in a multi-process distributed training setting.', RuntimeWarning)
            warnings.warn(f'...Processes will equally distribute the entire training dataset amongst each other.', RuntimeWarning)
            warnings.warn(f'...If you want to use soft sampling, you must switch to single GPU or CPU training.', UserWarning)
        train_set = TripletDataset(I=I, dataset=train_triplets)
        val_set = TripletDataset(I=I, dataset=test_triplets)
        train_sampler = DistributedSampler(dataset=train_set, shuffle=True, seed=rnd_seed)
        train_batches = DataLoader(dataset=train_set, batch_size=batch_size, sampler=train_sampler, num_workers=n_gpus)
        val_batches = DataLoader(dataset=val_set, batch_size=batch_size, shuffle=False, num_workers=n_gpus)
    else:
        #create train and validation mini-batches
        train_batches = BatchGenerator(I=I, dataset=train_triplets, batch_size=batch_size, sampling_method=sampling_method, p=p)
        val_batches = BatchGenerator(I=I, dataset=test_triplets, batch_size=batch_size, sampling_method=None, p=None)
    return train_batches, val_batches

def encode_as_onehot(I:torch.Tensor, triplets:torch.Tensor) -> torch.Tensor:
    """encode item triplets as one-hot-vectors"""
    return I[triplets.flatten(), :]

def softmax(sims:tuple) -> torch.Tensor:
    return torch.exp(sims[0]) / torch.sum(torch.stack([torch.exp(sim) for sim in sims]), dim=0)

def cross_entropy_loss(sims:tuple) -> torch.Tensor:
    return torch.mean(-torch.log(softmax(sims)))

def compute_similarities(anchor:torch.Tensor, positive:torch.Tensor, negative:torch.Tensor, method:str) -> Tuple:
    pos_sim = torch.sum(anchor * positive, dim=1)
    neg_sim = torch.sum(anchor * negative, dim=1)
    if method == 'odd_one_out':
        neg_sim_2 = torch.sum(positive * negative, dim=1)
        return pos_sim, neg_sim, neg_sim_2
    else:
        return pos_sim, neg_sim

def choice_accuracy(anchor:torch.Tensor, positive:torch.Tensor, negative:torch.Tensor, method:str) -> torch.Tensor:
    sims  = compute_similarities(anchor, positive, negative, method)
    choices = torch.argmax(torch.stack(sims), dim=0)
    choice_acc = len(choices[choices == 0]) / len(choices)
    return choice_acc

def trinomial_probs(anchor:torch.Tensor, positive:torch.Tensor, negative:torch.Tensor, method:str) -> torch.Tensor:
    sims = compute_similarities(anchor, positive, negative, method)
    return softmax(sims)

def trinomial_loss(anchor:torch.Tensor, positive:torch.Tensor, negative:torch.Tensor, method:str) -> torch.Tensor:
    sims = compute_similarities(anchor, positive, negative, method)
    return cross_entropy_loss(sims)

def get_nneg_dims(W:torch.Tensor, eps:float=0.1) -> int:
    w_max = W.max(dim=1)[0]
    nneg_d = len(w_max[w_max > eps])
    return nneg_d

########################################################
######### helper functions for offline evaluation ######
#######################################################

def validation(
                model,
                val_batches,
                version:str,
                task:str,
                device:torch.device,
                embed_dim:int,
                sampling:bool=False,
                batch_size=None,
                n_samples=None,
                ):
    if sampling:
        assert isinstance(batch_size, int), 'batch size must be defined'
        sampled_choices = np.zeros((int(len(val_batches) * batch_size), 3), dtype=int)

    model.eval()
    with torch.no_grad():
        batch_losses_val = torch.zeros(len(val_batches))
        batch_accs_val = torch.zeros(len(val_batches))
        for j, batch in enumerate(val_batches):
            batch = batch.to(device)

            if version == 'variational':
                assert isinstance(n_samples, int), 'ouputs of variational neural networks have to be averaged over different samples'
                k = 3 if task == 'odd_one_out' else 2
                sampled_probas = torch.zeros(n_samples, batch.shape[0] // k, k).to(device)
                sampled_choices = torch.zeros(n_samples, batch.shape[0] // k).to(device)

                for k in range(n_samples):
                    logits, _, _, _ = model(batch, device)
                    anchor, positive, negative = torch.unbind(torch.reshape(logits, (-1, 3, embed_dim)), dim=1)
                    similarities = compute_similarities(anchor, positive, negative, task)
                    soft_choices = softmax(similarities)
                    probas = F.softmax(torch.stack(similarities, dim=-1), dim=1)
                    sampled_probas[k] += probas
                    sampled_choices[k] +=  soft_choices

                probas = sampled_probas.mean(dim=0)
                preds = torch.argmax(probas, dim=1)
                val_acc = len(preds[preds == 0]) / len(preds)
                soft_choices = sampled_choices.mean(dim=0)
                val_loss = torch.mean(-torch.log(soft_choices))
            else:
                logits = model(batch)
                anchor, positive, negative = torch.unbind(torch.reshape(logits, (-1, 3, logits.shape[-1])), dim=1)

                if sampling:
                    similarities = compute_similarities(anchor, positive, negative, task)
                    probas = F.softmax(torch.stack(similarities, dim=-1), dim=1).numpy()
                    probas = probas[:, ::-1]
                    human_choices = batch.nonzero(as_tuple=True)[-1].view(batch_size, -1).numpy()
                    model_choices = np.array([np.random.choice(h_choice, size=len(p), replace=False, p=p)[::-1] for h_choice, p in zip(human_choices, probas)])
                    sampled_choices[j*batch_size:(j+1)*batch_size] += model_choices
                else:
                    val_loss = trinomial_loss(anchor, positive, negative, task)
                    val_acc = choice_accuracy(anchor, positive, negative, task)

            batch_losses_val[j] += val_loss.item()
            batch_accs_val[j] += val_acc

    if sampling:
        return sampled_choices

    avg_val_loss = torch.mean(batch_losses_val).item()
    avg_val_acc = torch.mean(batch_accs_val).item()
    return avg_val_loss, avg_val_acc

def get_digits(string:str) -> int:
    c = ""
    nonzero = False
    for i in string:
        if i.isdigit():
            if (int(i) == 0) and (not nonzero):
                continue
            else:
                c += i
                nonzero = True
    return int(c)

def get_results_files(
                      results_dir:str,
                      modality:str,
                      version:str,
                      subfolder:str,
                      vision_model=None,
                      layer=None,
) -> list:
    if modality == 'visual':
        assert isinstance(vision_model, str) and isinstance(layer, str), 'name of vision model and layer are required'
        PATH = os.path.join(results_dir, modality, vision_model, layer, version, f'{dim}d', f'{lmbda}')
    else:
        PATH = os.path.join(results_dir, modality, version, f'{dim}d', f'{lmbda}')
    files = [os.path.join(PATH, seed, f) for seed in os.listdir(PATH) for f in os.listdir(os.path.join(PATH, seed)) if f.endswith('.json')]
    return files

def sort_results(results:dict) -> dict:
    return dict(sorted(results.items(), key=lambda kv:kv[0], reverse=False))

def merge_dicts(files:list) -> dict:
    """merge multiple .json files efficiently into a single dictionary"""
    results = {}
    for f in files:
        with open(f, 'r') as f:
            results.update(dict(json.load(f)))
    results = sort_results(results)
    return results

def load_model(
                model,
                results_dir:str,
                modality:str,
                version:str,
                dim:int,
                lmbda:float,
                rnd_seed:int,
                device:torch.device,
                subfolder:str='model',
):
    model_path = os.path.join(results_dir, modality, version, f'{dim}d', f'{lmbda}', f'seed{rnd_seed}', subfolder)
    models = os.listdir(model_path)
    checkpoints = list(map(get_digits, models))
    last_checkpoint = np.argmax(checkpoints)
    PATH = os.path.join(model_path, models[last_checkpoint])
    checkpoint = torch.load(PATH, map_location=device)
    model.load_state_dict(checkpoint['model_state_dict'])
    return model
