import math
from typing import Tuple, Dict, Any

import torch
import torch.nn as nn
from torch.nn import TransformerEncoderLayer, LayerNorm, TransformerEncoder, ModuleList
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence, pad_sequence

from dp.text import Preprocessor


def get_dedup_tokens(logits_batch: torch.tensor) \
        -> Tuple[torch.tensor, torch.tensor]:

    """ Returns deduplicated tokens and probs of tokens """

    logits_batch = logits_batch.softmax(-1)
    out_tokens, out_probs = [], []
    for i in range(logits_batch.size(0)):
        logits = logits_batch[i]
        max_logits, max_indices = torch.max(logits, dim=-1)
        max_logits = max_logits[max_indices!=0]
        max_indices = max_indices[max_indices!=0]
        cons_tokens, counts = torch.unique_consecutive(
            max_indices, return_counts=True)
        out_probs_i = []
        ind = 0
        for c in counts:
            max_logit = max_logits[ind:ind + c].max()
            out_probs_i.append(max_logit.item())
            ind = ind + c
        out_tokens.append(cons_tokens)
        out_probs_i = torch.tensor(out_probs_i)
        out_probs.append(out_probs_i)

    out_tokens = pad_sequence(out_tokens, batch_first=True, padding_value=0)
    out_probs = pad_sequence(out_probs, batch_first=True, padding_value=0)

    return out_tokens, out_probs


class LstmModel(torch.nn.Module):

    def __init__(self,
                 num_symbols_in: int,
                 num_symbols_out: int,
                 lstm_dim: int,
                 num_layers: int) -> None:
        super().__init__()
        self.register_buffer('step', torch.tensor(1, dtype=torch.int))
        self.embedding = nn.Embedding(num_embeddings=num_symbols_in, embedding_dim=lstm_dim)
        lstms = [torch.nn.LSTM(lstm_dim, lstm_dim, batch_first=True, bidirectional=True)]
        for i in range(1, num_layers):
            lstms.append(
                torch.nn.LSTM(2 * lstm_dim, lstm_dim, batch_first=True, bidirectional=True)
            )
        self.lstms = ModuleList(lstms)
        self.lin = torch.nn.Linear(2 * lstm_dim, num_symbols_out)

    def forward(self,
                x: torch.tensor,
                x_len: torch.tensor = None) -> torch.tensor:
        if self.training:
            self.step += 1
        x = self.embedding(x)
        if x_len is not None:
            x = pack_padded_sequence(x, x_len.cpu(), batch_first=True, enforce_sorted=False)
        for lstm in self.lstms:
            x, _ = lstm(x)
        if x_len is not None:
            x, _ = pad_packed_sequence(x, batch_first=True)
        x = self.lin(x)
        return x

    def generate(self,
                 x: torch.tensor,
                 x_len: torch.tensor = None) -> Tuple[torch.tensor, torch.tensor]:
        with torch.no_grad():
            x = self.forward(x, x_len=x_len)
        tokens, logits = get_dedup_tokens(x)
        return tokens, logits

    def get_step(self) -> int:
        return self.step.data.item()

    @classmethod
    def from_config(cls, config: dict) -> 'LstmModel':
        preprocessor = Preprocessor.from_config(config)
        model = LstmModel(
            num_symbols_in=preprocessor.text_tokenizer.vocab_size,
            num_symbols_out=preprocessor.phoneme_tokenizer.vocab_size,
            lstm_dim=config['model']['lstm_dim'],
            num_layers=config['model']['num_layers']
        )
        return model


