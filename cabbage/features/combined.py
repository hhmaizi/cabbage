import numpy as np
from time import time
from os import makedirs, listdir, remove
from os.path import join, isfile, isdir, exists, splitext
from cabbage.features.GenerateFeatureVector import pairwise_features
from cabbage.data.video import VideoData
from cabbage.features.ReId import get_element
from time import time
from keras.applications.vgg16 import preprocess_input
import cabbage.features.spatio as st
from cabbage.regression.Regression import get_default_W
from cabbage.features.deepmatching import ReadOnlyDeepMatching
from cabbage.features.ReId import StackNet64x64
from cabbage.features.deepmatching import DeepMatching
import json
import subprocess

class AABBLookup:
    """ helper function to easier map the AABB's
    """

    def __init__(self, Dt, X, H=64, W=64):
        """ctor
            Dt: {np.array} detections for the video
                -> [(frame, x, y, w, h, score), ...]

            X: {np.array} (n, w, h, 3) video of the detections
        """
        n, m = Dt.shape
        assert m == 6

        self.Im = np.zeros((n, H, W, 3), 'uint8')
        self.AABBs = np.zeros((n, 4), 'float32')
        IDS_IN_FRAME = [None] * (n + 1)  # frames start at 1 and not at 0
        self.Scores = [0] * n
        self.Frames = np.array([0] * n)

        for i, (frame, x, y, w, h, score) in enumerate(Dt):
            frame = int(frame)
            im = get_element(X[frame-1], (x,y,w,h), (W, H), True)
            self.Im[i] = im
            self.AABBs[i] = np.array([x,y,w,h])
            self.Scores[i] = score
            self.Frames[i] = frame

            if IDS_IN_FRAME[frame] is None:
                IDS_IN_FRAME[frame] = []
            IDS_IN_FRAME[frame].append(i)

        self.ids_in_frame = IDS_IN_FRAME
        self.Scores = np.array(self.Scores)
        self.LAST_FRAME = X.shape[0]


    def __getitem__(self, i):
        return self.AABBs[i], self.Im[i], self.Scores[i], self.Frames[i]


    def get_all_pairs(self, dmax):
        """ get all possible pairs
        """
        LAST_FRAME = self.LAST_FRAME
        ALL_PAIRS = []
        __start = time()
        for frame_i, ids_in_frame in enumerate(self.ids_in_frame):
            if ids_in_frame is None:
                continue

            for i in ids_in_frame:
                for j in ids_in_frame:
                    if i < j:
                        ALL_PAIRS.append((i,j))

                for frame_j in range(frame_i + 1, min(frame_i + dmax + 1, LAST_FRAME)):
                    if self.ids_in_frame[frame_j] is None:
                        continue
                    for j in self.ids_in_frame[frame_j]:
                        if j > i:
                            ALL_PAIRS.append((i, j))

            if frame_i % 100 == 0:
                print('handle frame ' + str(frame_i) + " from " + str(LAST_FRAME))

        __end = time()
        ALL_PAIRS = np.array(ALL_PAIRS, 'int32')
        print("ALL PAIRS:", ALL_PAIRS.shape)
        print('\telapsed seconds:', __end - __start)
        return ALL_PAIRS




def gen_feature_batch(batch, lookup, dmax, dm, reid, W, video_name):
    """
        batch: {np.array} [(i,j), ...] list of i and j parameters
        lookup: {AABBLookup} lookup generated by the data
        dmax: {int32} delta-max
        dm: {DeepMatching}
    """
    i,j = batch[:,0],batch[:,1]
    aabb_j, Im_j, scores_j, frame_j = lookup[j]
    aabb_i, Im_i, scores_i, frame_i = lookup[i]

    delta = frame_j - frame_i
    IN_RANGE = (delta < dmax).nonzero()
    delta = delta[IN_RANGE]

    aabb_j, aabb_i = aabb_j[IN_RANGE], aabb_i[IN_RANGE]
    scores_i, scores_j = scores_i[IN_RANGE], scores_j[IN_RANGE]
    frame_i, framej = frame_i[IN_RANGE], frame_j[IN_RANGE]

    Im_j, Im_i = \
        preprocess_input(Im_j[IN_RANGE].astype('float64')), \
        preprocess_input(Im_i[IN_RANGE].astype('float64'))

    SCORES = np.where(scores_j < scores_i, scores_j, scores_i)

    ST = np.array(
        [st.calculate(bb1, bb2) for bb1, bb2 in zip(aabb_i, aabb_j)]
    )

    DM = np.array(
        [dm.calculate_cost(video_name, f1, bb1, f2, bb2) for \
            f1, bb1, f2, bb2 in zip(frame_i, aabb_i, frame_j, aabb_j)]
    )

    Y = reid.predict_raw(np.concatenate([Im_i, Im_j], axis=3))[:,0]

    Bias = np.ones(ST.shape)

    assert np.min(delta) >= 0

    F = np.array([
        Bias,
        ST, DM, Y, SCORES,
        ST**2, ST * DM, ST * Y, ST * SCORES,
        DM**2, DM * Y, DM * SCORES,
        Y**2, Y * SCORES,
        SCORES**2
    ]).T

    edge_weights = np.einsum('ij,ij->i', F, W[delta])
    edge_weights *= -1

    return delta, edge_weights, i, j
