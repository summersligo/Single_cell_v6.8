import torch
import torch.nn as nn
from torch.nn import Parameter
import numpy as np

class Actor(nn.Module):
    def __init__(self, args):
    # def __init__(self, flatten_dim , high_space_dim, weight_dim=32, hidden=64):
        super(Actor, self).__init__()
        self.args = args
        self.hidden_dim = self.args.rnn_hidden
        self.weight_dim = self.args.weight_dim
        self.flatten_dim = self.args.flatten_dim
        self.embedding_dim = self.args.embedding_dim
        self.high_space_dim = self.args.high_space_dim
        self.rnn_input_dim = self.args.rnn_input_dim
        self.noise_dim = self.args.noise_dim
        self.embedding_noise = nn.Linear(2, self.noise_dim)
        # 定义探索率
        self.epsilon = self.args.epsilon
        self.embedding_layer = nn.Linear(self.flatten_dim, self.embedding_dim)
        self.linear1 = nn.Linear(self.embedding_dim+1+self.noise_dim, self.high_space_dim)
        self.linear2 = nn.Linear(self.high_space_dim, self.rnn_input_dim)
        self.Encoder = nn.GRU(self.rnn_input_dim, self.hidden_dim, batch_first=True)
        self.Decoder = nn.GRU(self.rnn_input_dim, self.hidden_dim, batch_first=True)
        self.Encoder_affine = nn.Linear(self.hidden_dim, self.weight_dim)
        self.Decoder_affine = nn.Linear(self.hidden_dim, self.weight_dim)
        self.V = Parameter(torch.rand(1, self.weight_dim))
        # self.hidden_out = nn.Linear(3*self.hidden_dim, self.hidden_dim)
        self.Encoder_init_input = Parameter(torch.rand(1, self.hidden_dim))
        self.Decoder_init_input = Parameter(torch.rand(1, self.rnn_input_dim))
        self.tanh=nn.Tanh()

    def mask_weight(self, weight_vector, mask, index_vector):
        local_mask = mask.clone()
        if index_vector is None:
            return weight_vector, local_mask
        else:
            local_mask[index_vector] = True    
            weight_vector[local_mask] = -np.inf
            return weight_vector, local_mask

    def forward(self, channel_matrix, reward_array,noise,sample=True):
        # 默认输入进来的信道矩阵是batch * 20 * 32的
        input_data = 1e5 * channel_matrix
        embedding_noise = self.embedding_noise(noise)
        embedding_noise = self.tanh(embedding_noise)
        embedding_noise = embedding_noise.unsqueeze(0).repeat(input_data.shape[0],1)
        embedding_data = self.embedding_layer(input_data)
        preprocess_data = torch.cat((embedding_data, reward_array,embedding_noise), 1)
        high_dim_data_1 = torch.tanh(self.linear1(preprocess_data))
        # print(high_dim_data_1.shape)
        # print(embedding_noise.shape)
        high_dim_data = self.linear2(high_dim_data_1).unsqueeze(0)
        # print(high_dim_data.shape)
        batch_size = high_dim_data.shape[0]
        seq_len = high_dim_data.shape[1]
        total_len = seq_len + 1
        init_decoder = self.Encoder_init_input.repeat(batch_size,1).unsqueeze(0)
        Encoder_hidden_result, Encoder_hidden_output = self.Encoder(high_dim_data, init_decoder)
        Encoder_hidden_result = torch.cat((init_decoder.permute(1,0,2), Encoder_hidden_result),1)
        mask = torch.zeros(total_len).bool()
        selected_user = None
        result = []
        affine_encoder = self.Encoder_affine(Encoder_hidden_result) # batch_size * word_num + 1 * weight_dim
        input_decoder = self.Decoder_init_input.repeat(batch_size, 1).unsqueeze(1) # batch_size* 1 * input_dim
        hidden_decoder = Encoder_hidden_output # 1*batch_size * hidden_dim
        schedule_result = []
        while True:
            # 第一步使用Deocder进行解码操作
            _, hidden_decoder =  self.Decoder(input_decoder, hidden_decoder)  
            # 第二步使用attention机制,计算出对应的权重向量
            # 首先通过广播机制, 将hidden_decoder进行repeat操作
            expand_hidden_decoder = self.Decoder_affine(hidden_decoder).repeat(1,total_len,1) # batch_size *word_num+1* weight_dim               
            # 将权重向量进行expand操作
            expand_V = self.V.repeat(batch_size,1).unsqueeze(1) # batch_size * weight_dim * 1
            weight_vector = torch.bmm(expand_V, torch.tanh(affine_encoder + expand_hidden_decoder).permute(0,2,1)).squeeze() # batch_size * word_num+1 * 1
            # 通过selected_user 手动修改weight_vector
            weight_vector, mask = self.mask_weight(weight_vector, mask, selected_user)
            # 这个地方首先需要返回概率的值,其次需要返回调用的用户的索引是什么
            # 先对weight_vector进行softmax激活
            prob_matrix = torch.softmax(weight_vector, 0)
            # 返回用户选择向量,采用探索策略，探索率是0.05
            if sample:
                if torch.rand(1) < self.args.epsilon:
                    selected_user = torch.multinomial(prob_matrix, 1).squeeze()
                else:
                    selected_user = torch.argmax(prob_matrix)
            else:
                selected_user = torch.argmax(prob_matrix)
            prob_vector = prob_matrix.gather(0, index=selected_user) #size [1]的一个tensor
            result.append(prob_vector)
            # 首先根据selected_user选择出哪些batch将要取消
            if selected_user.item() == 0:
                break
            schedule_result.append(selected_user.item()-1)
            input_decoder = high_dim_data[:,selected_user.item()-1,:].unsqueeze(1)
        log_prob=torch.log(torch.prod(torch.stack(result,dim=0)))
        # print(log_prob)
        # print(schedule_result)
        schedule_result=torch.tensor(schedule_result,device=prob_vector.device)
        return log_prob, schedule_result
        
