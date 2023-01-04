import numpy as np
import scipy.sparse as sp
import torch
import torch.nn as nn

from model import Model
from utils import *

from sklearn.metrics import roc_auc_score
import random
import os
import dgl

import argparse
from tqdm import tqdm

os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

# Set argument
parser = argparse.ArgumentParser(description='CoLA: Self-Supervised Contrastive Learning for Anomaly Detection')
parser.add_argument('--dataset', type=str, default='cora')  # 'BlogCatalog'  'Flickr'  'ACM'  'cora'  'citeseer'  'pubmed'

# learning rate
parser.add_argument('--lr', type=float)

parser.add_argument('--weight_decay', type=float, default=0.0)
parser.add_argument('--seed', type=int, default=1)

# embedding dimensions - 64
parser.add_argument('--embedding_dim', type=int, default=64)

# no of epochs - complete training cycle - whole dataset
parser.add_argument('--num_epoch', type=int)

# ????
parser.add_argument('--drop_prob', type=float, default=0.0)

# batch size
parser.add_argument('--batch_size', type=int, default=300)

# subgraph size
parser.add_argument('--subgraph_size', type=int, default=4)

# readout module
parser.add_argument('--readout', type=str, default='avg')  #max min avg  weighted_sum

# R
parser.add_argument('--auc_test_rounds', type=int, default=256)

#  i think negative to positive sample ratio
parser.add_argument('--negsamp_ratio', type=int, default=1)

args = parser.parse_args()

# learning rate = page 10 of paper - bottom left
if args.lr is None:
    if args.dataset in ['cora','citeseer','pubmed','Flickr']:
        # 0.001
        args.lr = 1e-3
    elif args.dataset == 'ACM':
        # 0.0005
        args.lr = 5e-4
    elif args.dataset == 'BlogCatalog':
        # 0.003
        args.lr = 3e-3

# no of epochs = page 10 of paper - bottom left
if args.num_epoch is None:
    if args.dataset in ['cora','citeseer','pubmed']:
        args.num_epoch = 100
    elif args.dataset in ['BlogCatalog','Flickr','ACM']:
        args.num_epoch = 400

batch_size = args.batch_size
subgraph_size = args.subgraph_size

print('Dataset: ',args.dataset)

# ----------------------



# Set random seed
dgl.random.seed(args.seed)
np.random.seed(args.seed)
torch.manual_seed(args.seed)
torch.cuda.manual_seed(args.seed)
torch.cuda.manual_seed_all(args.seed)
random.seed(args.seed)
os.environ['PYTHONHASHSEED'] = str(args.seed)
os.environ['OMP_NUM_THREADS'] = '1'
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = False

# Load and preprocess data
adj, features, labels, idx_train, idx_val,\
idx_test, ano_label, str_ano_label, attr_ano_label = load_mat(args.dataset)




# adj = adjacency matrix - network
# features = Attribute vector
# labels  = 
# idx_train = training sample indices
# idx_val,
# idx_test = testing sample indexes
# ano_label = anomally label
# str_ano_label = structural anomally label 
# attr_ano_label = attribute anomally label


features, _ = preprocess_features(features)
dgl_graph = adj_to_dgl_graph(adj)
nb_nodes = features.shape[0]
ft_size = features.shape[1]

# can't understand this
nb_classes = labels.shape[1]

# Can't understand why did we normalize
adj = normalize_adj(adj)

# adding self loops i think
adj = (adj + sp.eye(adj.shape[0])).todense()


features = torch.FloatTensor(features[np.newaxis])
adj = torch.FloatTensor(adj[np.newaxis])
labels = torch.FloatTensor(labels[np.newaxis])
idx_train = torch.LongTensor(idx_train)
idx_val = torch.LongTensor(idx_val)
idx_test = torch.LongTensor(idx_test)


# ------------------------------


# Initialize model and optimiser

# RelU is the activation function
model = Model(ft_size, args.embedding_dim, 'prelu', args.negsamp_ratio, args.readout)

# Optimiser is Adam
optimiser = torch.optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)

