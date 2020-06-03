"""
Results:

With train / test splits:
    -Defaults: [(3,8),(3,8)], epochs = 300, learning rate = 0.01, batch size = 100
        train loss: 1.150081 -- train acc 0.585 -- test loss 1.230376 -- test acc 0.530
        2-hop: 0.13875
        2-target: 0.70125

    -hidden_layers = [(3,16),(3,16),(3,16)]learning rate = 0.001
        1) epochs = 500: train loss: 0.860433 -- train acc 0.669 -- test loss 1.238347 -- test acc 0.535
        training 2hop: 0.1875
        training 2-target: 0.775
        2) epochs = 1000: train loss: 0.660450 -- train acc 0.748 -- test loss 1.326550 -- test acc 0.610
        train/test 2-hop: 0.21374999 0.145
        train/test 2-target: 0.7875 0.75

        3) (diff data) train loss: 0.688547 -- train acc 0.725 -- test loss 1.850561 -- test acc 0.520
        0.14 0.185
        0.7775 0.73

        4) (other diff data) train loss: 0.831934 -- train acc 0.675 -- test loss 1.290683 -- test acc 0.545
        2hop binary: 0.14874999 0.12
        2hop dist: 0.229 0.815
        2-target: 0.75375 0.745
        reversed Test loss: 1.631899, Test acc: 0.480



    -hidden_layers = [(3,32),(3,32)], epochs = 500, learning rate = 0.001

    -hidden_layers = [(3,32),(3,32),(3,16)], epochs = 2000
        train loss: 0.611045 -- train acc 0.745 -- test loss 2.506877 -- test acc 0.505
        0.11625 0.105
        0.7975 0.725


    -hidden_layers = [(3,16),(3,16),(3,16)], epochs = 200; L_upper = L_lower
        gets to loss: 1.207, acc: 0.544, then NaNs


# todo
#   predict distributions, multihop (todo use distributions, not binary), etc (stuff from paper)
#   try other accuracy measurements
#   save models after testing
#   Experiment: reversing flows, then testing w/ ours and GRETEL (or boomerang shaped flows)
#   ablations: remove L1_upper, nonlinearities, etc

## Multi hop:
    # todo test 3-hop; try only predicting over last ?? nodes of prefix each time
"""
import os

import jax.numpy as np
from jax.scipy.special import logsumexp
import numpy as onp


# from synthetic_analysis.synthetic_sc_walk import load_training_data, generate_training_data, save_training_data
# from synthetic_analysis.hodge_trajectory_model import Hodge_GCN
try:
    from synthetic_analysis.synthetic_sc_walk import load_training_data, generate_training_data, save_training_data, neighborhood, conditional_incidence_matrix, generate_reversed_flows
    from synthetic_analysis.hodge_trajectory_model import Hodge_GCN
except Exception:
    from synthetic_sc_walk import load_training_data, generate_training_data, save_training_data, neighborhood, conditional_incidence_matrix, generate_reversed_flows
    from hodge_trajectory_model import Hodge_GCN

import sys

def hyperparams():
    """
    Parse hyperparameters from command line

    -hyperparam value

    For hidden_layers, input [(3, 8), (3, 8)] as 3_8_3_8
    """
    args = sys.argv
    hyperparams = {'epochs': 100,
                   'learning_rate': 0.001,
                   'batch_size': 100,
                   'hidden_layers': [(3, 8), (3, 8)],
                   'describe': 0,
                   'reverse': 0,
                   'load_data': 1,
                   'load_model': 0}

    for i in range(len(args) - 1):
        if args[i][0] == '-':
            if args[i][1:] == 'hidden_layers':
                nums = list(map(int, args[i + 1].split("_")))
                print(nums)
                hyperparams['hidden_layers'] = []
                for j in range(0, len(nums), 2):
                    hyperparams['hidden_layers'] += [(nums[j], nums[j + 1])]

            else:
                hyperparams[args[i][1:]] = float(args[i+1])
    print(hyperparams)
    return hyperparams['epochs'], hyperparams['learning_rate'], hyperparams['batch_size'], hyperparams['hidden_layers'], \
            hyperparams['reverse'], hyperparams['describe'], hyperparams['load_data'], hyperparams['load_model']

