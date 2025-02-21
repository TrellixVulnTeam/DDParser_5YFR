# -*- coding: UTF-8 -*-
################################################################################
#
#   Copyright (c) 2020  Baidu, Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#################################################################################
"""
本文件定义了使用到的工具类和函数
"""
from __future__ import print_function
from __future__ import unicode_literals
from __future__ import absolute_import
from __future__ import division

import copy
import logging
import logging.handlers
import os
import requests
import shutil
import tarfile
import unicodedata

import numpy as np
from tqdm import tqdm

from ddparser.parser.nets import nn

pad = '<pad>'
unk = '<unk>'
bos = '<bos>'
eos = '<eos>'

DOWNLOAD_MODEL_PATH_DICT = {
    'ernie-lstm': "https://ddparser.bj.bcebos.com/DDParser-ernie-lstm-1.0.6.tar.gz",
}


def kmeans(x, k):
    """kmeans algorithm, put sentence id into k buckets according to sentence length
    
    Args:
        x: list, sentence length
        k: int, k clusters

    Returns:
        centroids: list, center point of k clusters
        clusters: list(tuple), k clusters
    """
    x = np.array(x, dtype=np.float32)
    # count the frequency of each datapoint
    d, indices, f = np.unique(x, return_inverse=True, return_counts=True)
    # calculate the sum of the values of the same datapoints
    total = d * f
    # initialize k centroids randomly
    c, old = d[np.random.permutation(len(d))[:k]], None
    # assign labels to each datapoint based on centroids
    dists_abs = np.absolute(d[..., np.newaxis] - c)
    dists, y = dists_abs.min(axis=-1), dists_abs.argmin(axis=-1)
    # the number of clusters must not be greater than that of datapoints
    k = min(len(d), k)

    while old is None or not np.equal(c, old).all():
        # if an empty cluster is encountered,
        # choose the farthest datapoint from the biggest cluster
        # and move that the empty one
        for i in range(k):
            if not np.equal(y, i).any():
                # mask.shape=[k, n]
                mask = y == np.arange(k)[..., np.newaxis]
                lens = mask.sum(axis=-1)
                biggest = mask[lens.argmax()].nonzero()[0]
                farthest = dists[biggest].argmax()
                y[biggest[farthest]] = i
        mask = y == np.arange(k)[..., np.newaxis]
        # update the centroids
        c, old = (total * mask).sum(-1) / (f * mask).sum(-1), c
        # re-assign all datapoints to clusters
        dists_abs = np.absolute(d[..., np.newaxis] - c)
        dists, y = dists_abs.min(axis=-1), dists_abs.argmin(axis=-1)
    # assign all datapoints to the new-generated clusters
    # without considering the empty ones
    y, assigned = y[indices], np.unique(y).tolist()
    # get the centroids of the assigned clusters
    centroids = c[assigned].tolist()
    # map all values of datapoints to buckets
    clusters = [np.equal(y, i).nonzero()[0].tolist() for i in assigned]

    return centroids, clusters


