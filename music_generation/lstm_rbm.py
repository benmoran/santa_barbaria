# Author: Kratarth Goel
# BITS Pilani (2014)
# LSTM-RBM for music generation

from __future__ import print_function
import glob
import os
import sys
import numpy
import pylab
import zipfile
from midi.utils import midiread, midiwrite
import theano
import theano.tensor as T
from theano.tensor.shared_randomstreams import RandomStreams

try:
    import urllib.request as urllib  # for backwards compatibility
except ImportError:
    import urllib2 as urllib


def download(url, server_fname, local_fname=None, progress_update_percentage=5):
    """
    An internet download utility modified from
    http://stackoverflow.com/questions/22676/
    how-do-i-download-a-file-over-http-using-python/22776#22776
    """
    u = urllib.urlopen(url)
    if local_fname is None:
        local_fname = server_fname
    full_path = local_fname
    meta = u.info()
    with open(full_path, 'wb') as f:
        try:
            file_size = int(meta.get("Content-Length"))
        except TypeError:
            print("WARNING: Cannot get file size, displaying bytes instead!")
            file_size = 100
        print("Downloading: %s Bytes: %s" % (server_fname, file_size))
        file_size_dl = 0
        block_sz = int(1E7)
        p = 0
        while True:
            buffer = u.read(block_sz)
            if not buffer:
                break
            file_size_dl += len(buffer)
            f.write(buffer)
            if (file_size_dl * 100. / file_size) > p:
                status = r"%10d  [%3.2f%%]" % (file_size_dl, file_size_dl *
                                               100. / file_size)
                print(status)
                p += progress_update_percentage

# Don't use a python long as this don't work on 32 bits computers.
numpy.random.seed(0xbeef)
rng = RandomStreams(seed=numpy.random.randint(1 << 30))
theano.config.warn.subtensor_merge_bug = False


def fast_dropout(rng, x):
    """ Multiply activations by N(1,1) """
    mask = rng.normal(size=x.shape, avg=1., dtype=theano.config.floatX)
    return x * mask


def build_rbm(v, W, bv, bh, k):
    '''Construct a k-step Gibbs chain starting at v for an RBM.

v : Theano vector or matrix
  If a matrix, multiple chains will be run in parallel (batch).
W : Theano matrix
  Weight matrix of the RBM.
bv : Theano vector
  Visible bias vector of the RBM.
bh : Theano vector
  Hidden bias vector of the RBM.
k : scalar or Theano scalar
  Length of the Gibbs chain.

Return a (v_sample, cost, monitor, updates) tuple:

v_sample : Theano vector or matrix with the same shape as `v`
  Corresponds to the generated sample(s).
cost : Theano scalar
  Expression whose gradient with respect to W, bv, bh is the CD-k approximation
  to the log-likelihood of `v` (training example) under the RBM.
  The cost is averaged in the batch case.
monitor: Theano scalar
  Pseudo log-likelihood (also averaged in the batch case).
updates: dictionary of Theano variable -> Theano variable
  The `updates` object returned by scan.'''

    def gibbs_step(v):
        mean_h = T.nnet.sigmoid(T.dot(fast_dropout(rng, v), W) + bh)
        h = rng.binomial(size=mean_h.shape, n=1, p=mean_h,
                         dtype=theano.config.floatX)
        mean_v = T.nnet.sigmoid(T.dot(fast_dropout(rng, h), W.T) + bv)
        v = rng.binomial(size=mean_v.shape, n=1, p=mean_v,
                         dtype=theano.config.floatX)
        return mean_v, v

    chain, updates = theano.scan(lambda v: gibbs_step(v)[1], outputs_info=[v],
                                 n_steps=k)
    v_sample = chain[-1]

    mean_v = gibbs_step(v_sample)[0]
    monitor = T.xlogx.xlogy0(v, mean_v) + T.xlogx.xlogy0(1 - v, 1 - mean_v)
    monitor = monitor.sum() / v.shape[0]

    def free_energy(v):
        return -(v * bv).sum() - T.log(1 + T.exp(T.dot(v, W) + bh)).sum()
    cost = (free_energy(v) - free_energy(v_sample)) / v.shape[0]

    return v_sample, cost, monitor, updates


