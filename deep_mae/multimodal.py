import os
import keras
import pandas as pd
import numpy as np
from biom import load_table, Table
from biom.util import biom_open
from skbio.stats.composition import clr, centralize, closure
from skbio.stats.composition import clr_inv as softmax
import matplotlib.pyplot as plt
from scipy.stats import entropy, spearmanr
from keras.layers import Input, Embedding, Dense
from keras.models import Model
from keras.layers import concatenate
from keras import regularizers
import click


@click.group()
def multimodal():
    pass

@multimodal.command()
@click.option('--otu-table-file', help='Input otu biom table')
@click.option('--metabolite-table-file', help='Input metabolite biom table')
@click.option('--num_test', default=10,
              help='Number of testing samples')
@click.option('--min_samples',
              help=('Minimum number of samples a feature needs to be '
                    'observed in before getting filtered out'),
              default=10)
@click.option('--output_dir', help='output directory')
def split(otu_table_file, metabolite_table_file, num_test,
          min_samples, output_dir):
    microbes = load_table(otu_table_file)
    metabolites = load_table(metabolite_table_file)

    microbes_df = pd.DataFrame(
        np.array(microbes.matrix_data.todense()).T,
        index=microbes.ids(axis='sample'),
        columns=microbes.ids(axis='observation'))

    metabolites_df = pd.DataFrame(
        np.array(metabolites.matrix_data.todense()).T,
        index=metabolites.ids(axis='sample'),
        columns=metabolites.ids(axis='observation'))

    microbes_df, metabolites_df = microbes_df.align(
        metabolites_df, axis=0, join='inner')


    # filter out microbes that don't appear in many samples
    microbes_df = microbes_df.loc[:, (microbes_df>0).sum(axis=0)>min_samples]

    sample_ids = set(np.random.choice(microbes_df.index, size=num_test))
    sample_ids = np.array([x in sample_ids for x in microbes_df.index])
    train_microbes = microbes_df.loc[~sample_ids]
    test_microbes = microbes_df.loc[sample_ids]
    train_metabolites = metabolites_df.loc[~sample_ids]
    test_metabolites = metabolites_df.loc[sample_ids]

    if not os.path.exists(output_dir):
        os.mkdir(output_dir)

    train_microbes = Table(train_microbes.values.T,
                           train_microbes.columns, train_microbes.index)
    test_microbes = Table(test_microbes.values.T,
                          test_microbes.columns, test_microbes.index)
    train_metabolites = Table(train_metabolites.values.T,
                           train_metabolites.columns, train_metabolites.index)
    test_metabolites = Table(test_metabolites.values.T,
                          test_metabolites.columns, test_metabolites.index)

    # output paths
    test_microbes_path = os.path.join(
        output_dir, 'test_' + os.path.basename(otu_table_file))
    train_microbes_path = os.path.join(
        output_dir, 'train_' + os.path.basename(otu_table_file))
    test_metabolites_path = os.path.join(
        output_dir, 'test_' + os.path.basename(metabolite_table_file))
    train_metabolites_path = os.path.join(
        output_dir, 'train_' + os.path.basename(metabolite_table_file))

    with biom_open(train_microbes_path, 'w') as f:
        train_microbes.to_hdf5(f, "train")
    with biom_open(test_microbes_path, 'w') as f:
        test_microbes.to_hdf5(f, "test")
    with biom_open(train_metabolites_path, 'w') as f:
        train_metabolites.to_hdf5(f, "train")
    with biom_open(test_metabolites_path, 'w') as f:
        test_metabolites.to_hdf5(f, "test")


def build_model(microbes, metabolites,
                microbe_latent_dim=5, metabolite_latent_dim=5,
                lam=0.1):

    d1 = microbes.shape[1]
    d2 = metabolites.shape[1]

    ms_input = Input(shape=(d2,), dtype='float32', name='ms_input')

    # reduce the dimensionality
    ms_in = Dense(metabolite_latent_dim, activation='linear',
                  bias_regularizer=regularizers.l1(lam),
                  activity_regularizer=regularizers.l1(lam))(ms_input)

    shared = Dense(metabolite_latent_dim + microbe_latent_dim,
                   activation='linear',
                   bias_regularizer=regularizers.l2(lam),
                   activity_regularizer=regularizers.l2(lam))(ms_in)

    otu_out = Dense(microbe_latent_dim, activation='linear',
                    activity_regularizer=regularizers.l1(lam))(shared)

    otu_output = Dense(d1, activation='softmax',
                       bias_regularizer=regularizers.l1(lam),
                       activity_regularizer=regularizers.l1(lam),
                       name='otu_output')(otu_out)

    model = Model(inputs=[ms_input], outputs=[otu_output])

    model.compile(optimizer='adam',
                  loss={
                      'otu_output': 'kullback_leibler_divergence'
                  },
                  loss_weights={
                      'otu_output': 1.0,
                  }
    )
    return model