if torch.cuda.is_available():
    print('Using CUDA')
    model.cuda()
    features = features.cuda()
    adj = adj.cuda()
    labels = labels.cuda()
    idx_train = idx_train.cuda()
    idx_val = idx_val.cuda()
    idx_test = idx_test.cuda()



if torch.cuda.is_available():
    b_xent = nn.BCEWithLogitsLoss(reduction='none', pos_weight=torch.tensor([args.negsamp_ratio]).cuda())
else:
    b_xent = nn.BCEWithLogitsLoss(reduction='none', pos_weight=torch.tensor([args.negsamp_ratio]))


xent = nn.CrossEntropyLoss()
cnt_wait = 0

# 1000000000.0
best = 1e9

best_t = 0


batch_num = nb_nodes // batch_size + 1



added_adj_zero_row = torch.zeros((nb_nodes, 1, subgraph_size))
added_adj_zero_col = torch.zeros((nb_nodes, subgraph_size + 1, 1))
added_adj_zero_col[:,-1,:] = 1.

# possibly constructing zero vector for initial node in subgraph - anonymization
added_feat_zero_row = torch.zeros((nb_nodes, 1, ft_size))


if torch.cuda.is_available():
    added_adj_zero_row = added_adj_zero_row.cuda()
    added_adj_zero_col = added_adj_zero_col.cuda()
    added_feat_zero_row = added_feat_zero_row.cuda()

# ----------------------------

# Train model - tqdm progress bar
with tqdm(total=args.num_epoch) as pbar:
    pbar.set_description('Training')

    # Training for epochs - complete dataset = one epoch
    for epoch in range(args.num_epoch):

        loss_full_batch = torch.zeros((nb_nodes,1))
        if torch.cuda.is_available():
            loss_full_batch = loss_full_batch.cuda()

        model.train()

        all_idx = list(range(nb_nodes))
        
        # ------------------------------------------------------------
        random.shuffle(all_idx)
        total_loss = 0.

        # total number of subgraphs generated = total no of nodes.
        subgraphs = generate_rwr_subgraph(dgl_graph, subgraph_size)


        for batch_idx in range(batch_num):

            optimiser.zero_grad()

            is_final_batch = (batch_idx == (batch_num - 1))

            if not is_final_batch:
                idx = all_idx[batch_idx * batch_size: (batch_idx + 1) * batch_size]
            else:
                idx = all_idx[batch_idx * batch_size:]

            cur_batch_size = len(idx)

            # labels : 0 for negative and 1 for positive instance pairs
            lbl = torch.unsqueeze(torch.cat((torch.ones(cur_batch_size), torch.zeros(cur_batch_size * args.negsamp_ratio))), 1)
            
            ba = []
            bf = []

            added_adj_zero_row = torch.zeros((cur_batch_size, 1, subgraph_size))
            added_adj_zero_col = torch.zeros((cur_batch_size, subgraph_size + 1, 1))
            
            # adding a new node to the subgraph??
            added_adj_zero_col[:, -1, :] = 1.
            
            added_feat_zero_row = torch.zeros((cur_batch_size, 1, ft_size))

            if torch.cuda.is_available():
                lbl = lbl.cuda()
                added_adj_zero_row = added_adj_zero_row.cuda()
                added_adj_zero_col = added_adj_zero_col.cuda()
                added_feat_zero_row = added_feat_zero_row.cuda()

            for i in idx:
                # ************************************************ #
                # very important line, how is shuffled indexes and subgraphs associated
                
                # what does this line do??

                cur_adj = adj[:, subgraphs[i], :][:, :, subgraphs[i]]

                cur_feat = features[:, subgraphs[i], :]
                ba.append(cur_adj)
                bf.append(cur_feat)
            
            # converted from 3D to 2D matrix
            ba = torch.cat(ba)
            
            # adding extra last zero row and zero col in all ba's and bf's 
            # why ????????????????
            ba = torch.cat((ba, added_adj_zero_row), dim=1)
            ba = torch.cat((ba, added_adj_zero_col), dim=2)

            

            # ------------------------------------
            bf = torch.cat(bf)
            
            # attribute vector = zero for initial/target node in subgraph
            # very weird

            # converted from a 3D matrix to a 2-D matrix and 
            # also added zero row in second last position
            bf = torch.cat((bf[:, :-1, :], added_feat_zero_row, bf[:, -1:, :]),dim=1)
            # ------------------------------------
            
            logits = model(bf, ba)
            
            # b-xent is the loss function
            loss_all = b_xent(logits, lbl)

            loss = torch.mean(loss_all)

            # logits and loss all - both have size 600 = size of labels = batch_size*2
            # probably the negative sample is coming automatically from the model

            loss.backward()
            optimiser.step()

            # loss for each batch 
            loss = loss.detach().cpu().numpy()



            # only considering loss from positive samples
            loss_full_batch[idx] = loss_all[: cur_batch_size].detach()

            if not is_final_batch:
                total_loss += loss

        mean_loss = (total_loss * batch_size + loss * cur_batch_size) / nb_nodes

        if mean_loss < best:
            best = mean_loss
            best_t = epoch
            cnt_wait = 0
            # saving the improved model state in a file
            torch.save(model.state_dict(), 'best_model.pkl')
        else:
            cnt_wait += 1

        pbar.set_postfix(loss=mean_loss)
        pbar.update(1)