# Define a model
def relu(x):
    return np.maximum(x, 0)

def hodge_parallel_variable(weights, S_lower, S_upper, Bcond_func, last_node, flow):
    """
    Hodge parallel model with variable number of layers
    """
    n_layers = (len(weights) - 1) / 3
    assert n_layers % 1 == 0, 'wrong number of weights'

    cur_out = flow
    for i in range(int(n_layers)):
        cur_out = cur_out @ weights[i * 3] \
                  + S_lower @ cur_out @ weights[i*3 + 1] \
                  + S_upper @ cur_out @ weights[i*3 + 2]

        cur_out = relu(cur_out)

    logits = Bcond_func(last_node) @ cur_out @ weights[-1]
    return logits - logsumexp(logits)

def hodge_parallel(weights, S0, S1, Bcond, flows):
    W00, W10, W20, W01, W11, W21, Wf = weights

    g0 = np.dot(flows, W00) + np.dot(np.dot(S0, flows), W10) + np.dot(np.dot(S1, flows), W20)
    g1 = relu(g0)

    h0 = np.dot(g1, W01) + np.dot(np.dot(S0, g1), W11) + np.dot(np.dot(S1, g1), W21)
    h1 = relu(h0)

    logits = np.dot(np.dot(Bcond, h1), Wf)

    return logits - logsumexp(logits)


def data_setup(hops=(1,), load=True):
    """
    Imports and sets up flow, target, and shift matrices for model training. Supports generating data for multiple hops
        at once
    """
    inputs_all, y_all, target_nodes_all = [], [], []
    if not load:
        # Generate data
        Xs, B_matrices, ys, train_mask, test_mask, G_undir, last_nodes, suffixes = generate_training_data(400, 1000, hops=hops)
        target_nodes_all = [[] * len(suffixes[0])]
        for i in range(len(suffixes[0])):  # each hop
            for j in range(len(suffixes)): # each suffix
                target_nodes_all[i].append(suffixes[j][i])
    for i in range(len(hops)):
        if load:
            # Load data
            folder = 'trajectory_data_' + str(hops[i]) + 'hop'
            X, B_matrices, y, train_mask, test_mask, G_undir, last_nodes, target_nodes = load_training_data(folder)
            B1, B2 = B_matrices
            target_nodes_all.append(target_nodes)
        else:
            B1, B2, _ = B_matrices
            X, y = Xs[i], ys[i]
            save_training_data(Xs[i], B1, B2, ys[i], train_mask, test_mask, G_undir, last_nodes, target_nodes_all[i], 'trajectory_data_' + str(hops[i]) + 'hop')


        inputs_all.append([None, np.array(last_nodes), X])
        y_all.append(y)

        # Define shifts
        L1_lower = B1.T @ B1
        L1_upper = B2 @ B2.T
        shifts = [L1_lower, L1_upper]



    # Build E_lookup for multi-hop training
    e = onp.nonzero(B1.T)[1]
    edges = onp.array_split(e, len(e)/2)
    E, E_lookup = [], {}
    for i, e in enumerate(edges):
        E.append(tuple(e))
        E_lookup[tuple(e)] = i

    # set up neighborhood data
    max_degree = max(G_undir.degree, key=lambda x: x[1])[1]
    nbrhoods_dict = {node: onp.array(list(map(int, G_undir[node]))) for node in
                     map(int, sorted(G_undir.nodes))}
    n_nbrs = onp.array([len(nbrhoods_dict[n]) for n in last_nodes])

    # Bconds function
    nbrhoods = np.array([list(sorted(G_undir[n])) + [-1] * (max_degree - len(G_undir[n])) for n in range(max(G_undir.nodes) + 1)])
    nbrhoods = nbrhoods

    B1_jax = np.append(B1, np.zeros((1, B1.shape[1])), axis=0)

    def Bconds_func(n):
        Nv = nbrhoods[n]
        return B1_jax[Nv]

    for i in range(len(inputs_all)):
        inputs_all[i][0] = Bconds_func

    # assert np.equal(Bcondss[0][0], Bconds_func(last_nodes[0])).all()

    return inputs_all, y_all, train_mask, test_mask, shifts, G_undir, E_lookup, nbrhoods, n_nbrs, target_nodes_all