@multimodal.command()
@click.option('--otu-train-file',
              help='Input microbial abundances for training')
@click.option('--otu-test-file',
              help='Input microbial abundances for testing')
@click.option('--metabolite-train-file',
              help='Input metabolite abundances for training')
@click.option('--metabolite-test-file',
              help='Input metabolite abundances for testing')
@click.option('--epochs',
              help='Number of epochs to train', default=100)
@click.option('--batch_size',
              help='Size of mini-batch', default=32)
@click.option('--cv_iterations',
              help='Number of cross validation iterations', default=32)
@click.option('--microbe_latent_dim',
              help=('Dimensionality of microbial latent space. '
                    'This is analogous to the number of latent dimensions.'),
              default=3)
@click.option('--metabolite_latent_dim',
              help=('Dimensionality of metabolite latent space. '
                    'This is analogous to the number of PC axes.'),
              default=3)
@click.option('--regularization',
              help=('Parameter regularization.  Helps with preventing overfitting.'
                    'Higher regularization forces more parameters to zero.'),
              default=10.)
@click.option('--top-k',
              help=('Number of top hits to compare for cross-validation.'),
              default=10)
@click.option('--summary-dir',
              help='Summary directory')
@click.option('--results-file',
              help='Results file containing cross validation results.')
@click.option('--ranks-file',
              help='Ranks file containing microbe-metabolite rankings')
def autoencoder(otu_train_file, otu_test_file,
                metabolite_train_file, metabolite_test_file,
                epochs, batch_size, cv_iterations,
                microbe_latent_dim, metabolite_latent_dim,
                regularization, top_k,
                summary_dir, results_file, ranks_file):

    lam = regularization

    train_microbes = load_table(otu_train_file)
    test_microbes = load_table(otu_test_file)
    train_metabolites = load_table(metabolite_train_file)
    test_metabolites = load_table(metabolite_test_file)

    microbes_df = pd.DataFrame(
        np.array(train_microbes.matrix_data.todense()).T,
        index=train_microbes.ids(axis='sample'),
        columns=train_microbes.ids(axis='observation'))

    metabolites_df = pd.DataFrame(
        np.array(train_metabolites.matrix_data.todense()).T,
        index=train_metabolites.ids(axis='sample'),
        columns=train_metabolites.ids(axis='observation').astype(np.int))

    # filter out low abundance microbes
    microbe_ids = microbes_df.columns
    metabolite_ids = metabolites_df.columns

    # normalize the microbe and metabolite counts to sum to 1
    microbes = closure(microbes_df)
    metabolites = closure(metabolites_df)
    params = []

    model = build_model(microbes, metabolites,
                        microbe_latent_dim=microbe_latent_dim,
                        metabolite_latent_dim=metabolite_latent_dim,
                        lam=lam)

    sname = 'microbe_latent_dim_' + str(microbe_latent_dim) + \
           '_metabolite_latent_dim_' + str(metabolite_latent_dim) + \
           '_lam' + str(lam)

    # tbCallBack = keras.callbacks.TensorBoard(
    #     log_dir=os.path.join(summary_dir + '/run_' + sname),
    #     histogram_freq=0,
    #     write_graph=True,
    #     write_images=True)

    model.fit(
        {
            'ms_input': metabolites
        },
        {
            'otu_output': microbes,
        },
        #verbose=0,
        #callbacks=[tbCallBack],
        epochs=epochs, batch_size=batch_size)

    microbes_df = pd.DataFrame(
        np.array(test_microbes.matrix_data.todense()).T,
        index=test_microbes.ids(axis='sample'),
        columns=test_microbes.ids(axis='observation'))

    metabolites_df = pd.DataFrame(
        np.array(test_metabolites.matrix_data.todense()).T,
        index=test_metabolites.ids(axis='sample'),
        columns=test_metabolites.ids(axis='observation').astype(np.int))

    microbes = closure(microbes_df)
    metabolites = closure(metabolites_df)

    # otu_output, ms_output = model.predict(
    #     [microbes, metabolites], batch_size=microbes.shape[0])
    weights = model.get_weights()
    V1 = weights[0]   # ms input weights
    V1b = weights[1]  # ms input bias
    V2 = weights[2]   # ms shared weights
    V2b = weights[3]  # ms shared bias
    U2 = weights[4]   # otu shared weights
    U2b = weights[5]  # otu shared bias
    U3 = weights[6]  # otu output weights
    U3b = weights[7] # otu output bias

    # all of the weights to predict metabolites from microbes
    def predict(x):
        return softmax(
            # x @ U3.T @ U2.T @ V2.T @ V1.T
            (((x @ U3.T + U2b) @ U2.T + V2b) @ V2.T + V1b) @ V1.T
        )

    ranks = ((((U3.T + U2b) @ U2.T + V2b) @ V2.T + V1b) @ V1.T)
    ranks = pd.DataFrame(ranks, index=microbes_df.columns,
                         columns=metabolites_df.columns)

    pred_ms = predict(microbes)

    mean_ms_diff = np.mean(
        np.sum(np.abs(pred_ms - metabolites), axis=1) * 0.5)
    k = top_k
    ms_r = []
    for i in range(metabolites.shape[0]):
        idx = np.argsort(pred_ms[i, :])[-k:]
        r = spearmanr(pred_ms[i, idx], metabolites[i, idx])
        ms_r.append(r)
    ms_r = np.mean(ms_r)
    ms_kl = np.mean(entropy(metabolites, pred_ms))
    p = {'ms_err' : mean_ms_diff,
         'ms_kl' : ms_kl,
         'ms_spearman': ms_r,
         'microbe_latent_dim': microbe_latent_dim,
         'metabolite_latent_dim': metabolite_latent_dim,
         'regularization': lam}

    params.append(p)
    params = pd.DataFrame(params)
    params.to_csv(results_file)
    ranks.to_csv(ranks_file)