def shared_normal(num_rows, num_cols, scale=1):
    '''Initialize a matrix shared variable with normally distributed
elements.'''
    return theano.shared(numpy.random.normal(
        scale=scale, size=(num_rows, num_cols)).astype(theano.config.floatX))


def shared_zeros(*shape):
    '''Initialize a vector shared variable with zero elements.'''
    return theano.shared(numpy.zeros(shape, dtype=theano.config.floatX))


def build_lstmrbm(n_visible, n_hidden, n_hidden_recurrent):
    '''Construct a symbolic RNN-RBM and initialize parameters.

n_visible : integer
  Number of visible units.
n_hidden : integer
  Number of hidden units of the conditional RBMs.
n_hidden_recurrent : integer
  Number of hidden units of the RNN.

Return a (v, v_sample, cost, monitor, params, updates_train, v_t,
          updates_generate) tuple:

v : Theano matrix
  Symbolic variable holding an input sequence (used during training)
v_sample : Theano matrix
  Symbolic variable holding the negative particles for CD log-likelihood
  gradient estimation (used during training)
cost : Theano scalar
  Expression whose gradient (considering v_sample constant) corresponds to the
  LL gradient of the RNN-RBM (used during training)
monitor : Theano scalar
  Frame-level pseudo-likelihood (useful for monitoring during training)
params : tuple of Theano shared variables
  The parameters of the model to be optimized during training.
updates_train : dictionary of Theano variable -> Theano variable
  Update object that should be passed to theano.function when compiling the
  training function.
v_t : Theano matrix
  Symbolic variable holding a generated sequence (used during sampling)
updates_generate : dictionary of Theano variable -> Theano variable
  Update object that should be passed to theano.function when compiling the
  generation function.'''

    W = shared_normal(n_visible, n_hidden, 0.01)
    bv = shared_zeros(n_visible)
    bh = shared_zeros(n_hidden)
    Wuh = shared_normal(n_hidden_recurrent, n_hidden, 0.0001)
    Wuv = shared_normal(n_hidden_recurrent, n_visible, 0.0001)
    Wvu = shared_normal(n_visible, n_hidden_recurrent, 0.0001)
    Wuu = shared_normal(n_hidden_recurrent, n_hidden_recurrent, 0.0001)
    bu = shared_zeros(n_hidden_recurrent)

    Wui = shared_normal(n_hidden_recurrent, n_hidden_recurrent, 0.0001)
    Wqi = shared_normal(n_hidden_recurrent, n_hidden_recurrent, 0.0001)
    Wci = shared_normal(n_hidden_recurrent, n_hidden_recurrent, 0.0001)
    bi = shared_zeros(n_hidden_recurrent)
    Wuf = shared_normal(n_hidden_recurrent, n_hidden_recurrent, 0.0001)
    Wqf = shared_normal(n_hidden_recurrent, n_hidden_recurrent, 0.0001)
    Wcf = shared_normal(n_hidden_recurrent, n_hidden_recurrent, 0.0001)
    bf = shared_zeros(n_hidden_recurrent)
    Wuc = shared_normal(n_hidden_recurrent, n_hidden_recurrent, 0.0001)
    Wqc = shared_normal(n_hidden_recurrent, n_hidden_recurrent, 0.0001)
    bc = shared_zeros(n_hidden_recurrent)
    Wuo = shared_normal(n_hidden_recurrent, n_hidden_recurrent, 0.0001)
    Wqo = shared_normal(n_hidden_recurrent, n_hidden_recurrent, 0.0001)
    Wco = shared_normal(n_hidden_recurrent, n_hidden_recurrent, 0.0001)
    Wqv = shared_normal(n_hidden_recurrent, n_visible, 0.0001)
    Wqh = shared_normal(n_hidden_recurrent, n_hidden, 0.0001)
    bo = shared_zeros(n_hidden_recurrent)

    params = W, bv, bh, Wuh, Wuv, Wvu, Wuu, bu, Wui, Wqi, Wci, bi,
    Wuf, Wqf, Wcf, bf, Wuc, Wqc, bc, Wuo, Wqo, Wco, bo, Wqv, Wqh
    # learned parameters as shared
    # variables

    v = T.matrix()  # a training sequence
    u0 = T.zeros((n_hidden_recurrent,))  # initial value for the RNN hidden
    q0 = T.zeros((n_hidden_recurrent,))
    c0 = T.zeros((n_hidden_recurrent,))

    # If `v_t` is given, deterministic recurrence to compute the variable
    # biases bv_t, bh_t at each time step. If `v_t` is None, same recurrence
    # but with a separate Gibbs chain at each time step to sample (generate)
    # from the RNN-RBM. The resulting sample v_t is returned in order to be
    # passed down to the sequence history.
    def recurrence(v_t, u_tm1, q_tm1, c_tm1):
        bv_t = bv + T.dot(u_tm1, Wuv) + T.dot(q_tm1, Wqv)
        bh_t = bh + T.dot(u_tm1, Wuh) + T.dot(q_tm1, Wqh)
        generate = v_t is None
        if generate:
            v_t, _, _, updates = build_rbm(T.zeros((n_visible,)), W, bv_t,
                                           bh_t, k=25)
        u_t = T.tanh(bu + T.dot(v_t, Wvu) + T.dot(u_tm1, Wuu))

        i_t = T.tanh(bi + T.dot(c_tm1, Wci) + T.dot(q_tm1, Wqi)
                     + T.dot(u_t, Wui))
        f_t = T.tanh(bf + T.dot(c_tm1, Wcf) + T.dot(q_tm1, Wqf)
                     + T.dot(u_t, Wuf))
        c_t = (f_t * c_tm1) + (i_t * T.tanh(T.dot(u_t, Wuc)
                                            + T.dot(q_tm1, Wqc) + bc))
        o_t = T.tanh(bo + T.dot(c_t, Wco) + T.dot(q_tm1, Wqo) + T.dot(u_t, Wuo))
        q_t = o_t * T.tanh(c_t)

        return ([v_t, u_t, q_t, c_t], updates) if generate else [
            u_t, q_t, c_t, bv_t, bh_t]

    # For training, the deterministic recurrence is used to compute all the
    # {bv_t, bh_t, 1 <= t <= T} given v. Conditional RBMs can then be trained
    # in batches using those parameters.

    (u_t, q_t, c_t, bv_t, bh_t), updates_train = theano.scan(
        lambda v_t, u_tm1, q_tm1, c_tm1, *_: recurrence(v_t, u_tm1, q_tm1, c_tm1),
        sequences=v, outputs_info=[u0, q0, c0, None, None], non_sequences=params)
    v_sample, cost, monitor, updates_rbm = build_rbm(v, W, bv_t[:], bh_t[:],
                                                     k=15)
    updates_train.update(updates_rbm)

    # symbolic loop for sequence generation
    (v_t, u_t, q_t, c_t), updates_generate = theano.scan(
        lambda u_tm1, q_tm1, c_tm1, *_: recurrence(None, u_tm1, q_tm1, c_tm1),
        outputs_info=[None, u0, q0, c0], non_sequences=params, n_steps=200)

    return (v, v_sample, cost, monitor, params, updates_train, v_t,
            updates_generate)


