from typing import List, Any, Iterable
import random
import math

import numpy as np
import torch
from torch.utils.data import Dataset, Sampler, BatchSampler
import pandas as pd

from sequence_models.utils import Tokenizer
from sequence_models.constants import PAD, START, STOP, MASK
from sequence_models.constants import ALL_AAS


class FlatDataset(Dataset):

    def __init__(self, fpath, offsets, cols=[1]):
        self.fpath = fpath
        self.offsets = offsets
        self.cols = cols

    def __len__(self):
        return len(self.offsets)

    def __getitem__(self, idx):
        with open(self.fpath, 'r') as f:
            f.seek(self.offsets[idx])
            line = f.readline()[:-1]  # strip the \n
            line = line.split(',')
            return [line[i] for i in self.cols]


class CSVDataset(Dataset):

    def __init__(self, fpath=None, df=None, split=None, outputs=[]):
        if df is None:
            self.data = pd.read_csv(fpath)
        else:
            self.data = df
        if split is not None:
            self.data = self.data[self.data['split'] == split]
        self.outputs = outputs
        self.data = self.data[['sequence'] + self.outputs]

    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        row = self.data.loc[idx]
        return [row['sequence'], *row[self.outputs]]


class SimpleCollater(object):

    def __init__(self, alphabet: str, pad=False):
        self.pad = pad
        self.tokenizer = Tokenizer(alphabet)

    def __call__(self, batch: List[Any], ) -> List[torch.Tensor]:
        data = tuple(zip(*batch))
        sequences = data[0]
        sequences = [torch.LongTensor(self.tokenizer.tokenize(s)) for s in sequences]
        if self.pad:
            pad_idx = self.tokenizer.alphabet.index(PAD)
            sequences = _pad(sequences, pad_idx)
        else:
            sequences = torch.stack(sequences)
        data = (torch.tensor(d) for d in data[1:])
        return [sequences, *data]


class LMCollater(SimpleCollater):

    def __init__(self, alphabet: str, pad=False, backwards=False):
        super().__init__(alphabet, pad=pad)
        self.backwards = backwards

    def __call__(self, batch: List[Any]) -> List[torch.Tensor]:
        data = tuple(zip(*batch))
        sequences = data[0]
        if not self.backwards:
            src = [START + s for s in sequences]
            tgt = [s + STOP for s in sequences]
        else:
            src = [STOP + s[::-1] for s in sequences]
            tgt = [s[::-1] + START for s in sequences]
        src = [torch.LongTensor(self.tokenizer.tokenize(s)) for s in src]
        tgt = [torch.LongTensor(self.tokenizer.tokenize(s)) for s in tgt]
        mask = [torch.ones_like(t) for t in tgt]
        pad_idx = self.tokenizer.alphabet.index(PAD)
        src = _pad(src, pad_idx)
        tgt = _pad(tgt, pad_idx)
        mask = _pad(mask, 0)
        data = (torch.tensor(d) for d in data[1:])
        return [src, tgt, mask, *data]


def _pad(tokenized: List[torch.Tensor], value: int) -> torch.Tensor:
    batch_size = len(tokenized)
    max_len = max(len(t) for t in tokenized)
    output = torch.zeros((batch_size, max_len), dtype=tokenized[0].dtype) + value
    for row, t in enumerate(tokenized):
        output[row, :len(t)] = t
    return output


class MLMCollater(SimpleCollater):

    def __call__(self, batch: List[Any], ) -> List[torch.Tensor]:
        data = tuple(zip(*batch))
        sequences = data[0]
        tgt = data[0]
        src = []
        mask = []
        for seq in sequences:
            mod_idx = random.sample(list(range(len(seq))), int(len(seq) * 0.15))
            if len(mod_idx) == 0:
                mod_idx = [np.random.choice(list(range(len(seq))))]  # make sure at least one aa is chosen
            seq_mod = list(seq)
            for idx in mod_idx:
                p = np.random.uniform()
                if p <= 0.10:  # do nothing
                    mod = seq[idx]
                elif 0.10 < p <= 0.20:  # replace with random amino acid
                    mod = np.random.choice([i for i in ALL_AAS if i != seq[idx]])
                else:  # mask
                    mod = MASK
                seq_mod[idx] = mod
            src.append(''.join(seq_mod))
            m = torch.zeros(len(seq_mod))
            m[mod_idx] = 1
            mask.append(m)
        src = [torch.LongTensor(self.tokenizer.tokenize(s)) for s in src]
        tgt = [torch.LongTensor(self.tokenizer.tokenize(s)) for s in tgt]
        pad_idx = self.tokenizer.alphabet.index(PAD)
        src = _pad(src, pad_idx)
        tgt = _pad(tgt, pad_idx)
        mask = _pad(mask, 0)
        data = (torch.tensor(d) for d in data[1:])
        return [src, tgt, mask, *data]


class SortishSampler(Sampler):
    """Returns indices such that inputs with similar lengths are close together."""

    def __init__(self, sequence_lengths: Iterable, bucket_size: int, num_replicas: int = 1, rank: int = 0):
        self.data = np.argsort(sequence_lengths)
        self.num_replicas = num_replicas
        self.num_samples = int(math.ceil(len(self.data) * 1.0 / self.num_replicas))
        self.bucket_size = bucket_size
        n_buckets = int(np.ceil(len(self.data) / self.bucket_size))
        self.data = [self.data[i * bucket_size: i * bucket_size + bucket_size] for i in range(n_buckets)]
        self.rank = rank
        self.epoch = 0
        self.total_size = self.num_samples * self.num_replicas

    def __iter__(self):
        np.random.seed(self.epoch)
        for bucket in self.data:
            np.random.shuffle(bucket)
        np.random.shuffle(self.data)
        indices = [item for sublist in self.data for item in sublist]
        indices += indices[:(self.total_size - len(indices))]
        assert len(indices) == self.total_size
        # subsample
        indices = indices[self.rank:self.total_size:self.num_replicas]
        assert len(indices) == self.num_samples
        return iter(indices)

    def __len__(self):
        return self.num_samples

    def set_epoch(self, epoch):
        self.epoch = epoch


class ApproxBatchSampler(BatchSampler):
    """
	Parameters:
	-----------
	sampler : Pytorch Sampler
		Choose base sampler class to use for bucketing

	max_tokens : int
		Maximum number of tokens per batch

	max_batch: int
		Maximum batch size

	sample_lengths : array-like
		List of lengths of sequences in the order of the dataset
	"""

    def __init__(self, sampler, max_tokens, max_batch, sample_lengths):
        self.longest_token = 0
        self.max_tokens = max_tokens
        self.max_batch = max_batch
        self.sampler = sampler
        self.sample_lengths = sample_lengths

    def __iter__(self):
        batch = []
        length = 0
        for idx in self.sampler:
            this_length = self.sample_lengths[idx]
            if (len(batch) + 1) * max(length, this_length) <= self.max_tokens:
                batch.append(idx)
                length = max(length, this_length)
                if len(batch) == self.max_batch:
                    yield batch
                    batch = []
                    length = 0
            else:
                yield batch
                batch = [idx]
                length = this_length