@multimodal.command()
@click.option('--ranks-file',
              help='Ranks file containing microbe-metabolite rankings')
@click.option('--k-nearest-neighbors',
              help=('Number of nearest neighbors.'),
              default=3)
@click.option('--node-metadata',
              help='Node metadata for cytoscape.')
@click.option('--edge-metadata',
              help='Edge metadata for cytoscape.')
def network(ranks_file, k_nearest_neighbors, node_metadata, edge_metadata):
    ranks = pd.read_csv(ranks_file, index_col=0).T
    probs = ranks.apply(softmax, axis=1)
    top_hits = pd.DataFrame(
        {'ms_id': ranks.apply(np.argmin, axis=1),
         'rank': ranks.apply(np.min, axis=1)},
        index=ranks.index)
    k = k_nearest_neighbors
    otus = {x : i for i, x in enumerate(ranks.columns)}

    topk = ranks.apply(lambda x: [
        otus[ranks.columns[k]] for k in np.argsort(x)[-k:]],
                       axis=1).values
    topk = pd.DataFrame([x for x in topk], index=ranks.index)
    top_hits = pd.merge(
        top_hits, topk, left_index=True, right_index=True)
    top_hits = top_hits.reset_index()
    edges = pd.melt(
        top_hits, id_vars=['index'],
        value_vars=list(range(k)),
        value_name='otu_id')
    edges = edges.rename(columns={'index': 'ms_id'})
    edges = edges.rename(columns={'index': 'ms_id'})
    edges['edge_type'] = 'co_occur'
    # edges['ms_id'] = ['metabolite_%s' % x for x in edges.ms_id]
    # edges['otu_id'] = ['otu_%s' % x for x in edges.otu_id]
    edges = edges.set_index(['ms_id'])
    edges[['edge_type', 'otu_id']].to_csv(
        edge_metadata, sep='\t', header=False)

    otu_ids = set(edges.otu_id.values)
    ms_ids = set(edges.index)

    nodes = pd.DataFrame(columns=['id', 'node_type'])
    nodes['id'] = list(ms_ids) + list(otu_ids)
    nodes['node_type'] = ['metabolite'] * len(ms_ids) + ['OTU'] * len(otu_ids)
    nodes = nodes.set_index('id')
    nodes.to_csv(node_metadata, sep='\t')


if __name__ == '__main__':
    multimodal()