from pathlib import Path
from random import Random
from typing import List, Tuple, Any, Dict
import pickle
import numpy as np
import torch
from torch.nn.utils.rnn import pad_sequence
from torch.utils.data.dataloader import DataLoader
from torch.utils.data.dataset import Dataset
from torch.utils.data.sampler import Sampler
from dp.text import Tokenizer
from dp.utils import unpickle_binary


class PhonemizerDataset(Dataset):

    def __init__(self, items: List[tuple]) -> None:
        super().__init__()
        self.items = items

    def __getitem__(self, index: int) -> Dict[str, Any]:
        language, text, phonemes = self.items[index]
        text = torch.tensor(text, dtype=torch.long)
        phonemes = torch.tensor(phonemes, dtype=torch.long)

        return {'item_id': index, 'text': text,
                'phonemes': phonemes, 'language': language,
                'text_len': text.size(0), 'phonemes_len': phonemes.size(0)}

    def __len__(self):
        return len(self.items)


# From https://github.com/fatchord/WaveRNN/blob/master/utils/dataset.py
class BinnedLengthSampler(Sampler):

    def __init__(self, phoneme_lens: torch.tensor, batch_size: int, bin_size: int, seed=42) -> None:
        _, self.idx = torch.sort(torch.tensor(phoneme_lens))
        self.batch_size = batch_size
        self.bin_size = bin_size
        self.random = Random(seed)
        assert self.bin_size % self.batch_size == 0

    def __iter__(self):
        idx = self.idx.numpy()
        bins = []
        for i in range(len(idx) // self.bin_size):
            this_bin = idx[i * self.bin_size:(i + 1) * self.bin_size]
            self.random.shuffle(this_bin)
            bins += [this_bin]
        self.random.shuffle(bins)
        binned_idx = np.stack(bins).reshape(-1)
        if len(binned_idx) < len(idx):
            last_bin = idx[len(binned_idx):]
            self.random.shuffle(last_bin)
            binned_idx = np.concatenate([binned_idx, last_bin])
        return iter(torch.tensor(binned_idx).long())

    def __len__(self):
        return len(self.idx)


def collate_dataset(batch: List[dict]) -> torch.tensor:
    lang = [b['language'] for b in batch]
    lang = torch.tensor(lang).long()
    text = [b['text'] for b in batch]
    text = pad_sequence(text, batch_first=True, padding_value=0)
    text_len = torch.tensor([b['text_len'] for b in batch]).long()
    phonemes = [b['phonemes'] for b in batch]
    phonemes = pad_sequence(phonemes, batch_first=True, padding_value=0)
    phonemes_len = torch.tensor([b['phonemes_len'] for b in batch]).long()
    item_ids = [b['item_id'] for b in batch]
    return {'text': text, 'phonemes': phonemes, 'text_len': text_len,
            'phonemes_len': phonemes_len, 'item_id': item_ids, 'language': lang}


def new_dataloader(data: List[tuple],
                   batch_size=32,
                   use_binning=True) -> DataLoader:

    phonemizer_dataset = PhonemizerDataset(items=data)
    phoneme_lens = [len(p) for _, _, p in data]
    if use_binning:
        sampler = BinnedLengthSampler(phoneme_lens=phoneme_lens,
                                      batch_size=batch_size,
                                      bin_size=batch_size*3)
    else:
        sampler = None

    return DataLoader(phonemizer_dataset,
                      collate_fn=collate_dataset,
                      batch_size=batch_size,
                      sampler=sampler,
                      num_workers=0,
                      shuffle=False,
                      pin_memory=True)