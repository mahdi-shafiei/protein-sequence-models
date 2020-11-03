from typing import Union
from pathlib import Path
import lmdb
import subprocess
import string
import json
from os import path
import pickle as pkl
from scipy.spatial.distance import *

import numpy as np
import torch
from torch.utils.data import Dataset
import pandas as pd

from sequence_models.utils import Tokenizer
from sequence_models.constants import trR_ALPHABET
from sequence_models.gnn import bins_to_vals


class LMDBDataset(Dataset):
    """Creates a dataset from an lmdb file.
    Args:
        data_file (Union[str, Path]): Path to lmdb file.
        in_memory (bool, optional): Whether to load the full dataset into memory.
            Default: False.
    """

    def __init__(self,
                 data_file: Union[str, Path],
                 in_memory: bool = False):

        data_file = Path(data_file)
        if not data_file.exists():
            raise FileNotFoundError(data_file)

        env = lmdb.open(str(data_file), max_readers=1, readonly=True,
                        lock=False, readahead=False, meminit=False)

        with env.begin(write=False) as txn:
            num_examples = pkl.loads(txn.get(b'num_examples'))

        if in_memory:
            cache = [None] * num_examples
            self._cache = cache

        self._env = env
        self._in_memory = in_memory
        self._num_examples = num_examples

    def __len__(self) -> int:
        return self._num_examples

    def __getitem__(self, index: int):
        if not 0 <= index < self._num_examples:
            raise IndexError(index)

        if self._in_memory and self._cache[index] is not None:
            item = self._cache[index]
        else:
            with self._env.begin(write=False) as txn:
                item = pkl.loads(txn.get(str(index).encode()))
                if 'id' not in item:
                    item['id'] = str(index)
                if self._in_memory:
                    self._cache[index] = item
        return item


class TAPEDataset(Dataset):

    def __init__(self,
                 data_path: Union[str, Path],
                 data_type: str,
                 split: str,
                 contact_method : str = 'distance',
                 in_memory: bool = False):

        """
        data_path : path to data directory

        data_type : name of downstream task, [fluorescence, stability, remote_homology, 
            secondary_structure, contact]
        
        split : data split to load

        contact_method : if data_type == contact, choose 'distance' to get 
            distance instead of binary contact output
        """
        
        self.data_type = data_type
        self.contact_method = contact_method
        
        if data_type == 'fluorescence':
            if split not in ('train', 'valid', 'test'):
                raise ValueError(f"Unrecognized split: {split}. "
                                 f"Must be one of ['train', 'valid', 'test']")

            data_file = Path(data_path + f'fluorescence_{split}.lmdb')
            self.output_label = 'log_fluorescence'
            
        if data_type == 'stability':
            if split not in ('train', 'valid', 'test'):
                raise ValueError(f"Unrecognized split: {split}. "
                                 f"Must be one of ['train', 'valid', 'test']")

            data_file = Path(data_path + f'stability_{split}.lmdb')
            self.output_label = 'stability_score'
        
        if data_type == 'remote_homology':
            if split not in ('train', 'valid', 'test_fold_holdout',
                             'test_family_holdout', 'test_superfamily_holdout'):
                raise ValueError(f"Unrecognized split: {split}. Must be one of "
                                 f"['train', 'valid', 'test_fold_holdout', "
                                 f"'test_family_holdout', 'test_superfamily_holdout']")

            data_file = Path(data_path + f'remote_homology_{split}.lmdb')
            self.output_label = 'fold_label'
            
        if data_type == 'secondary_structure':
            if split not in ('train', 'valid', 'casp12', 'ts115', 'cb513'):
                raise ValueError(f"Unrecognized split: {split}. Must be one of "
                                 f"['train', 'valid', 'casp12', "
                                 f"'ts115', 'cb513']")

            data_file = Path(data_path + f'secondary_structure_{split}.lmdb')
            self.output_label = 'ss3'
            
        if data_type == 'contact':
            if split not in ('train', 'train_unfiltered', 'valid', 'test'):
                raise ValueError(f"Unrecognized split: {split}. Must be one of "
                                 f"['train', 'train_unfiltered', 'valid', 'test']")

            data_file = Path(data_path + f'proteinnet_{split}.lmdb')
            self.output_label = 'tertiary'
            
        self.data = LMDBDataset(data_file, in_memory)

    def __len__(self) -> int:
        return len(self.data)

    def __getitem__(self, index: int):
        item = self.data[index]
        primary = item['primary']
        
        if self.data_type in ['fluorescence', 'stability', ]:
            output = float(item[self.output_label][0])
        
        if self.data_type in ['remote_homology']:
            output = item[self.output_label]
        
        if self.data_type in ['secondary_structure']:
            # pad with -1s because of cls/sep tokens