def train_model():
    """
    Trains a model to predict the next node in each input path (represented as a flow)
    """
    # Hyperparameters (from args)
    epochs, learning_rate, batch_size, hidden_layers, reverse, describe, load_data, load_model = hyperparams()


    inputs_all, y_all, train_mask, test_mask, shifts, G_undir, E_lookup, nbrhoods, n_nbrs, target_nodes_all = data_setup(hops=(1,2), load=load_data)
    (inputs_1hop, inputs_2hop), (y_1hop, y_2hop) = inputs_all, y_all

    last_nodes = inputs_1hop[1]



    if describe == 1:
        desc = input("Describe this test: ")

    in_axes = tuple([None, None, None, None, 0, 0])



    # Create model
    hodge = Hodge_GCN(epochs, learning_rate, batch_size)
    hodge.setup(hodge_parallel_variable, hidden_layers, shifts, inputs_1hop, y_1hop, in_axes, train_mask)

    if load_model:
        hodge.weights = onp.load('models/model.npy', allow_pickle=True)
        (train_loss, train_acc), (test_loss, test_acc) = hodge.test(inputs_1hop, y_1hop, train_mask, n_nbrs), \
                                                         hodge.test(inputs_1hop, y_1hop, test_mask, n_nbrs)
    else:
        # Train
        train_loss, train_acc, test_loss, test_acc = hodge.train(inputs_1hop, y_1hop, train_mask, test_mask, n_nbrs)

        try:
            os.mkdir('models')
        except:
            pass
        onp.save('models/model', hodge.weights)

    print(hodge.multi_hop_accuracy_dist(shifts, inputs_1hop, target_nodes_all[1], [train_mask, test_mask], nbrhoods, E_lookup, last_nodes, n_nbrs, 2))

    raise Exception
    train_2hop, test_2hop = hodge.multi_hop_accuracy_binary(shifts, inputs_2hop, y_2hop, train_mask, nbrhoods, E_lookup, last_nodes, n_nbrs, 2), \
                            hodge.multi_hop_accuracy_binary(shifts, inputs_2hop, y_2hop, test_mask, nbrhoods, E_lookup,
                                                            last_nodes, n_nbrs, 2)
    print(train_2hop, test_2hop)
    train_2target, test_2target = hodge.two_target_accuracy(shifts, inputs_1hop, y_1hop, train_mask, n_nbrs), \
                                  hodge.two_target_accuracy(shifts, inputs_1hop, y_1hop, test_mask, n_nbrs)

    print(train_2target, test_2target)

    if reverse:
        rev_flows_in, rev_targets_1hop, rev_targets_2hop, rev_last_nodes = \
            onp.load('trajectory_data_1hop/rev_flows_in.npy'), onp.load('trajectory_data_1hop/rev_targets.npy'), \
            onp.load('trajectory_data_2hop/rev_targets.npy'), onp.load('trajectory_data_1hop/rev_last_nodes.npy')
        rev_n_nbrs = [len(neighborhood(G_undir, n)) for n in rev_last_nodes]
        hodge.test([inputs_1hop[0], rev_last_nodes, rev_flows_in], rev_targets_1hop, test_mask, rev_n_nbrs)



    if describe == 1:
        print(desc)

if __name__ == '__main__':
    train_model()