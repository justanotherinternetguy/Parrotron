import os
import torch
import matplotlib
import torch.nn as nn
from torch.autograd import Variable
from torch.nn import functional as F
import random
from models.attention import DotProductAttention, LocationSensitiveAttention
from models.postnet import Postnet

class Prenet(nn.Module):
    def __init__(self, in_dim, out_dim, drop_out_p):
        super(Prenet, self).__init__()
        
        self.pre_net = nn.Sequential(
            nn.Linear(in_dim, out_dim),
            nn.ReLU(),
            nn.Dropout(p=drop_out_p),
            nn.Linear(out_dim, out_dim),
            nn.ReLU(),
            nn.Dropout(p=drop_out_p)
        )

    def forward(self, x):
        output = self.pre_net(x)

        return output

class Decoder(nn.Module):
    def __init__(self, target_dim, pre_net_dim, rnn_hidden_size, second_rnn_hidden_size, postnet_hidden_size, n_layers, dropout, attention_type):
        super(Decoder, self).__init__()

        self.pre_net = Prenet(target_dim, pre_net_dim, dropout)

        if attention_type == "DotProduct":
            self.attention_layer = DotProductAttention()

        elif attention_type == "LocationSensitive":
            self.attention_layer = LocationSensitiveAttention(512, 512, 128, 32, 31)
        
        else:
            print("no attention")

        self.rnn_hidden_size = rnn_hidden_size

        self.projection_layer = nn.Linear(rnn_hidden_size*2, target_dim, False)
        
        self.attention_rnn = nn.LSTMCell(pre_net_dim+rnn_hidden_size,rnn_hidden_size)
        
        self.decoder_rnn = nn.LSTMCell(1024,rnn_hidden_size)


        self.rnn_projection = nn.Sequential(
            nn.Linear(in_features=rnn_hidden_size, out_features=rnn_hidden_size, bias=True),
            nn.ReLU()
        )

        self.memory_layer = nn.Linear(in_features=512, out_features=128, bias=True)

        self.postnet = Postnet(target_dim, postnet_hidden_size, dropout, int((5 - 1) / 2)) # 5 = kernel size

        self.attention_type = attention_type

    def forward_step(self, encoder_inputs, decoder_input):
        """
        PARAMS
        ------
        encoder_inputs: Encoder outputs. (B, T, e_F)
        decoder_input: Prenet output. (B, p_F)
                
        RETURNS(보류)
        -------
        mel_outputs: mel outputs from the decoder
        gate_outputs: gate outputs from the decoder
        alignments: sequence of attention weights from the decoder
        """
        cell_input_1 = torch.cat((decoder_input, self.attention_context), -1) # pre-net and attention context vector concatenated
        
        self.attention_hidden, self.attention_cell = self.attention_rnn(cell_input_1, (self.attention_hidden, self.attention_cell))

        attention_weights_cat = torch.cat(
            (self.attention_weights.unsqueeze(1),
             self.attention_weights_cum.unsqueeze(1)), dim=1)

        if self.attention_type == "DotProduct":
            attention_context, attn = self.attention_layer(self.attention_hidden, encoder_inputs, encoder_inputs) 

        elif self.attention_type == "LocationSensitive":
            self.attention_context, self.attention_weights = self.attention_layer(self.attention_hidden, encoder_inputs, self.memory, attention_weights_cat)
            self.attention_weights_cum += self.attention_weights
            decoder_input = torch.cat((self.attention_hidden, self.attention_context), -1)

            self.decoder_hidden, self.decoder_cell = self.decoder_rnn(decoder_input, (self.decoder_hidden, self.decoder_cell))
            decoder_hidden_attention_context = torch.cat((self.decoder_hidden, self.attention_context), dim=1) # concatenation of the LSTM output and attention context vector
            decoder_output = self.projection_layer(decoder_hidden_attention_context) # projected through a linear transform
        else:
            pass

        return decoder_output, self.attention_weights
   
    def forward(self, encoder_inputs, decoder_inputs):
        """ 
        Decoder forward pass for training
        PARAMS
        ------
        encoder_inputs: Encoder outputs (B, T, e_F)
        decoder_inputs: Decoder inputs for teacher forcing. (B, T, d_F)
        
        RETURNS(보류)
        -------
        mel_outputs: mel outputs from the decoder
        gate_outputs: gate outputs from the decoder
        alignments: sequence of attention weights from the decoder
        """
        #init part
        batch_size, max_len, decoder_dim = decoder_inputs.size()
        _, encoder_max_len, _ = encoder_inputs.size()

        go_frame = torch.zeros(batch_size, 1, decoder_dim)
        if encoder_inputs.is_cuda: go_frame = go_frame.cuda()

        self.attention_hidden = torch.zeros(batch_size, self.rnn_hidden_size) # (n_layer, bathch sise, hidden_size)
        self.attention_cell = torch.zeros(batch_size, self.rnn_hidden_size)

        self.decoder_hidden = torch.zeros(batch_size, self.rnn_hidden_size)
        self.decoder_cell = torch.zeros(batch_size, self.rnn_hidden_size)

        self.attention_context = torch.zeros(batch_size, self.rnn_hidden_size)

        self.attention_weights_cum = torch.zeros(batch_size, encoder_max_len)
        self.attention_weights = torch.zeros(batch_size, encoder_max_len)

        self.memory = self.memory_layer(encoder_inputs)

        if encoder_inputs.is_cuda:
            self.attention_context = self.attention_context.cuda()
            self.attention_cell = self.attention_cell.cuda()
            self.attention_hidden = self.attention_hidden.cuda()

            self.decoder_hidden = self.decoder_hidden.cuda()
            self.decoder_cell = self.decoder_cell.cuda()

            self.attention_weights_cum = self.attention_weights_cum.cuda()
            self.attention_weights = self.attention_weights.cuda()

        # teacher forcing pre_net
        decoder_inputs = torch.cat((go_frame, decoder_inputs), dim=1)
        decoder_inputs = self.pre_net(decoder_inputs) # B, T, F (prenet output)
        decoder_inputs = decoder_inputs.transpose(0, 1).contiguous() # T, B, F

        mel_outputs, alignments = [], []
        
        while len(mel_outputs) < decoder_inputs.size(0) - 1:
            decoder_input = decoder_inputs[len(mel_outputs)]
            mel_output, attention_weights = self.forward_step(encoder_inputs, decoder_input)
            
            mel_outputs += [mel_output.squeeze(1)]
            alignments += [attention_weights]

        mel_outputs = torch.stack(mel_outputs).transpose(0, 1).contiguous()

        mel_outputs_postnet = self.postnet(mel_outputs)
        final_mel_outputs = mel_outputs + mel_outputs_postnet.transpose(1, 2)
        
        return final_mel_outputs

    def inference(self, encoder_inputs, decoder_inputs):
        if decoder_inputs == None:
            pass

        else:
            batch_size, max_len, _ = decoder_inputs.size()
            
            go_frame = torch.zeros(decoder_inputs.shape[0], 1, decoder_inputs.shape[2])
                
            if encoder_inputs.is_cuda: go_frame = go_frame.cuda()

            mel_outputs, gate_outputs, alignments = [], [], []

            decoder_inputs = decoder_inputs.transpose(0, 1).contiguous() # T, B, F
            
            attention_context = torch.zeros(batch_size, self.rnn_hidden_size)
            if encoder_inputs.is_cuda: attention_context = attention_context.cuda()
            
            decoder_input = go_frame
            while len(mel_outputs) < max_len - 1:
                decoder_input = self.pre_net(decoder_input) # B, T, F (prenet output) 
                decoder_input = decoder_input.squeeze(0)
                
                mel_output, attention_context = self.forward_step(encoder_inputs, decoder_input, attention_context)                
                decoder_input = mel_output

                mel_outputs += [mel_output.squeeze(1)]

        mel_outputs = torch.stack(mel_outputs).transpose(0, 1).contiguous()
        
        mel_outputs_postnet = self.postnet(mel_outputs)
        final_mel_outputs = mel_outputs + mel_outputs_postnet.transpose(1, 2)

        return final_mel_outputs    


   

    