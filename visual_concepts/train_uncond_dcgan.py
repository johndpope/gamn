import sys
sys.path.append('..')

import os
import json
from time import time
import numpy as np
from tqdm import tqdm
from matplotlib import pyplot as plt
from sklearn.externals import joblib

import theano
import theano.sandbox.cuda
import theano.tensor as T

from lib import activations
from lib import updates
from lib import inits
from lib.vis import color_grid_vis
from lib.rng import py_rng, np_rng
from lib.ops import batchnorm, conv_cond_concat, deconv, dropout, l2normalize
from lib.metrics import nnc_score, nnd_score
from lib.theano_utils import floatX, sharedX
from lib.data_utils import OneHot, shuffle, iter_data, center_crop, patch
from lib.config import data_dir
import lib.utils as utils
from lib import models

from lib.img_utils import inverse_transform, transform

from load import visual_concepts

k = 1             # # of discrim updates for each gen update
l2 = 1e-5         # l2 weight decay
nvis = 196        # # of samples to visualize during training
b1 = 0.5          # momentum term of adam
nc = 3            # # of channels in image
nbatch = 128      # # of examples in batch
npx = 64          # # of pixels width/height of images
# nz is set later by looking at the feature vector length
# nz = 100          # # of dim for Z
ngf = 128         # # of gen filters in first conv layer
ndf = 128         # # of discrim filters in first conv layer
nx = npx*npx*nc   # # of dimensions in X
niter = 50        # # of iter at starting learning rate
niter_decay = 0   # # of iter to linearly decay learning rate to zero
lr = 0.0002       # initial learning rate for adam
desc = 'vcgan_orig_multi2_recon'
path = os.path.join(data_dir, "vc.hdf5")  # Change path to visual concepts file
tr_data, tr_stream = visual_concepts(path, ntrain=None)

patches_idx = tr_stream.dataset.provides_sources.index('patches')
labels_idx = tr_stream.dataset.provides_sources.index('labels')
feat_l2_idx = tr_stream.dataset.provides_sources.index('feat_l2')
feat_orig_idx = tr_stream.dataset.provides_sources.index('feat_orig')

if "orig" in desc:
    zmb_idx = feat_orig_idx
else:
    zmb_idx = feat_l2_idx

tr_handle = tr_data.open()
data = tr_data.get_data(tr_handle, slice(0, 10000))
vaX = data[patches_idx]
vaX = transform(vaX, npx)

data = tr_data.get_data(tr_handle, slice(0, tr_data.num_examples))
nvc = np.max(data[labels_idx]) # Number of visual concepts
assert nvc == 176 # Debugging code. Remove it later
nz = data[feat_l2_idx].shape[1]  # Length of the population encoding vector
ntrain = tr_data.num_examples  # # of examples to train on

model_dir = 'models/%s'%desc
samples_dir = 'samples/%s'%desc
if not os.path.exists('logs/'):
    os.makedirs('logs/')
if not os.path.exists(model_dir):
    os.makedirs(model_dir)
if not os.path.exists(samples_dir):
    os.makedirs(samples_dir)

latest_epoch = utils.getLatestModelNum(model_dir)

