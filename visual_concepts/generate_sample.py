import sys
sys.path.append('..')

import numpy as np
from lib import models
from lib.theano_utils import floatX, sharedX
from lib.rng import py_rng, np_rng
from lib.vis import color_grid_vis
from lib.img_utils import inverse_transform, transform

from sklearn.externals import joblib

import theano
import theano.tensor as T

dcgan_root = "/mnt/disk1/vittal/dcgan_code/visual_concepts/"

desc = "vcgan_l2_multi"
model_dir = dcgan_root + '/models/%s/'%desc
model_number = "20_gen_params.jl"
gen_params_np = joblib.load(model_dir + model_number)
gen_params = [sharedX(element) for element in gen_params_np]
vc_num = 10
Z = T.matrix()
gX = models.gen(Z, *gen_params)

if 'vc_num' in locals():
    from load import visual_concepts
    from lib.config import data_dir
    import os
    path = os.path.join(data_dir, "vc.hdf5")
    tr_data, tr_stream = visual_concepts(path, ntrain=None)
    tr_handle = tr_data.open()
    labels_idx = tr_stream.dataset.provides_sources.index('labels')
    patches_idx = tr_stream.dataset.provides_sources.index('patches')
    data = tr_data.get_data(tr_handle, slice(0, tr_data.num_examples))
    labels = data[labels_idx]
    vc_idx = np.where(labels == vc_num)[0]
    np.random.shuffle(vc_idx)
    vc_idx = vc_idx[:196]

    feat_l2_idx = tr_stream.dataset.provides_sources.index('feat_l2')
    sample_zmb = data[feat_l2_idx][vc_idx,:]

    patches = data[patches_idx][vc_idx,:]
    patches = transform(patches, 64)
    print patches.shape
    color_grid_vis(inverse_transform(patches, nc=3, npx=64), (14, 14), './patches.png')
else:
    sample_zmb = floatX(np_rng.uniform(-1., 1., size=(196, 100)))

print 'COMPILING...'
_gen = theano.function([Z], gX)
print 'Done!'

samples = np.asarray(_gen(sample_zmb))
save_file = dcgan_root + 'samples/%s/vc_%s.png'%(desc, str(vc_num))
print samples.shape
color_grid_vis(inverse_transform(samples, nc=3, npx=64), (14, 14), save_file)