class PositionalEncoding(nn.Module):

    def __init__(self, d_model: int, dropout=0.1, max_len=5000) -> None:
        super(PositionalEncoding, self).__init__()
        self.dropout = nn.Dropout(p=dropout)
        self.scale = nn.Parameter(torch.ones(1))

        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(torch.arange(
            0, d_model, 2).float() * (-math.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0).transpose(0, 1)
        self.register_buffer('pe', pe)

    def forward(self, x: torch.tensor) -> torch.tensor:         # shape: [T, N]
        x = x + self.scale * self.pe[:x.size(0), :]
        return self.dropout(x)


class ForwardTransformer(nn.Module):

    def __init__(self,
                 encoder_vocab_size: int,
                 decoder_vocab_size: int,
                 d_model=512,
                 d_fft=1024,
                 layers=4,
                 dropout=0.1,
                 heads=1) -> None:
        super(ForwardTransformer, self).__init__()

        self.d_model = d_model

        self.embedding = nn.Embedding(encoder_vocab_size, d_model)
        self.pos_encoder = PositionalEncoding(d_model, dropout)

        encoder_layer = TransformerEncoderLayer(d_model=d_model,
                                                nhead=heads,
                                                dim_feedforward=d_fft,
                                                dropout=dropout,
                                                activation='relu')
        encoder_norm = LayerNorm(d_model)
        self.encoder = TransformerEncoder(encoder_layer=encoder_layer,
                                          num_layers=layers,
                                          norm=encoder_norm)

        self.fc_out = nn.Linear(d_model, decoder_vocab_size)

        self.register_buffer('step', torch.tensor(1, dtype=torch.int))

        self.src_mask = None
        self.memory_mask = None

    def generate_square_subsequent_mask(self, sz: int) -> torch.tensor:
        mask = torch.triu(torch.ones(sz, sz), 1)
        mask = mask.masked_fill(mask == 1, float('-inf'))
        return mask

    def make_len_mask(self, inp: torch.tensor) -> torch.tensor:
        return (inp == 0).transpose(0, 1)

    def forward(self, x, **kwargs) -> torch.tensor:         # shape: [N, T]

        if self.training:
            self.step += 1

        x = x.transpose(0, 1)        # shape: [T, N]
        src_pad_mask = self.make_len_mask(x).to(x.device)
        x = self.embedding(x)
        x = self.pos_encoder(x)
        x = self.encoder(x, src_key_padding_mask=src_pad_mask)
        x = self.fc_out(x)
        x = x.transpose(0, 1)
        return x

    def generate(self,
                 x: torch.tensor,
                 x_len: torch.tensor = None) -> Tuple[torch.tensor, torch.tensor]:
        with torch.no_grad():
            x = self.forward(x, x_len=x_len)
        tokens, logits = get_dedup_tokens(x)
        return tokens, logits

    def get_step(self) -> int:
        return self.step.data.item()

    @classmethod
    def from_config(cls, config: dict) -> 'ForwardTransformer':
        preprocessor = Preprocessor.from_config(config)
        return ForwardTransformer(
            encoder_vocab_size=preprocessor.text_tokenizer.vocab_size,
            decoder_vocab_size=preprocessor.phoneme_tokenizer.vocab_size,
            d_model=config['model']['d_model'],
            d_fft=config['model']['d_fft'],
            layers=config['model']['layers'],
            dropout=config['model']['dropout'],
            heads=config['model']['heads']
        )


def load_checkpoint(checkpoint_path: str, device='cpu') -> Tuple[torch.nn.Module, Dict[str, Any]]:
    device = torch.device(device)
    checkpoint = torch.load(checkpoint_path, map_location=device)

    model_type = checkpoint['config']['model']['type']
    supported_types = ['lstm', 'transformer']
    if model_type == 'lstm':
            model = LstmModel.from_config(checkpoint['config']).to(device)
    elif model_type == 'transformer':
            model = ForwardTransformer.from_config(checkpoint['config']).to(device)
    elif model_type == 'autoreg_transformer':
            model = AutoregressiveTransformer.from_config(checkpoint['config']).to(device)
    else:
        raise ValueError(f'Model type not supported: {model_type}. Supported types: {supported_types}')

    model.load_state_dict(checkpoint['model'])

    model.eval()
    return model, checkpoint


class AutoregressiveTransformer(nn.Module):

    def __init__(self,
                 encoder_vocab_size: int,
                 decoder_vocab_size: int,
                 end_index: int,
                 d_model=512,
                 d_fft=1024,
                 encoder_layers=4,
                 decoder_layers=4,
                 dropout=0.1,
                 heads=1):
        super(AutoregressiveTransformer, self).__init__()

        self.end_index = end_index

        self.d_model = d_model

        self.encoder = nn.Embedding(encoder_vocab_size, d_model)
        self.pos_encoder = PositionalEncoding(d_model, dropout)

        self.decoder = nn.Embedding(decoder_vocab_size, d_model)
        self.pos_decoder = PositionalEncoding(d_model, dropout)

        self.transformer = nn.Transformer(d_model=d_model, nhead=heads, num_encoder_layers=encoder_layers,
                                          num_decoder_layers=decoder_layers, dim_feedforward=d_fft,
                                          dropout=dropout, activation='relu')
        self.fc_out = nn.Linear(d_model, decoder_vocab_size)

        self.register_buffer('step', torch.tensor(1, dtype=torch.int))

        self.src_mask = None
        self.memory_mask = None

    def generate_square_subsequent_mask(self, sz):
        mask = torch.triu(torch.ones(sz, sz), 1)
        mask = mask.masked_fill(mask == 1, float('-inf'))
        return mask

    def make_len_mask(self, inp):
        return (inp == 0).transpose(0, 1)

    def forward(self, src, trg, **kwargs):         # shape: [N, T]

        if self.training:
            self.step += 1

        src = src.transpose(0, 1)        # shape: [T, N]
        trg = trg.transpose(0, 1)

        trg_mask = self.generate_square_subsequent_mask(len(trg)).to(trg.device)

        src_pad_mask = self.make_len_mask(src).to(trg.device)
        trg_pad_mask = self.make_len_mask(trg).to(trg.device)

        src = self.encoder(src)
        src = self.pos_encoder(src)

        trg = self.decoder(trg)
        trg = self.pos_decoder(trg)

        output = self.transformer(src, trg, src_mask=self.src_mask, tgt_mask=trg_mask,
                                  memory_mask=self.memory_mask, src_key_padding_mask=src_pad_mask,
                                  tgt_key_padding_mask=trg_pad_mask, memory_key_padding_mask=src_pad_mask)
        output = self.fc_out(output)
        output = output.transpose(0, 1)
        return output

    def generate(self,
                 input: torch.tensor,       # shape: [N, T]
                 start_index: torch.tensor,
                 max_len=100,
                 **kwargs) -> Tuple[torch.tensor, torch.tensor]:

        """ Returns indices and logits """
        batch_size = input.size(0)
        input = input.transpose(0, 1)          # shape: [T, N]
        src_pad_mask = self.make_len_mask(input).to(input.device)
        with torch.no_grad():
            input = self.encoder(input)
            input = self.pos_encoder(input)
            input = self.transformer.encoder(input,
                                             src_key_padding_mask=src_pad_mask)
            out_indices = start_index.unsqueeze(0)
            out_logits = []
            for i in range(max_len):
                tgt_mask = self.generate_square_subsequent_mask(i + 1).to(input.device)
                output = self.decoder(out_indices)
                output = self.pos_decoder(output)
                output = self.transformer.decoder(output,
                                                  input,
                                                  memory_key_padding_mask=src_pad_mask,
                                                  tgt_mask=tgt_mask)
                output = self.fc_out(output).softmax(-1)  # shape: [T, N, V]
                out_tokens = output.argmax(2)[-1:, :]
                out_logits.append(output[-1:, :, :])

                out_indices = torch.cat([out_indices, out_tokens], dim=0)
                stop_rows, _ = torch.max(out_indices == self.end_index, dim=0)
                if torch.sum(stop_rows) == batch_size:
                    break

        out_indices = out_indices.transpose(0, 1)  # out shape [N, T]
        out_logits = torch.cat(out_logits, dim=0).transpose(0, 1) # out shape [N, T, V]
        out_logits = out_logits.softmax(-1)
        out_probs = torch.ones((out_indices.size(0), out_indices.size(1)))
        for i in range(out_indices.size(0)):
            for j in range(0, out_indices.size(1)-1):
                out_probs[i, j+1] = out_logits[i, j].max()
        return out_indices, out_probs

    def get_step(self):
        return self.step.data.item()

    @classmethod
    def from_config(cls, config: dict) -> 'AutoregressiveTransformer':
        preprocessor = Preprocessor.from_config(config)
        return AutoregressiveTransformer(
            encoder_vocab_size=preprocessor.text_tokenizer.vocab_size,
            decoder_vocab_size=preprocessor.phoneme_tokenizer.vocab_size,
            end_index=preprocessor.phoneme_tokenizer.end_index,
            d_model=config['model']['d_model'],
            d_fft=config['model']['d_fft'],
            encoder_layers=config['model']['layers'],
            decoder_layers=config['model']['layers'],
            dropout=config['model']['dropout'],
            heads=config['model']['heads']
        )