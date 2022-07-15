# -*- coding: utf-8 -*-

import numpy as np
# from high_dim_filter_factory import SpatialHighDimFilter, BilateralHighDimFilter
from permutohedral_tf import Permutohedral as PermutohedralTF
from PIL import Image
import matplotlib.pyplot as plt
import cv2
import tensorflow as tf

'''
CRF inference
`image` and `unary` are channel-last
'''

def unary_from_labels(labels: np.ndarray, n_labels: int, gt_prob: float, zero_unsure=True) -> np.ndarray:
    """
    Simple classifier that is 50% certain that the annotation is correct.
    (same as in the inference example).


    Parameters
    ----------
    labels: numpy.array
        The label-map, i.e. an array of your data's shape where each unique
        value corresponds to a label.
    n_labels: int
        The total number of labels there are.
        If `zero_unsure` is True (the default), this number should not include
        `0` in counting the labels, since `0` is not a label!
    gt_prob: float
        The certainty of the ground-truth (must be within (0,1)).
    zero_unsure: bool
        If `True`, treat the label value `0` as meaning "could be anything",
        i.e. entries with this value will get uniform unary probability.
        If `False`, do not treat the value `0` specially, but just as any
        other class.
    """
    assert 0 < gt_prob < 1, "`gt_prob must be in (0,1)."

    labels = labels.flatten()

    n_energy = -np.log((1.0 - gt_prob) / (n_labels - 1))
    p_energy = -np.log(gt_prob)

    # Note that the order of the following operations is important.
    # That's because the later ones overwrite part of the former ones, and only
    # after all of them is `U` correct!
    U = np.full((n_labels, len(labels)), n_energy, dtype='float32')
    U[labels - 1 if zero_unsure else labels, np.arange(U.shape[1])] = p_energy

    # Overwrite 0-labels using uniform probability, i.e. "unsure".
    if zero_unsure:
        U[:, labels == 0] = -np.log(1.0 / n_labels)

    return U

def _diagonal_compatibility(shape):
    return tf.eye(shape[0], shape[1], dtype=np.float32)

def _potts_compatibility(shape):
    return -1 * _diagonal_compatibility(shape)

# unary first
@tf.function
def inference(unary, image, spatial_filter, bilateral_filter, height, width, num_classes, theta_alpha, theta_beta, theta_gamma, spatial_compat, bilateral_compat, num_iterations):
    # # Check if data scale of `image` is [0, 1]
    # tf.debugging.assert_less_equal(tf.reduce_max(image), 1.)
    # tf.debugging.assert_greater_equal(tf.reduce_min(image), 0.)
    # # Check if `image` is three-channel and (h, w, 3)
    # tf.debugging.assert_rank(image, 3)
    # tf.debugging.assert_equal(tf.shape(image)[-1], 3)
    # # Check if theta_beta is float and < 1
    # tf.debugging.assert_less_equal(theta_beta, 1.)

    # spatial_weights = spatial_compat * _diagonal_compatibility((num_classes, num_classes)) # [n_classes, n_classes]
    # bilateral_weights = bilateral_compat * _diagonal_compatibility((num_classes, num_classes)) # [n_classes, n_classes]
    compatibility_matrix = _potts_compatibility((num_classes, num_classes)) # [n_classes, n_classes]

    # Create spatial and bilateral features
    ys, xs = tf.meshgrid(tf.range(height), tf.range(width), indexing='ij') # [h, w] and [h, w]
    ys, xs = tf.cast(ys, dtype=tf.float32), tf.cast(xs, dtype=tf.float32)

    spatial_feats = tf.stack([xs, ys], axis=-1) / theta_gamma # [h, w, 2]
    spatial_feats = tf.reshape(spatial_feats, shape=[n_feats, d_spfeats]) # [h x w, 2]
    
    bilateral_feats = tf.concat([tf.stack([xs, ys], axis=-1) / theta_alpha, image / theta_beta], axis=-1) # [h, w, d_bifeats]
    bilateral_feats = tf.reshape(bilateral_feats, shape=[n_feats, d_bifeats]) # [n_feats, d_bifeats]

    bilateral_filter.init(bilateral_feats)
    spatial_filter.init(spatial_feats)

    all_ones = tf.ones([n_feats, 1], dtype=tf.float32) # [n_feats, 1]

    # Compute symmetric weight
    spatial_norm_vals = spatial_filter.compute(all_ones) # [n_feats, 1]
    spatial_norm_vals = 1. / (spatial_norm_vals ** .5 + 1e-20)
    bilateral_norm_vals = bilateral_filter.compute(all_ones) # [n_feats, 1]
    bilateral_norm_vals = 1. / (bilateral_norm_vals ** .5 + 1e-20)

    # Initialize Q
    unary = tf.reshape(unary, shape=[-1, num_classes]) # [n_feats, n_classes]
    Q = tf.nn.softmax(-unary) # [n_feats, n_classes]

    for i in range(num_iterations):
        tmp1 = -unary # [n_feats, n_classes]

        # Symmetric normalization and spatial message passing
        spatial_out = spatial_filter.compute(Q * spatial_norm_vals) # [n_feats, n_classes]
        spatial_out *= spatial_norm_vals # [n_feats, n_classes]

        # Symmetric normalization and bilateral message passing
        bilateral_out = bilateral_filter.compute(Q * bilateral_norm_vals) # [n_feats, n_classes]
        bilateral_out *= bilateral_norm_vals # [n_feats, n_classes]

        # Message passing
        message_passing = spatial_compat * spatial_out + bilateral_compat * bilateral_out # [n_feats, n_classes]
        # spatial_out = tf.matmul(spatial_out, spatial_weights)
        # bilateral_out = tf.reshape(bilateral_out, [-1, num_classes])
        # bilateral_out = tf.matmul(bilateral_out, bilateral_weights)
        # message_passing = spatial_out + bilateral_out

        # Compatibility transform
        pairwise = tf.matmul(message_passing, compatibility_matrix) # [n_feats, n_classes]
        # pairwise = tf.reshape(pairwise, tf.shape(unary))

        # Local update
        tmp1 -= pairwise # [n_feats, n_classes]

        # Normalize
        Q = tf.nn.softmax(tmp1) # [n_feats, n_classes]

    return tf.reshape(Q, shape=[height, width, num_classes])