def eisner(scores, mask):
    """Eisner algorithm is a general dynamic programming decoding algorithm for bilexical grammar.

    Args：
        scores: Adjacency matrix，shape=(batch, seq_len, seq_len)
        mask: mask matrix，shape=(batch, sql_len)

    Returns:
        output，shape=(batch, seq_len)，the index of the parent node corresponding to the token in the query

    """
    lens = mask.sum(1)
    batch_size, seq_len, _ = scores.shape
    scores = scores.transpose(2, 1, 0)
    # score for incomplete span
    s_i = np.full_like(scores, float('-inf'))
    # score for complete span
    s_c = np.full_like(scores, float('-inf'))
    # incompelte span position for backtrack
    p_i = np.zeros((seq_len, seq_len, batch_size), dtype=np.int64)
    # compelte span position for backtrack
    p_c = np.zeros((seq_len, seq_len, batch_size), dtype=np.int64)
    # set 0 to s_c.diagonal
    s_c = nn.fill_diagonal(s_c, 0)
    # contiguous
    s_c = np.ascontiguousarray(s_c)
    s_i = np.ascontiguousarray(s_i)
    for w in range(1, seq_len):
        n = seq_len - w
        starts = np.arange(n, dtype=np.int64)[np.newaxis, :]
        # ilr = C(i->r) + C(j->r+1)
        ilr = nn.stripe(s_c, n, w) + nn.stripe(s_c, n, w, (w, 1))
        # [batch_size, n, w]
        ilr = ilr.transpose(2, 0, 1)
        # scores.diagonal(-w).shape:[batch, n]
        il = ilr + scores.diagonal(-w)[..., np.newaxis]
        # I(j->i) = max(C(i->r) + C(j->r+1) + s(j->i)), i <= r < j
        il_span, il_path = il.max(-1), il.argmax(-1)
        s_i = nn.fill_diagonal(s_i, il_span, offset=-w)
        p_i = nn.fill_diagonal(p_i, il_path + starts, offset=-w)

        ir = ilr + scores.diagonal(w)[..., np.newaxis]
        # I(i->j) = max(C(i->r) + C(j->r+1) + s(i->j)), i <= r < j
        ir_span, ir_path = ir.max(-1), ir.argmax(-1)
        s_i = nn.fill_diagonal(s_i, ir_span, offset=w)
        p_i = nn.fill_diagonal(p_i, ir_path + starts, offset=w)

        # C(j->i) = max(C(r->i) + I(j->r)), i <= r < j
        cl = nn.stripe(s_c, n, w, (0, 0), 0) + nn.stripe(s_i, n, w, (w, 0))
        cl = cl.transpose(2, 0, 1)
        cl_span, cl_path = cl.max(-1), cl.argmax(-1)
        s_c = nn.fill_diagonal(s_c, cl_span, offset=-w)
        p_c = nn.fill_diagonal(p_c, cl_path + starts, offset=-w)

        # C(i->j) = max(I(i->r) + C(r->j)), i < r <= j
        cr = nn.stripe(s_i, n, w, (0, 1)) + nn.stripe(s_c, n, w, (1, w), 0)
        cr = cr.transpose(2, 0, 1)
        cr_span, cr_path = cr.max(-1), cr.argmax(-1)
        s_c = nn.fill_diagonal(s_c, cr_span, offset=w)
        s_c[0, w][np.not_equal(lens, w)] = float('-inf')
        p_c = nn.fill_diagonal(p_c, cr_path + starts + 1, offset=w)

    predicts = []
    p_c = p_c.transpose(2, 0, 1)
    p_i = p_i.transpose(2, 0, 1)
    for i, length in enumerate(lens.tolist()):
        heads = np.ones(length + 1, dtype=np.int64)
        nn.backtrack(p_i[i], p_c[i], heads, 0, length, True)
        predicts.append(heads)

    return nn.pad_sequence(predicts, fix_len=seq_len)


class NODE:
    """NODE class"""
    def __init__(self, id=None, parent=None):
        self.lefts = []
        self.rights = []
        self.id = int(id)
        self.parent = parent if parent is None else int(parent)


class DepTree:
    """
    DepTree class, used to check whether the prediction result is a project Tree.
    A projective tree means that you can project the tree without crossing arcs.
    """
    def __init__(self, sentence):
        # set root head to -1
        sentence = copy.deepcopy(sentence)
        sentence[0] = -1
        self.sentence = sentence
        self.build_tree()
        self.visit = [False] * len(sentence)

    def build_tree(self):
        """Build the tree"""
        self.nodes = [NODE(index, p_index) for index, p_index in enumerate(self.sentence)]
        # set root
        self.root = self.nodes[0]
        for node in self.nodes[1:]:
            self.add(self.nodes[node.parent], node)

    def add(self, parent, child):
        """Add a child node"""
        if parent.id is None or child.id is None:
            raise Exception("id is None")
        if parent.id < child.id:
            parent.rights = sorted(parent.rights + [child.id])
        else:
            parent.lefts = sorted(parent.lefts + [child.id])

    def judge_legal(self):
        """Determine whether it is a project tree"""
        target_seq = list(range(len(self.nodes)))
        if len(self.root.lefts + self.root.rights) != 1:
            return False
        cur_seq = self.inorder_traversal(self.root)
        if target_seq != cur_seq:
            return False
        else:
            return True

    def inorder_traversal(self, node):
        """Inorder traversal"""
        if self.visit[node.id]:
            return []
        self.visit[node.id] = True
        lf_list = []
        rf_list = []
        for ln in node.lefts:
            lf_list += self.inorder_traversal(self.nodes[ln])
        for rn in node.rights:
            rf_list += self.inorder_traversal(self.nodes[rn])

        return lf_list + [node.id] + rf_list


