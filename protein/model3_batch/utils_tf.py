import os
import numpy as np
import matplotlib.pyplot as plt
import tensorflow as tf
from keras.layers import *
import keras
import pandas as pd
import sklearn
import os
from model3_batch.unet import *

def save_submission(dm, phi, psi, save_path):
    # load protein names
    testfile = os.path.join(save_path, 'test.csv')
    test_input = pd.read_csv(testfile, header=None)
    protein_names = np.array(test_input.iloc[:, 1])
    protein_len = np.array(test_input.iloc[:, 2])

    # concatenate all output to one-dimensional
    all_data = []
    all_names = []
    for i, pname in enumerate(protein_names):
        dist_flat = dm[i].ravel()
        array = np.concatenate([dist_flat, psi[i], phi[i]])
        all_data.append(array)

        length = protein_len[i]
        dist_names = ["{}_d_{}_{}".format(pname, i + 1, j + 1) for i in range(length) for
                      j in range(length)]

        psi_names = ["{}_psi_{}".format(pname, i + 1) for i in range(length)]
        phi_names = ["{}_phi_{}".format(pname, i + 1) for i in range(length)]
        row_names = np.array(dist_names + psi_names + phi_names)
        all_names.append(row_names)

    all_data = np.concatenate(all_data)
    all_names = np.concatenate(all_names)
    output = {"Id": all_names, "Predicted": all_data}
    output = pd.DataFrame(output)
    output.to_csv(os.path.join(save_path, "submission.csv"), index=False)


from model3_batch.geom_ops import *
class CkptSaver:
    def __init__(self, work_dir, sess):
        self.sess = sess
        self.saver = tf.train.Saver()
        self.work_dir = work_dir
        self.best_eval = None


    def update(self, epoch, eval_loss):
        if self.best_eval is None or self.best_eval >= eval_loss:
            self.best_eval = eval_loss
            self.save(epoch)

    def save(self, epoch):
        self.saver.save(sess=self.sess,
                        save_path=os.path.join(self.work_dir, 'model.ckpt'), global_step=epoch)

    def restore(self, ckpt_fpath):
        self.saver.restore(sess=self.sess, save_path=ckpt_fpath)

class ModelSaver:
    def __init__(self, work_dir, model):
        self.model = model
        self.saver = tf.train.Saver()
        self.work_dir = work_dir
        self.best_eval = None


    def update(self, epoch, eval_loss):
        if self.best_eval is None or self.best_eval >= eval_loss:
            self.best_eval = eval_loss
            self.save(epoch)

    def save(self, epoch):
        self.model.save(os.path.join(self.work_dir, 'model-{}.h5'.format(epoch)))

def get_distance_matrix(torsion_angles):
    """ Convert torsion angles to distance matrix
    using differentiable geometric transformation. """
    coordinates = torsion_angles_to_coordinates(torsion_angles)
    dist = coordinates_to_dist_matrix(coordinates)
    return dist, coordinates

# some functions below are adapted from https://github.com/aqlaboratory/rgn
class DistanceMatrix(keras.layers.Layer):
    """ Convert torsion angles to distance matrix 
    using differentiable geometric transformation. """
    def __init__(self):
        super(DistanceMatrix, self).__init__()

    def call(self, torsion_angles):
        coordinates = torsion_angles_to_coordinates(torsion_angles)
        dist = coordinates_to_dist_matrix(coordinates)
        return dist, coordinates

class TorsionAngles(keras.layers.Layer):
    """ computes torsion angles using softmax probabilities 
    and a learned alphabet of angles. (as an alternative to directly predictin angles) """
    def __init__(self, alphabet_size=50):
        super(TorsionAngles, self).__init__()
        self.alphabet = create_alphabet_mixtures(alphabet_size=alphabet_size)
    
    def call(self, probs): 
        torsion_angles = alphabet_mixtures_to_torsion_angles(probs, self.alphabet)
        return torsion_angles

def create_alphabet_mixtures(alphabet_size=50):
    """ Creates alphabet for alphabetized dihedral prediction. """
    init_range = np.pi 
    alphabet_initializer = tf.keras.initializers.RandomUniform(-init_range, init_range)
    alphabet_init = alphabet_initializer(shape=[alphabet_size, NUM_DIHEDRALS], dtype=tf.float32)
    alphabet = tf.Variable(name='alphabet', initial_value=alphabet_init, trainable=True)
    return alphabet  # [alphabet_size, NUM_DIHEDRALS]

def alphabet_mixtures_to_torsion_angles(probs, alphabet):
    """ Converts softmax probabilties + learned mixture components (alphabets) 
        into dihedral angles. 
    """
    torsion_angles = reduce_mean_angle(probs, alphabet)
    return torsion_angles  # [BATCH_SIZE, MAX_LEN, NUM_DIHEDRALS]


def torsion_angles_to_coordinates(torsion_angles, c_alpha_only=True):
    """ Converts dihedrals into full 3D structures. """
    original_shape = torsion_angles.shape
    torsion_angles = tf.transpose(torsion_angles, [1,0,2])
    # converts dihedrals to points ready for reconstruction.

    # torsion_angles: [MAX_LEN=768, BATCH_SIZE=32, NUM_DIHEDRALS=3]
    points = dihedral_to_point(torsion_angles) 
    # points: [MAX_LEN x NUM_DIHEDRALS, BATCH_SIZE, NUM_DIMENSIONS]
             
    # converts points to final 3D coordinates.
    coordinates = point_to_coordinate(points, num_fragments=6, parallel_iterations=4) 
    # [MAX_LEN x NUM_DIHEDRALS, BATCH_SIZE, NUM_DIMENSIONS]
    if c_alpha_only:
        coordinates = coordinates[1::NUM_DIHEDRALS]  # calpha starts from 1
        # [MAX_LEN x 1, BATCH_SIZE, NUM_DIMENSIONS]
    coordinates = tf.transpose(coordinates, [1,0,2])  # do not use reshape
    return coordinates

