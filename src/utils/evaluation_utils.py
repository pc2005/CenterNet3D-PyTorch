"""
# -*- coding: utf-8 -*-
-----------------------------------------------------------------------------------
# Author: Nguyen Mau Dung
# DoC: 2020.08.17
# email: nguyenmaudung93.kstn@gmail.com
-----------------------------------------------------------------------------------
# Description: The utils for evaluation
# Refer from: https://github.com/xingyizhou/CenterNet
"""

from __future__ import division
import sys

import torch
import numpy as np
import torch.nn.functional as F

sys.path.append('../')
import config.kitti_config as cnf


def _nms(heat, kernel=3):
    pad = (kernel - 1) // 2
    hmax = F.max_pool2d(heat, (kernel, kernel), stride=1, padding=pad)
    keep = (hmax == heat).float()

    return heat * keep


def _gather_feat(feat, ind, mask=None):
    dim = feat.size(2)
    ind = ind.unsqueeze(2).expand(ind.size(0), ind.size(1), dim)
    feat = feat.gather(1, ind)
    if mask is not None:
        mask = mask.unsqueeze(2).expand_as(feat)
        feat = feat[mask]
        feat = feat.view(-1, dim)
    return feat


def _transpose_and_gather_feat(feat, ind):
    feat = feat.permute(0, 2, 3, 1).contiguous()
    feat = feat.view(feat.size(0), -1, feat.size(3))
    feat = _gather_feat(feat, ind)
    return feat


def _topk(scores, K=40):
    batch, cat, height, width = scores.size()

    topk_scores, topk_inds = torch.topk(scores.view(batch, cat, -1), K)

    topk_inds = topk_inds % (height * width)
    topk_ys = (topk_inds / width).int().float()
    topk_xs = (topk_inds % width).int().float()

    topk_score, topk_ind = torch.topk(topk_scores.view(batch, -1), K)
    topk_clses = (topk_ind / K).int()
    topk_inds = _gather_feat(topk_inds.view(batch, -1, 1), topk_ind).view(batch, K)
    topk_ys = _gather_feat(topk_ys.view(batch, -1, 1), topk_ind).view(batch, K)
    topk_xs = _gather_feat(topk_xs.view(batch, -1, 1), topk_ind).view(batch, K)

    return topk_score, topk_inds, topk_clses, topk_ys, topk_xs


def _topk_channel(scores, K=40):
    batch, cat, height, width = scores.size()

    topk_scores, topk_inds = torch.topk(scores.view(batch, cat, -1), K)

    topk_inds = topk_inds % (height * width)
    topk_ys = (topk_inds / width).int().float()
    topk_xs = (topk_inds % width).int().float()

    return topk_scores, topk_inds, topk_ys, topk_xs


def centernet3d_decode(hm_cen, hm_conners, cen_offset, direction, z_coor, dim, K=40):
    batch_size, num_classes, height, width = hm_cen.size()

    hm_cen = _nms(hm_cen)
    scores, inds, clses, ys, xs = _topk(hm_cen, K=K)
    if cen_offset is not None:
        cen_offset = _transpose_and_gather_feat(cen_offset, inds)
        cen_offset = cen_offset.view(batch_size, K, 2)
        xs = xs.view(batch_size, K, 1) + cen_offset[:, :, 0:1]
        ys = ys.view(batch_size, K, 1) + cen_offset[:, :, 1:2]
    else:
        xs = xs.view(batch_size, K, 1) + 0.5
        ys = ys.view(batch_size, K, 1) + 0.5

    direction = _transpose_and_gather_feat(direction, inds)
    direction = direction.view(batch_size, K, 8)
    z_coor = _transpose_and_gather_feat(z_coor, inds)
    z_coor = z_coor.view(batch_size, K, 1)
    dim = _transpose_and_gather_feat(dim, inds)
    dim = dim.view(batch_size, K, 3)
    clses = clses.view(batch_size, K, 1).float()
    scores = scores.view(batch_size, K, 1)

    # (scores x 1, xs x 1, ys x 1, direction x 2, z_coor x 1, dim x 3, clses x 1)
    # (scores-0:1, xs-1:2, ys-2:3, direction-3:5, z_coor-5:6, dim-6:9, clses-9:10)
    # detections: [batch_size, K, 10]
    detections = torch.cat([scores, xs, ys, direction, z_coor, dim, clses], dim=2)

    return detections


def get_yaw(direction):
    return np.arctan2(direction[:, 0:1], direction[:, 1:2])


def post_processing(detections, num_classes=3, down_ratio=4):
    """
    :param detections: [batch_size, K, 10]
    # (scores x 1, xs x 1, ys x 1, direction x 2, z_coor x 1, dim x 3, clses x 1)
    # (scores-0:1, xs-1:2, ys-2:3, direction-3:5, z_coor-5:6, dim-6:9, clses-9:10)
    :return:
    """
    # TODO: Need to consider rescale to the original scale: bbox, xs, ys, and ver_coor - 1:25

    ret = []
    for i in range(detections.shape[0]):
        top_preds = {}
        classes = detections[i, :, -1]
        for j in range(num_classes):
            inds = (classes == j)
            # x, y, z, h, w, l, yaw
            bc = cnf.boundary
            top_preds[j] = np.concatenate([
                detections[i, inds, :1].astype(np.float32),
                detections[i, inds, 2:3].astype(np.float32) * down_ratio / 1400 * cnf.bound_size_x + bc['minX'],
                detections[i, inds, 1:2].astype(np.float32) * down_ratio / 1600 * cnf.bound_size_y + bc['minY'],
                detections[i, inds, 5:6].astype(np.float32),
                detections[i, inds, 6:9].astype(np.float32),
                get_yaw(detections[i, inds, 3:5]).astype(np.float32)], axis=1),
            ret.append(top_preds)

    return ret


def get_final_pred(detections, num_classes=3, peak_thresh=0.2):
    for j in range(num_classes):
        if len(detections[j] > 0):
            keep_inds = (detections[j][:, 0] > peak_thresh)
            detections[j] = detections[j][keep_inds]

    return detections