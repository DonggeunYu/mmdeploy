# Copyright (c) OpenMMLab. All rights reserved.

import torch

from mmdeploy.codebase.mmdet import get_post_processing_params
from mmdeploy.codebase.mmdet.core.post_processing import multiclass_nms
from mmdeploy.core import FUNCTION_REWRITER


@FUNCTION_REWRITER.register_rewriter(
    'mmdet.models.dense_heads.GuidedAnchorHead.get_bboxes')
def guided_anchor_head__get_bboxes(ctx,
                                   self,
                                   cls_scores,
                                   bbox_preds,
                                   shape_preds,
                                   loc_preds,
                                   img_metas=None,
                                   cfg=None,
                                   **kwargs):
    """Rewrite `get_bboxes` of `RPNHead` for default backend.

    Rewrite this function to deploy model, transform network output for a
    batch into bbox predictions.

    Args:
        ctx (ContextCaller): The context with additional information.
        self (FoveaHead): The instance of the class FoveaHead.
        cls_scores (list[Tensor]): Box scores for each scale level
            with shape (N, num_anchors * num_classes, H, W).
        bbox_preds (list[Tensor]): Box energies / deltas for each scale
            level with shape (N, num_anchors * 4, H, W).
        score_factors (list[Tensor], Optional): Score factor for
            all scale level, each is a 4D-tensor, has shape
            (batch_size, num_priors * 1, H, W). Default None.
        img_metas (list[dict]):  Meta information of the image, e.g.,
            image size, scaling factor, etc.
        cfg (mmcv.Config | None): Test / postprocessing configuration,
            if None, test_cfg would be used. Default: None.
        rescale (bool): If True, return boxes in original image space.
            Default False.
        with_nms (bool): If True, do nms before return boxes.
            Default: True.
    Returns:
        If with_nms == True:
            tuple[Tensor, Tensor]: tuple[Tensor, Tensor]: (dets, labels),
            `dets` of shape [N, num_det, 5] and `labels` of shape
            [N, num_det].
        Else:
            tuple[Tensor, Tensor, Tensor]: batch_mlvl_bboxes, batch_mlvl_scores
    """
    assert len(cls_scores) == len(bbox_preds) == len(shape_preds) == len(
        loc_preds)
    num_levels = len(cls_scores)
    featmap_sizes = [featmap.size()[-2:] for featmap in cls_scores]
    device = cls_scores[0].device
    # get guided anchors

    cfg = self.test_cfg if cfg is None else cfg
    deploy_cfg = ctx.cfg
    post_params = get_post_processing_params(deploy_cfg)
    iou_threshold = cfg.nms.get('iou_threshold', post_params.iou_threshold)
    score_threshold = cfg.get('score_thr', post_params.score_threshold)

    _, guided_anchors = self.get_anchors(
        featmap_sizes,
        shape_preds,
        loc_preds,
        img_metas,
        use_loc_filter=not self.training,
        device=device)

    result_proposals = []
    result_labels = []
    for img_id in range(len(img_metas)):
        cls_score_list = [
            cls_scores[i][img_id].detach() for i in range(num_levels)
        ]
        bbox_pred_list = [
            bbox_preds[i][img_id].detach() for i in range(num_levels)
        ]
        guided_anchor_list = [
            guided_anchors[img_id][i].detach() for i in range(num_levels)
        ]

        img_shape = img_metas[img_id]['img_shape']

        mlvl_proposals = []
        mlvl_labels = []
        for idx in range(len(cls_score_list)):
            rpn_cls_score = cls_score_list[idx]
            rpn_bbox_pred = bbox_pred_list[idx]
            anchors = guided_anchor_list[idx]
            print(rpn_cls_score.shape, rpn_bbox_pred.shape)
            assert rpn_cls_score.size()[-2:] == rpn_bbox_pred.size()[-2:]
            # if no location is kept, end.

            rpn_cls_score = rpn_cls_score.permute(1, 2, 0)
            if self.use_sigmoid_cls:
                rpn_cls_score = rpn_cls_score.reshape(-1)
                scores = rpn_cls_score.sigmoid()
            else:
                rpn_cls_score = rpn_cls_score.reshape(-1, 2)
                # remind that we set FG labels to [0, num_class-1]
                # since mmdet v2.0
                # BG cat_id: num_class
                scores = rpn_cls_score.softmax(dim=1)[:, :-1]
            # filter scores, bbox_pred w.r.t. mask.
            # anchors are filtered in get_anchors() beforehand.
            rpn_bbox_pred = rpn_bbox_pred.permute(1, 2, 0).reshape(-1, 4)
            print(rpn_bbox_pred.shape, rpn_cls_score.shape)

            if scores.dim() == 0:
                rpn_bbox_pred = rpn_bbox_pred.unsqueeze(0)
                anchors = anchors.unsqueeze(0)
                scores = scores.unsqueeze(0)
            # filter anchors, bbox_pred, scores w.r.t. scores

            if cfg.nms_pre > 0 and scores.shape[0] > cfg.nms_pre:
                _, topk_inds = scores.topk(cfg.nms_pre)
                rpn_bbox_pred = rpn_bbox_pred[topk_inds, :]
                anchors = anchors[topk_inds, :]
                scores = scores[topk_inds]
            # get proposals w.r.t. anchors and rpn_bbox_pred
            proposals = self.bbox_coder.decode(
                anchors, rpn_bbox_pred, max_shape=img_shape)

            # NMS in current level
            proposals, labels = multiclass_nms(
                proposals.reshape(1, -1, 4),
                scores.reshape(1, -1, 1),
                max_output_boxes_per_class=cfg.nms_pre,
                iou_threshold=iou_threshold,
                score_threshold=score_threshold,
                pre_top_k=cfg.nms_pre,
                keep_top_k=cfg.nms_pre)
            proposals = proposals[:, :cfg.nms_post]
            labels = labels[:, :cfg.nms_post]
            mlvl_proposals.append(proposals)
            mlvl_labels.append(labels)
        proposals = torch.cat(mlvl_proposals, 1)
        labels = torch.cat(mlvl_labels, 1)
        if cfg.get('nms_across_levels', False):
            # NMS across multi levels
            proposals, labels = multiclass_nms(
                proposals[:, :, :4],
                proposals[:, :, -1],
                max_output_boxes_per_class=cfg.max_per_img,
                iou_threshold=iou_threshold,
                score_threshold=score_threshold,
                pre_top_k=cfg.max_per_img,
                keep_top_k=cfg.max_per_img)

        result_proposals.append(proposals)
        result_labels.append(labels)

    result_proposals = torch.cat(result_proposals, 1)
    result_labels = torch.cat(result_labels, 1)
    return result_proposals, result_labels