class LstmRbm:
    '''Simple class to train an RNN-RBM from MIDI files and to generate sample
sequences.'''

    def __init__(self, n_hidden=150, n_hidden_recurrent=100, lr=0.001,
                 r=(21, 109), dt=0.3):
        '''Constructs and compiles Theano functions for training and sequence
generation.

n_hidden : integer
  Number of hidden units of the conditional RBMs.
n_hidden_recurrent : integer
  Number of hidden units of the RNN.
lr : float
  Learning rate
r : (integer, integer) tuple
  Specifies the pitch range of the piano-roll in MIDI note numbers, including
  r[0] but not r[1], such that r[1]-r[0] is the number of visible units of the
  RBM at a given time step. The default (21, 109) corresponds to the full range
  of piano (88 notes).
dt : float
  Sampling period when converting the MIDI files into piano-rolls, or
  equivalently the time difference between consecutive time steps.'''

        self.r = r
        self.dt = dt
        (v, v_sample, cost, monitor, params, updates_train, v_t,
         updates_generate) = build_lstmrbm(r[1] - r[0], n_hidden,
                                           n_hidden_recurrent)

        gradient = T.grad(cost, params, consider_constant=[v_sample])
        updates_train.update(((p, p - lr * g) for p, g in zip(params,
                                                              gradient)))
        self.train_function = theano.function([v], monitor,
                                              updates=updates_train)
        self.generate_function = theano.function([], v_t,
                                                 updates=updates_generate)

    def train(self, files, batch_size=100, num_epochs=200):
        '''Train the RNN-RBM via stochastic gradient descent (SGD) using MIDI
files converted to piano-rolls.

files : list of strings
  List of MIDI files that will be loaded as piano-rolls for training.
batch_size : integer
  Training sequences will be split into subsequences of at most this size
  before applying the SGD updates.
num_epochs : integer
  Number of epochs (pass over the training set) performed. The user can
  safely interrupt training with Ctrl+C at any time.'''

        assert len(files) > 0, 'Training set is empty!' \
                               ' (did you download the data files?)'
        dataset = [midiread(f, self.r,
                            self.dt).piano_roll.astype(theano.config.floatX)
                   for f in files]
        print(len(dataset))
        print(len(dataset[0]))
        print((dataset[0]))
        try:
            for epoch in range(num_epochs):
                numpy.random.shuffle(dataset)
                costs = []

                for s, sequence in enumerate(dataset):
                    for i in range(0, len(sequence), batch_size):
                        cost = self.train_function(sequence[i:i + batch_size])
                        costs.append(cost)

                print('Epoch %i/%i' % (epoch + 1, num_epochs), end=' ')
                print(numpy.mean(costs))
                sys.stdout.flush()

        except KeyboardInterrupt:
            print('Interrupted by user.')

    def generate(self, filename, show=True):
        '''Generate a sample sequence, plot the resulting piano-roll and save
it as a MIDI file.

filename : string
  A MIDI file will be created at this location.
show : boolean
  If True, a piano-roll of the generated sequence will be shown.'''

        piano_roll = self.generate_function()
        midiwrite(filename, piano_roll, self.r, self.dt)
        if show:
            extent = (0, self.dt * len(piano_roll)) + self.r
            pylab.figure()
            pylab.imshow(piano_roll.T, origin='lower', aspect='auto',
                         interpolation='nearest', cmap=pylab.cm.gray_r,
                         extent=extent)
            pylab.xlabel('time (s)')
            pylab.ylabel('MIDI note number')
            pylab.title('generated piano-roll')


def check_fetch_Nottingham():
    download_fname = "Nottingham.zip"
    if not os.path.exists(download_fname):
        download("http://www.iro.umontreal.ca/~lisa/deep/data/Nottingham.zip",
                 "Nottingham.zip")
    data_path = os.path.join("Nottingham", "train")
    if not os.path.exists(data_path):
        zipfile.ZipFile(download_fname).extractall()
    return data_path


def test_lstmrbm(batch_size=100, num_epochs=200):
    data_path = check_fetch_Nottingham()
    model = LstmRbm()
    model.train(glob.glob(os.path.join(data_path, "*.mid")),
                batch_size=batch_size, num_epochs=num_epochs)
    return model

if __name__ == '__main__':
    model = test_lstmrbm()
    model.generate('sample1.mid')
    model.generate('sample2.mid')
    pylab.show()