if latest_epoch == -1:
    print "Initializing weights from scratch"
    gifn = inits.Normal(scale=0.02)
    difn = inits.Normal(scale=0.02)
    gain_ifn = inits.Normal(loc=1., scale=0.02)
    bias_ifn = inits.Constant(c=0.)

    gw  = gifn((nz, ngf*8*4*4), 'gw')
    gg = gain_ifn((ngf*8*4*4), 'gg')
    gb = bias_ifn((ngf*8*4*4), 'gb')
    gw2 = gifn((ngf*8, ngf*4, 5, 5), 'gw2')
    gg2 = gain_ifn((ngf*4), 'gg2')
    gb2 = bias_ifn((ngf*4), 'gb2')
    gw3 = gifn((ngf*4, ngf*2, 5, 5), 'gw3')
    gg3 = gain_ifn((ngf*2), 'gg3')
    gb3 = bias_ifn((ngf*2), 'gb3')
    gw4 = gifn((ngf*2, ngf, 5, 5), 'gw4')
    gg4 = gain_ifn((ngf), 'gg4')
    gb4 = bias_ifn((ngf), 'gb4')
    gwx = gifn((ngf, nc, 5, 5), 'gwx')

    dw  = difn((ndf, nc, 5, 5), 'dw')
    dw2 = difn((ndf*2, ndf, 5, 5), 'dw2')
    dg2 = gain_ifn((ndf*2), 'dg2')
    db2 = bias_ifn((ndf*2), 'db2')
    dw3 = difn((ndf*4, ndf*2, 5, 5), 'dw3')
    dg3 = gain_ifn((ndf*4), 'dg3')
    db3 = bias_ifn((ndf*4), 'db3')
    dw4 = difn((ndf*8, ndf*4, 5, 5), 'dw4')
    dg4 = gain_ifn((ndf*8), 'dg4')
    db4 = bias_ifn((ndf*8), 'db4')
    # dwy = difn((ndf*8*4*4, 1), 'dwy')
    dwmy = difn((ndf*8*4*4, nvc*2), 'dwmy')
    bm = bias_ifn((nvc*2), 'bm')

    gen_params = [gw, gg, gb, gw2, gg2, gb2, gw3, gg3, gb3, gw4, gg4, gb4, gwx]
    discrim_params = [dw, dw2, dg2, db2, dw3, dg3, db3, dw4, dg4, db4, dwmy, bm]
    iter_array = range(niter)
else:
    print "Initializing weights from %d epoch network" % (latest_epoch)
    gen_params = [sharedX(element) for element in joblib.load(model_dir + "/%s"%(str(latest_epoch)) + "_gen_params.jl")]
    discrim_params = [sharedX(element) for element in joblib.load(model_dir + "/%s"%(str(latest_epoch)) + "_discrim_params.jl")]
    iter_array = range(latest_epoch,niter)

X = T.tensor4()
Z = T.matrix()
Y = T.matrix()

gX = models.gen(Z, *gen_params)

p_real_multi = models.discrim(X, *discrim_params)
p_gen_multi = models.discrim(gX, *discrim_params)

bce = T.nnet.binary_crossentropy
cce = T.nnet.categorical_crossentropy

# d_cost_real = bce(p_real, T.ones(p_real.shape)).mean() # bce is defined in models.py
# d_cost_gen = bce(p_gen, T.zeros(p_gen.shape)).mean()

d_cost_multi_real = cce(p_real_multi, T.concatenate([Y, T.zeros((p_real_multi.shape[0], nvc))], axis=1)).mean()
d_cost_multi_gen = cce(p_gen_multi, T.concatenate([T.zeros((p_gen_multi.shape[0], nvc)), Y], axis=1)).mean()

# g_cost_d = bce(p_gen, T.ones(p_gen.shape)).mean()
g_cost_multi_d = cce(p_gen_multi, T.concatenate([Y, T.zeros((p_real_multi.shape[0], nvc))], axis=1)).mean()
g_cost_recon = T.mean(T.sqr(gX - X))

d_cost = d_cost_multi_real + d_cost_multi_gen
g_cost = g_cost_multi_d + g_cost_recon

cost = [g_cost, d_cost]

lrt = sharedX(lr)
d_updater = updates.Adam(lr=lrt, b1=b1, regularizer=updates.Regularizer(l2=l2))
g_updater = updates.Adam(lr=lrt, b1=b1, regularizer=updates.Regularizer(l2=l2))
d_updates = d_updater(discrim_params, d_cost)
g_updates = g_updater(gen_params, g_cost)
updates = d_updates + g_updates

print 'COMPILING'
t = time()
_train_g = theano.function([X, Y, Z], cost, updates=g_updates)
_train_d = theano.function([X, Y, Z], cost, updates=d_updates)
_gen = theano.function([Z], gX)
print '%.2f seconds to compile theano functions'%(time()-t)

