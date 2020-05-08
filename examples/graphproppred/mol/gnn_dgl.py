import torch
import torch.nn as nn
import torch.nn.functional as F

import dgl

from ogb.graphproppred.mol_encoder import AtomEncoder,BondEncoder


class GatedGCNLayer(nn.Module):
    def __init__(self, input_dim, output_dim, dropout=0.0, graph_norm=True, batch_norm=True, residual=True):
        super().__init__()
        self.dropout = dropout
        self.graph_norm = graph_norm
        self.batch_norm = batch_norm
        self.residual = residual
        
        if input_dim != output_dim:
            self.residual = False
        
        self.A = nn.Linear(input_dim, output_dim, bias=True)
        self.B = nn.Linear(input_dim, output_dim, bias=True)
        self.C = nn.Linear(input_dim, output_dim, bias=True)
        self.D = nn.Linear(input_dim, output_dim, bias=True)
        self.E = nn.Linear(input_dim, output_dim, bias=True)
        
        self.bn_node_h = nn.BatchNorm1d(output_dim)
        self.bn_node_e = nn.BatchNorm1d(output_dim)
        
        if dropout != 0.0:
            self.drop_h = nn.Dropout(dropout)
            self.drop_e = nn.Dropout(dropout)

    def message_func(self, edges):
        Bh_j = edges.src['Bh']    
        e_ij = edges.data['Ce'] +  edges.src['Dh'] + edges.dst['Eh'] # e_ij = Ce_ij + Dhi + Ehj
        edges.data['e'] = e_ij
        return {'Bh_j' : Bh_j, 'e_ij' : e_ij}

    def reduce_func(self, nodes):
        Ah_i = nodes.data['Ah']
        Bh_j = nodes.mailbox['Bh_j']
        e = nodes.mailbox['e_ij'] 
        sigma_ij = torch.sigmoid(e)
        h = Ah_i + torch.sum( sigma_ij * Bh_j, dim=1 ) / ( torch.sum( sigma_ij, dim=1 ) + 1e-6 )
        return {'h' : h}
    
    def forward(self, g, h, e, snorm_n, snorm_e):
        
        h_in = h  # for residual connection
        e_in = e  # for residual connection
        
        g.ndata['h']  = h 
        g.ndata['Ah'] = self.A(h) 
        g.ndata['Bh'] = self.B(h) 
        g.ndata['Dh'] = self.D(h)
        g.ndata['Eh'] = self.E(h) 
        
        g.edata['e']  = e 
        g.edata['Ce'] = self.C(e) 
        
        g.update_all(self.message_func, self.reduce_func) 
        h = g.ndata['h']  # result of graph convolution
        e = g.edata['e']  # result of graph convolution
        
        if self.graph_norm:
            h = h * snorm_n  # normalize activation w.r.t. graph size
            e = e * snorm_e  # normalize activation w.r.t. graph size
        
        if self.batch_norm:
            h = self.bn_node_h(h)  # batch normalization  
            e = self.bn_node_e(e)  # batch normalization  
        
        h = F.relu(h)  # non-linear activation
        e = F.relu(e)  # non-linear activation
        
        if self.residual:
            h = h_in + h  # residual connection
            e = e_in + e  # residual connection
            
        if self.dropout != 0:
            h = self.drop_h(h)  # dropout  
            e = self.drop_e(e)  # dropout
        
        return h, e


class GNN(nn.Module):
    
    def __init__(self, gnn_type, num_tasks, num_layer=4, emb_dim=256, 
                 dropout=0.0, graph_norm=True, batch_norm=True, 
                 residual=True, graph_pooling="mean"):
        super().__init__()
        
        self.num_tasks = num_tasks
        self.num_layer = num_layer
        self.emb_dim = emb_dim
        self.dropout = dropout
        self.graph_norm = graph_norm
        self.batch_norm = batch_norm
        self.residual = residual
        self.graph_pooling = graph_pooling
        
        self.atom_encoder = AtomEncoder(emb_dim)
        self.bond_encoder = BondEncoder(emb_dim)
        
        gnn_layer = {
            'gated-gcn': GatedGCNLayer,
        }.get(gnn_type, GatedGCNLayer)
         
        self.layers = nn.ModuleList([
            gnn_layer(emb_dim, emb_dim, dropout, graph_norm, batch_norm, residual) 
                for _ in range(num_layer) 
        ])
        
        self.pooler = {
            "mean": dgl.mean_nodes,
            "sum": dgl.sum_nodes,
            "max": dgl.max_nodes,
        }.get(graph_pooling, dgl.mean_nodes)
        
        self.graph_pred_linear = torch.nn.Linear(emb_dim, num_tasks)
        
    def forward(self, g, h, e, snorm_n, snorm_e):
        
        h = self.atom_encoder(h)
        e = self.bond_encoder(e)
        
        for conv in self.layers:
            h, e = conv(g, h, e, snorm_n, snorm_e)
        g.ndata['h'] = h
        
        hg = self.pooler(g, 'h')
        
        return self.graph_pred_linear(hg)