#             labels = np.asarray(item['ss3'], np.int64)
#             labels = np.pad(labels, (1, 1), 'constant', constant_values=-1)
            output = torch.Tensor(item[self.output_label],).to(torch.int8)
            # output = item[self.output_label]
    
        if self.data_type in ['contact']:
            # -1 is contact, 0 in no contact
            if self.contact_method == 'distance':
                output = torch.Tensor(squareform(pdist(item[self.output_label])))
            else:
                valid_mask = item['valid_mask']
                contact_map = np.less(squareform(pdist(item[self.output_label])), 8.0).astype(np.int64)
                yind, xind = np.indices(contact_map.shape)
                invalid_mask = ~(valid_mask[:, None] & valid_mask[None, :])
                invalid_mask |= np.abs(yind - xind) < 6
                contact_map[invalid_mask] = -1
                output = torch.Tensor(contact_map).to(torch.int8)
        return primary, output


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


class FFDataset(Dataset):

    def __init__(self, stem, max_len=np.inf, tr_only=True):
        self.index = stem + 'ffindex'
        self.data = stem + 'ffdata'
        result = subprocess.run(['wc', '-l', self.index], stdout=subprocess.PIPE)
        self.length = int(result.stdout.decode('utf-8').split(' ')[0])
        self.tokenizer = Tokenizer(trR_ALPHABET)
        self.table = str.maketrans(dict.fromkeys(string.ascii_lowercase))
        self.max_len = max_len
        self.tr_only = tr_only

    def __len__(self):
        return self.length

    def __getitem__(self, idx):
        result = subprocess.run(['ffindex_get', self.data, self.index, '-n', str(idx + 1)],
                                stdout=subprocess.PIPE)
        a3m = result.stdout.decode('utf-8')
        seqs = []
        for line in a3m.split('\n'):
            # skip labels
            if len(line) == 0:
                continue
            if line[0] == '#':
                continue
            if line[0] != '>':
                # remove lowercase letters and right whitespaces
                s = line.rstrip().translate(self.table)
                if self.tr_only:
                    s = ''.join([a if a in trR_ALPHABET else '-' for a in s])
                if len(s) > self.max_len:
                    return torch.tensor([])
                seqs.append(s)
        seqs = torch.tensor([self.tokenizer.tokenize(s) for s in seqs])
        return seqs


class UniRefDataset(Dataset):
    """
    Dataset that pulls from UniRef/Uniclust downloads.

    The data folder should contain the following:
    - 'consensus.fasta': consensus sequences, no line breaks in sequences
    - 'splits.json': a dict with keys 'train', 'valid', and 'test' mapping to lists of indices
    - 'lengths_and_offsets.npz': byte offsets for the 'consensus.fasta' and sequence lengths
    """

    def __init__(self, data_dir: str, split: str, structure=False, pdb=False, p_drop=0.0, max_len=2048):
        self.data_dir = data_dir
        self.split = split
        self.structure = structure
        with open(data_dir + 'splits.json', 'r') as f:
            self.indices = json.load(f)[self.split]
        metadata = np.load(self.data_dir + 'lengths_and_offsets.npz')
        self.offsets = metadata['seq_offsets']
        self.pdb = pdb
        if self.pdb:
            self.n_digits = 6
        else:
            self.n_digits = 8
        self.p_drop = p_drop
        self.max_len = max_len

    def __len__(self):
        return len(self.indices)

    def __getitem__(self, idx):
        idx = self.indices[idx]
        offset = self.offsets[idx]
        with open(self.data_dir + 'consensus.fasta') as f:
            f.seek(offset)
            consensus = f.readline()[:-1]
        if len(consensus) - self.max_len > 0:
            start = np.random.choice(len(consensus) - self.max_len)
            stop = start + self.max_len
        else:
            start = 0
            stop = len(consensus)
        if self.structure:
            sname = 'structures/{num:{fill}{width}}.npz'.format(num=idx, fill='0', width=self.n_digits)
            fname = self.data_dir + sname
            if path.isfile(fname):
                structure = np.load(fname)
            else:
                structure = None
            if structure is not None:
                if np.random.random() < self.p_drop:
                    structure = None
                elif self.pdb:
                    dist = torch.tensor(structure['dist']).float()
                    omega = torch.tensor(structure['omega']).float()
                    theta = torch.tensor(structure['theta']).float()
                    phi = torch.tensor(structure['phi']).float()
                else:
                    dist, omega, theta, phi = bins_to_vals(data=structure)
            if structure is None:
                dist, omega, theta, phi = bins_to_vals(L=len(consensus))
            consensus = consensus[start:stop]
            dist = dist[start:stop, start:stop]
            omega = omega[start:stop, start:stop]
            theta = theta[start:stop, start:stop]
            phi = phi[start:stop, start:stop]
            return consensus, dist, omega, theta, phi
        consensus = consensus[start:stop]
        return (consensus, )