if __name__ == "__main__":
    img = cv2.imread('../../data/examples/im1.png')
    anno_rgb = cv2.imread('../../data/examples/anno1.png').astype(np.uint32)
    anno_lbl = anno_rgb[:,:,0] + (anno_rgb[:,:,1] << 8) + (anno_rgb[:,:,2] << 16)

    colors, labels = np.unique(anno_lbl, return_inverse=True)

    HAS_UNK = 0 in colors
    if HAS_UNK:
        print("Found a full-black pixel in annotation image, assuming it means 'unknown' label, and will thus not be present in the output!")
        print("If 0 is an actual label for you, consider writing your own code, or simply giving your labels only non-zero values.")
        colors = colors[1:]
    
    colorize = np.empty((len(colors), 3), np.uint8)
    colorize[:,0] = (colors & 0x0000FF)
    colorize[:,1] = (colors & 0x00FF00) >> 8
    colorize[:,2] = (colors & 0xFF0000) >> 16

    n_labels = len(set(labels.flat)) - int(HAS_UNK)
    print(n_labels, " labels", (" plus \"unknown\" 0: " if HAS_UNK else ""), set(labels.flat))

    unary = unary_from_labels(labels, n_labels, 0.7, HAS_UNK)
    height, width = img.shape[:2]
    unary = unary.reshape(n_labels, height, width)
    unary = np.transpose(unary, (1, 2, 0))
    print(unary.shape, img.shape)

    # Sizes and dims of spatial and color features
    d_spatial, d_image = 2, img.shape[-1]
    n_feats = height * width
    d_bifeats = d_spatial + d_image
    d_spfeats = d_spatial
    bilateral_filter = PermutohedralTF(n_feats, d_bifeats)
    spatial_filter = PermutohedralTF(n_feats, d_spfeats)

    pred = inference(tf.constant(unary.astype(np.float32)), tf.constant((img / 255.).astype(np.float32)), spatial_filter, bilateral_filter, height, width, n_labels, theta_alpha=80., theta_beta=.0625, theta_gamma=3., spatial_compat=3., bilateral_compat=10., num_iterations=10)
    pred = pred.numpy()
    
    MAP = np.argmax(pred, axis=-1)
    plt.imshow(MAP)
    plt.show()