def coordinates_to_dist_matrix(u, name=None):
    """ Computes the pairwise distance (l2 norm) between all vectors in the tensor.
        Vectors are assumed to be in the third dimension. Op is done element-wise over batch.
    Args:
        u: [MAX_LEN, BATCH_SIZE, NUM_DIMENSIONS]
    Returns:
           [BATCH_SIZE, MAX_LEN, MAX_LEN]
    """
    with tf.name_scope(name, 'pairwise_distance', [u]) as scope:
        u = tf.convert_to_tensor(u, name='u')
        u = tf.transpose(u, [1,0,2])
        
        diffs = u - tf.expand_dims(u, 1)                                 # [MAX_LEN, MAX_LEN, BATCH_SIZE, NUM_DIMENSIONS]
        norms = reduce_l2_norm(diffs, reduction_indices=[3], name=scope) # [MAX_LEN, MAX_LEN, BATCH_SIZE]
        norms = tf.transpose(norms, [2,0,1])
        return norms

def drmsd_dist_matrix(mat1, mat2, batch_seqlen, weights, name=None):
    """
    mat1, mat2: [BATCH_SIZE, MAX_LEN, MAX_LEN]
    batch_seqlen: [BATCH_SIZE,]
    """
    with tf.name_scope(name, 'dRMSD', [mat1, mat2, weights]) as scope:
        mat1 = tf.convert_to_tensor(mat1, name='mat1')
        mat2 = tf.convert_to_tensor(mat2, name='mat2')
        weights = tf.convert_to_tensor(weights, name='weights')
        diffs = mat1 - mat2                      # [BATCH_SIZE, MAX_LEN, MAX_LEN]
        #diffs = tf.transpose(diffs, [1,2,0])      # [MAX_LEN, MAX_LEN, BATCH_SIZE]
        #weights = tf.transpose(weights, [1,2,0])

        norms = reduce_l2_norm(diffs, reduction_indices=[1, 2], weights=weights, name=scope) # [BATCH_SIZE]
        drmsd = norms / batch_seqlen
        return drmsd  # [BATCH_SIZE,]

def mse_dist_matrix(y_true, y_pred, batch_seqlen):
    mse_error = 0.0
    for yt, yp, sl in zip(y_true, y_pred, batch_seqlen):
        mse_error += sklearn.metrics.mean_squared_error(yt[:sl, :sl], yp[:sl, :sl])
    return mse_error / y_true.shape[0]

def mse_torsion_angle(y_true, y_pred, batch_seqlen):
    mse_error = 0.0
    for yt, yp, sl in zip(y_true, y_pred, batch_seqlen):
        mse_error += sklearn.metrics.mean_squared_error(yt[:sl], yp[:sl])
    return mse_error / y_true.shape[0]

def rmsd_torsion_angle(angles1, angles2, batch_seqlen, weights, name=None):
    """
    angles1, angles2: [BATCH_SIZE, MAX_LEN]
    batch_seqlen: [BATCH_SIZE,]
    """
    with tf.name_scope(name, 'RMSD_torsion', [angles1, angles2, weights]) as scope:
        angles1 = tf.convert_to_tensor(angles1, name='angles1')
        angles2 = tf.convert_to_tensor(angles2, name='angles2')
        weights = tf.convert_to_tensor(weights, name='weights')
        diffs = angles1 - angles2                      # [BATCH_SIZE, MAX_LEN]

        norms = reduce_l2_norm(diffs, reduction_indices=[1], weights=weights, name=scope) # [BATCH_SIZE]
        drmsd = norms / tf.sqrt(batch_seqlen)
        return drmsd  # [BATCH_SIZE,]

def plot_train_val(train, val, title=None, savepath=None):
    fig, ax = plt.subplots(1, 1, figsize=(6,4))
    ax.plot(train, c='g', label='train')
    ax.plot(val, c='b', label='val')
    ax.legend()
    if not title is None:
        ax.set_title(title)
    fig.savefig(savepath)
    plt.close()

def plot_dist_matrix(pred, gt, protein_names, lengths, scores, savepath=None):
    assert(pred.shape[0] == gt.shape[0])
    fig, axes = plt.subplots(2, pred.shape[0], figsize=(4 * pred.shape[0],8))
    for i, pname in enumerate(protein_names):
        axes[0, i].imshow(pred[i, :lengths[i], :lengths[i]])
        axes[0, i].set_title(pname + " prediction ({:.4g})".format(scores[i]))
    for i, pname in enumerate(protein_names):
        axes[1, i].imshow(gt[i, :lengths[i], :lengths[i]])
        axes[1, i].set_title(pname + " ground truth")
    plt.savefig(savepath)
    plt.close()

def rmsd_kaggle(rmsd_batch, seqlen_batch):
    """ rmsd across the entire batch """
    norm = tf.reduce_sum(tf.multiply(tf.square(rmsd_batch), seqlen_batch))
    rmsd = tf.sqrt(norm / tf.reduce_sum(seqlen_batch))
    return rmsd 