@FUNCTION_REWRITER.register_rewriter(
    'mmdet.models.dense_heads.GuidedAnchorHead.get_anchors')
def guided_anchor_head_get_anchors(ctx,
                                   self,
                                   featmap_sizes,
                                   shape_preds,
                                   loc_preds,
                                   img_metas,
                                   use_loc_filter=False,
                                   device='cuda'):
    """Get squares according to feature map sizes and guided anchors.

    Args:
        featmap_sizes (list[tuple]): Multi-level feature map sizes.
        shape_preds (list[tensor]): Multi-level shape predictions.
        loc_preds (list[tensor]): Multi-level location predictions.
        img_metas (list[dict]): Image meta info.
        use_loc_filter (bool): Use loc filter or not.
        device (torch.device | str): device for returned tensors

    Returns:
        tuple: square approxs of each image, guided anchors of each image,
            loc masks of each image
    """
    num_imgs = len(img_metas)
    num_levels = len(featmap_sizes)

    # since feature map sizes of all images are the same, we only compute
    # squares for one time
    multi_level_squares = self.square_anchor_generator.grid_priors(
        featmap_sizes, device=device)
    squares_list = [multi_level_squares for _ in range(num_imgs)]

    # for each image, we compute multi level guided anchors
    guided_anchors_list = []
    for img_id, img_meta in enumerate(img_metas):
        multi_level_guided_anchors = []
        for i in range(num_levels):
            squares = squares_list[img_id][i]
            shape_pred = shape_preds[i][img_id]
            loc_pred = loc_preds[i][img_id]
            guided_anchors = self._get_guided_anchors_single(
                squares, shape_pred, loc_pred, use_loc_filter=use_loc_filter)
            multi_level_guided_anchors.append(guided_anchors)
        guided_anchors_list.append(multi_level_guided_anchors)
    return squares_list, guided_anchors_list


@FUNCTION_REWRITER.register_rewriter(
    'mmdet.models.dense_heads.GuidedAnchorHead._get_guided_anchors_single')
def guided_anchor_head__get_guided_anchors_single(ctx,
                                                  self,
                                                  squares,
                                                  shape_pred,
                                                  loc_pred,
                                                  use_loc_filter=False):
    """Get guided anchors and loc masks for a single level.

    Args:
        square (tensor): Squares of a single level.
        shape_pred (tensor): Shape predictions of a single level.
        loc_pred (tensor): Loc predictions of a single level.
        use_loc_filter (list[tensor]): Use loc filter or not.
    Returns:
        tuple: guided anchors
    """
    # calculate location filtering mask
    # calculate guided anchors
    anchor_deltas = shape_pred.permute(1, 2, 0).contiguous().view(-1,
                                                                  2).detach()
    bbox_deltas = anchor_deltas.new_full(squares.size(), 0)
    bbox_deltas[:, 2:] = anchor_deltas
    guided_anchors = self.anchor_coder.decode(
        squares, bbox_deltas, wh_ratio_clip=1e-6)
    return guided_anchors