# -------------------

# Test model
print('Loading {}th epoch'.format(best_t))
model.load_state_dict(torch.load('best_model.pkl'))

multi_round_ano_score = np.zeros((args.auc_test_rounds, nb_nodes))

# positive instance scores
multi_round_ano_score_p = np.zeros((args.auc_test_rounds, nb_nodes))

# negative instance scores
multi_round_ano_score_n = np.zeros((args.auc_test_rounds, nb_nodes))

with tqdm(total=args.auc_test_rounds) as pbar_test:
    pbar_test.set_description('Testing')
    for round in range(args.auc_test_rounds):

        all_idx = list(range(nb_nodes))
        random.shuffle(all_idx)

        subgraphs = generate_rwr_subgraph(dgl_graph, subgraph_size)

        for batch_idx in range(batch_num):

            optimiser.zero_grad()

            is_final_batch = (batch_idx == (batch_num - 1))

            if not is_final_batch:
                idx = all_idx[batch_idx * batch_size: (batch_idx + 1) * batch_size]
            else:
                idx = all_idx[batch_idx * batch_size:]

            cur_batch_size = len(idx)

            ba = []
            bf = []
            added_adj_zero_row = torch.zeros((cur_batch_size, 1, subgraph_size))
            added_adj_zero_col = torch.zeros((cur_batch_size, subgraph_size + 1, 1))
            added_adj_zero_col[:, -1, :] = 1.
            added_feat_zero_row = torch.zeros((cur_batch_size, 1, ft_size))

            if torch.cuda.is_available():
                lbl = lbl.cuda()
                added_adj_zero_row = added_adj_zero_row.cuda()
                added_adj_zero_col = added_adj_zero_col.cuda()
                added_feat_zero_row = added_feat_zero_row.cuda()

            for i in idx:
                cur_adj = adj[:, subgraphs[i], :][:, :, subgraphs[i]]
                cur_feat = features[:, subgraphs[i], :]
                ba.append(cur_adj)
                bf.append(cur_feat)

            ba = torch.cat(ba)
            ba = torch.cat((ba, added_adj_zero_row), dim=1)
            ba = torch.cat((ba, added_adj_zero_col), dim=2)
            bf = torch.cat(bf)
            bf = torch.cat((bf[:, :-1, :], added_feat_zero_row, bf[:, -1:, :]), dim=1)

            with torch.no_grad():
                logits = torch.squeeze(model(bf, ba))
                logits = torch.sigmoid(logits)
                
                

            # difference of negative and positive pairs
            ano_score = - (logits[:cur_batch_size] - logits[cur_batch_size:]).cpu().numpy()
            multi_round_ano_score[round, idx] = ano_score

        pbar_test.update(1)


ano_score_final = np.mean(multi_round_ano_score, axis=0)
# ano_score_final_p = np.mean(multi_round_ano_score_p, axis=0)
# ano_score_final_n = np.mean(multi_round_ano_score_n, axis=0)
auc = roc_auc_score(ano_label, ano_score_final)

print('AUC:{:.4f}'.format(auc))