def ispunct(token):
    """Is the token a punctuation"""
    return all(unicodedata.category(char).startswith('P') for char in token)


def istree(sequence):
    """Is the sequence a project tree"""
    return DepTree(sequence).judge_legal()


def numericalize(sequence):
    """Convert the dtype of sequence to int"""
    return [int(i) for i in sequence]


def init_log(log_path,
             devices,
             level=logging.INFO,
             when="D",
             backup=7,
             format="%(levelname)s: %(asctime)s: %(filename)s:%(lineno)d * %(thread)d %(message)s",
             datefmt="%m-%d %H:%M:%S"):
    """initialize log module"""
    formatter = logging.Formatter(format, datefmt)
    logger = logging.getLogger()
    if devices != 0:
        logger.setLevel(logging.FATAL)

    else:
        logger.setLevel(level)

        dir = os.path.dirname(log_path)
        if not os.path.isdir(dir):
            os.makedirs(dir)

        handler = logging.handlers.TimedRotatingFileHandler(log_path + str(devices) + ".log",
                                                            when=when,
                                                            backupCount=backup)
        handler.setLevel(level)
        handler.setFormatter(formatter)
        logger.addHandler(handler)

        handler = logging.handlers.TimedRotatingFileHandler(log_path + str(devices) + ".log.wf",
                                                            when=when,
                                                            backupCount=backup)
        handler.setLevel(logging.WARNING)
        handler.setFormatter(formatter)
        logger.addHandler(handler)


def download_model_from_url(path, model='lstm'):
    """Downlod the model from url"""
    if os.path.exists(path):
        return
    download_model_path = DOWNLOAD_MODEL_PATH_DICT[model]
    logging.info("The first run will download the pre-trained model, which will take some time, please be patient!")
    dir_path = os.path.dirname(path)
    temp_path = dir_path + "_temp"
    file_path = os.path.join(temp_path, "model.tar.gz")
    if os.path.exists(temp_path):
        shutil.rmtree(temp_path)
    os.mkdir(temp_path)

    with (open(file_path, 'ab')) as f:
        r = requests.get(download_model_path, stream=True)
        total_len = int(r.headers.get('content-length'))
        for chunk in tqdm(r.iter_content(chunk_size=1024),
                          total=total_len // 1024,
                          desc='downloading %s' % download_model_path,
                          unit='KB'):
            if chunk:
                f.write(chunk)
                f.flush()

    logging.debug('extacting... to %s' % file_path)
    with tarfile.open(file_path) as tf:
        def is_within_directory(directory, target):
            
            abs_directory = os.path.abspath(directory)
            abs_target = os.path.abspath(target)
        
            prefix = os.path.commonprefix([abs_directory, abs_target])
            
            return prefix == abs_directory
        
        def safe_extract(tar, path=".", members=None, *, numeric_owner=False):
        
            for member in tar.getmembers():
                member_path = os.path.join(path, member.name)
                if not is_within_directory(path, member_path):
                    raise Exception("Attempted Path Traversal in Tar File")
        
            tar.extractall(path, members, numeric_owner=numeric_owner) 
            
        
        safe_extract(tf, path=temp_path)

    # mv temp_path path
    for _, dirs, _ in os.walk(temp_path):
        if len(dirs) != 1:
            raise RuntimeError("There is a problem with the model catalogue," "please contact the author")
        shutil.move(os.path.join(temp_path, dirs[0]), path)
    # delete temp directory
    shutil.rmtree(temp_path)