vis_idxs = py_rng.sample(np.arange(len(vaX)), nvis)
vaX_vis = inverse_transform(vaX[vis_idxs], nc, npx)
color_grid_vis(vaX_vis, (14, 14), 'samples/%s_etl_test.png'%desc)

# sample_zmb = floatX(np_rng.uniform(-1., 1., size=(nvis, nz)))
sample_zmb = floatX(data[zmb_idx][vis_idxs,:])

def gen_samples(n, nbatch=128):
    samples = []
    n_gen = 0
    for i in range(n/nbatch):
        zmb = floatX(np_rng.uniform(-1., 1., size=(nbatch, nz)))
        xmb = _gen(zmb)
        samples.append(xmb)
        n_gen += len(xmb)
    n_left = n-n_gen
    zmb = floatX(np_rng.uniform(-1., 1., size=(n_left, nz)))
    xmb = _gen(zmb)
    samples.append(xmb)
    return np.concatenate(samples, axis=0)

f_log = open('logs/%s.ndjson'%desc, 'wb')
log_fields = [
    'n_epochs',
    'n_updates',
    'n_examples',
    'n_seconds',
    '1k_va_nnd',
    '10k_va_nnd',
    '100k_va_nnd',
    'g_cost',
    'd_cost',
]

vaX = vaX.reshape(len(vaX), -1)

print desc.upper()
n_updates = 0
n_check = 0
n_epochs = iter_array[0]
n_updates = 0
n_examples = 0
t = time()
for epoch in iter_array:
    for data in tqdm(tr_stream.get_epoch_iterator(), total=ntrain/nbatch):

        imb = data[patches_idx]
        imb = transform(imb, npx)

        labels = data[labels_idx]
        label_stack = np.array([], dtype=np.uint8).reshape(0,nvc)
        for label in labels:
            hot_vec = np.zeros((1,nvc), dtype=np.uint8)
            hot_vec[0,label-1] = 1 # labels are 1-nvc
            label_stack = np.vstack((label_stack, hot_vec))

        ymb = label_stack
        # "Noise" is no longer random noise. We replace it with the population encoding vector
        # zmb = floatX(np_rng.uniform(-1., 1., size=(len(imb), nz)))
        zmb = floatX(data[zmb_idx])

        if n_updates % (k+1) == 0:
            cost = _train_g(imb, ymb, zmb)
        else:
            cost = _train_d(imb, ymb, zmb)
        n_updates += 1
        n_examples += len(imb)
    g_cost = float(cost[0])
    d_cost = float(cost[1])
    # gX = gen_samples(100000)
    # gX = gX.reshape(len(gX), -1)
    # va_nnd_1k = nnd_score(gX[:1000], vaX, metric='euclidean')
    # va_nnd_10k = nnd_score(gX[:10000], vaX, metric='euclidean')
    # va_nnd_100k = nnd_score(gX[:100000], vaX, metric='euclidean')
    # log = [n_epochs, n_updates, n_examples, time()-t, va_nnd_1k, va_nnd_10k, va_nnd_100k, g_cost, d_cost]
    log = [n_epochs, n_updates, n_examples, time() - t, g_cost, d_cost]
    # print '%.0f %.2f %.2f %.2f %.4f %.4f'%(epoch, va_nnd_1k, va_nnd_10k, va_nnd_100k, g_cost, d_cost)
    print '%.0f %.4f %.4f' % (epoch, g_cost, d_cost)
    f_log.write(json.dumps(dict(zip(log_fields, log)))+'\n')
    f_log.flush()

    samples = np.asarray(_gen(sample_zmb))
    color_grid_vis(inverse_transform(samples, nc, npx), (14, 14), 'samples/%s/%d.png'%(desc, n_epochs))
    n_epochs += 1
    if n_epochs > niter:
        lrt.set_value(floatX(lrt.get_value() - lr/niter_decay))
    if n_epochs % 5 == 0:
        joblib.dump([p.get_value() for p in gen_params], 'models/%s/%d_gen_params.jl'%(desc, n_epochs))
        joblib.dump([p.get_value() for p in discrim_params], 'models/%s/%d_discrim_params.jl'%(desc, n_epochs))