class Critic(nn.Module):
    def __init__(self, args):
        super(Critic, self).__init__()
        self.args = args
        self.device = 'cuda' if self.args.cuda else 'cpu'
        self.embedding_dim = self.args.embedding_dim
        self.flatten_dim = self.args.flatten_dim
        self.embedding_layer = nn.Linear(self.flatten_dim, self.embedding_dim)
        self.fc_layer_number = self.args.fc_layer_number
        self.hidden_dim = self.args.hidden_dim
        input_dim = self.embedding_dim + 1
        self.fc_net = []
        for layer in range(self.fc_layer_number):
            self.fc_net.append(nn.Linear(input_dim, self.hidden_dim[layer]))
            input_dim = self.hidden_dim[layer]
        self.fc_net=nn.Sequential(*self.fc_net)
        self.flatten = nn.Flatten()
        self.ouput_layer = nn.Linear(input_dim*self.args.user_antennas * self.args.user_numbers, 1)

    def forward(self, channel_matrix, reward_array):
        input_data = 1e6 * channel_matrix
        embedding_data = self.embedding_layer(input_data)
        fc_data = torch.cat((embedding_data, reward_array), 2)
        for layer in range(self.fc_layer_number):
            fc_data = self.fc_net[layer](fc_data)
        flatten_vector = self.flatten(fc_data)
        value = self.ouput_layer(flatten_vector)
        return value


class Critic_2(nn.Module):
    
    def __init__(self, args):
        super(Critic_2, self).__init__()
        self.args = args
        self.device = 'cuda' if self.args.cuda else 'cpu'
        self.embedding_dim = self.args.embedding_dim
        self.flatten_dim = self.args.flatten_dim
        self.embedding_layer = nn.Linear(self.flatten_dim, self.embedding_dim)
        self.fc_layer_number = self.args.fc_layer_number
        self.hidden_dim = self.args.hidden_dim
        input_dim = self.embedding_dim + 1
        self.fc_net = []
        for layer in range(self.fc_layer_number):
            self.fc_net.append(nn.Linear(input_dim, self.hidden_dim[layer]))
            input_dim = self.hidden_dim[layer]
        self.fc_net=nn.Sequential(*self.fc_net)
        self.flatten = nn.Flatten()
        self.ouput_layer = nn.Linear(input_dim*self.args.user_antennas * self.args.user_numbers, 1)

    def forward(self, channel_matrix, reward_array):
        input_data = 1e6 * channel_matrix
        embedding_data = self.embedding_layer(input_data)
        fc_data = torch.cat((embedding_data, reward_array), 2)
        for layer in range(self.fc_layer_number):
            fc_data = self.fc_net[layer](fc_data)
        flatten_vector = self.flatten(fc_data)
        value = self.ouput_layer(flatten_vector)
        return value

