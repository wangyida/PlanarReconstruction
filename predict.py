import os
import open3d as o3d
from matplotlib import cm
import cv2
import random
import numpy as np
from PIL import Image
from distutils.version import LooseVersion

from sacred import Experiment
from easydict import EasyDict as edict

import torch
import torch.nn.functional as F
import torchvision.transforms as tf

from models.baseline_same import Baseline as UNet
from utils.disp import tensor_to_image
from utils.disp import colors_256 as colors
from bin_mean_shift import Bin_Mean_Shift
from modules import get_coordinate_map
from utils.loss import Q_loss
from utils.loss import surface_normal_loss
from instance_parameter_loss import InstanceParameterLoss

ex = Experiment()


@ex.main
def predict(_run, _log):
    cfg = edict(_run.config)

    torch.manual_seed(cfg.seed)
    np.random.seed(cfg.seed)
    random.seed(cfg.seed)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # build network
    network = UNet(cfg.model)

    if not (cfg.resume_dir == 'None'):
        model_dict = torch.load(cfg.resume_dir, map_location=lambda storage, loc: storage)
        network.load_state_dict(model_dict)

    # load nets into gpu
    if cfg.num_gpus > 1 and torch.cuda.is_available():
        network = torch.nn.DataParallel(network)
    network.to(device)
    network.eval()

    transforms = tf.Compose([
        tf.ToTensor(),
        tf.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
    ])

    bin_mean_shift = Bin_Mean_Shift(device=device)
    k_inv_dot_xy1 = get_coordinate_map(device)
    instance_parameter_loss = InstanceParameterLoss(k_inv_dot_xy1)

    h, w = 192, 256

    with torch.no_grad():
        image = cv2.imread(cfg.image_path)
        # the network is trained with 192*256 and the intrinsic parameter is set as ScanNet
        image = cv2.resize(image, (w, h))
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(image)
        image = transforms(image)
        image = image.to(device).unsqueeze(0)
        # forward pass
        logit, embedding, _, _, param = network(image)

        prob = torch.sigmoid(logit[0])
        
        # infer per pixel depth using per pixel plane parameter, currently Q_loss need a dummy gt_depth as input
        _, _, per_pixel_depth = Q_loss(param, k_inv_dot_xy1, torch.ones_like(logit))

        # fast mean shift
        segmentation, sampled_segmentation, sample_param = bin_mean_shift.test_forward(
            prob, embedding[0], param, mask_threshold=0.1)

        # since GT plane segmentation is somewhat noise, the boundary of plane in GT is not well aligned, 
        # we thus use avg_pool_2d to smooth the segmentation results
        b = segmentation.t().view(1, -1, h, w)
        pooling_b = torch.nn.functional.avg_pool2d(b, (7, 7), stride=1, padding=(3, 3))
        b = pooling_b.view(-1, h*w).t()
        segmentation = b

        # infer instance depth
        instance_loss, instance_depth, instance_abs_disntace, instance_parameter = instance_parameter_loss(
            segmentation, sampled_segmentation, sample_param, torch.ones_like(logit), torch.ones_like(logit), False)

        # infer instance normal
        _, _, manhattan_norm, instance_norm = surface_normal_loss(param, torch.ones_like(image), None)

        # return cluster results
        predict_segmentation = segmentation.cpu().numpy().argmax(axis=1)

        # mask out non planar region
        predict_segmentation[prob.cpu().numpy().reshape(-1) <= 0.1] = 20
        predict_segmentation = predict_segmentation.reshape(h, w)

        # visualization and evaluation
        image = tensor_to_image(image.cpu()[0])
        mask = (prob > 0.1).float().cpu().numpy().reshape(h, w)
        depth = instance_depth.cpu().numpy()[0, 0].reshape(h, w)
        per_pixel_depth = per_pixel_depth.cpu().numpy()[0, 0].reshape(h, w)
        manhattan_normal_2d = manhattan_norm.cpu().numpy().reshape(h, w, 3) * np.expand_dims((predict_segmentation != 20), -1)
        instance_normal_2d = instance_norm.cpu().numpy().reshape(h, w, 3) * np.expand_dims((predict_segmentation != 20), -1)
        pcd = o3d.geometry.PointCloud()
        norm_colors = cm.Set3(predict_segmentation.reshape(w*h))
        pcd.points = o3d.utility.Vector3dVector(np.reshape(manhattan_normal_2d, (w*h , 3)))
        pcd.colors = o3d.utility.Vector3dVector(norm_colors[:,0:3])
        o3d.io.write_point_cloud('./manhattan_sphere.ply', pcd)
        pcd.points = o3d.utility.Vector3dVector(np.reshape(instance_normal_2d, (w*h , 3)))
        o3d.io.write_point_cloud('./instance_sphere.ply', pcd)
        normal_plot = cv2.resize(((manhattan_normal_2d+1) * 128).astype(np.uint8), (w, h))

        # use per pixel depth for non planar region
        depth = depth * (predict_segmentation != 20) + per_pixel_depth * (predict_segmentation == 20)

        # change non planar to zero, so non planar region use the black color
        predict_segmentation += 1
        predict_segmentation[predict_segmentation == 21] = 0

        pred_seg = cv2.resize(np.stack([colors[predict_segmentation, 0],
                                        colors[predict_segmentation, 1],
                                        colors[predict_segmentation, 2]], axis=2), (w, h))

        # blend image
        blend_pred = (pred_seg * 0.7 + image * 0.3).astype(np.uint8)

        mask = cv2.resize((mask * 255).astype(np.uint8), (w, h))
        mask = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)

        # visualize depth map as PlaneNet
        depth = 255 - np.clip(depth / 5 * 255, 0, 255).astype(np.uint8)
        depth = cv2.cvtColor(cv2.resize(depth, (w, h)), cv2.COLOR_GRAY2BGR)

        Camera_fx = 481.2
        Camera_fy = 480.0
        Camera_cx = -319.5
        Camera_cy = 239.50

        ##
        # Camera_fx = 535.4
        # Camera_fy = 539.2
        # Camera_cx = 320.1
        # Camera_cy = 247.6

        Camera_fx = 518.8
        Camera_fy = 518.8
        Camera_cx = 320
        Camera_cy = 240
        points = []
        points_instance=[]
        scalingFactor = 1.0
        for v in range(h):
            for u in range(w):
                color = image[v, u]
                color_instance=pred_seg[v,u]
                Z = (depth[v, u]/scalingFactor)[0]
                print(Z)
                if Z == 0: continue
                X = (u - Camera_cx/2) * Z / Camera_fx*2
                Y = (v - Camera_cy/2) * Z / Camera_fy*2
                # points.append("%f %f %f %d %d %d 0\n" % (X, Y, Z, color[2], color[1], color[0]))
                points_instance.append("%f %f %f %d %d %d 0\n" % (X, Y, Z, color_instance[2], color_instance[1], color_instance[0]))
        # file = open('./pointCloud.ply', "w")
        file1 = open('./pointCloud_instance.ply', "w")
        # file.write('''ply
        #        format ascii 1.0
        #        element vertex %d
        #        property float x
        #        property float y
        ##        property float z
        #        property uchar red
        #        property uchar green
        #        property uchar blue
        #        property uchar alpha
        #        end_header
        #        %s
        #        ''' % (len(points), "".join(points)))
        file1.write('''ply
                       format ascii 1.0
                       element vertex %d
                       property float x
                       property float y
                       property float z
                       property uchar red
                       property uchar green
                       property uchar blue
                       property uchar alpha
                       end_header
                       %s
                       ''' % (len(points_instance), "".join(points_instance)))
        # file.close()
        file1.close()

        depth_norm = cv2.cvtColor(cv2.resize(depth_norm, (w, h)), cv2.COLOR_GRAY2BGR)
        cv2.imwrite("./depth.png", depth_norm)
        #cv2.imwrite("./rgb.png",image)



        #
        # camera_fx = 588.03
        # camera_fy = 587.07
        #
        # for m in range(0, h):
        #     for n in range(0, w):
        #         d = depth[m][n][0] + depth[m][n][1] * 256
        #         if d == 0:
        #             pass
        #         else:
        #             z = float(d)
        #             x = n * z / camera_fx
        #             y = m * z / camera_fy
        #             points = [x, y, z]

        image = np.concatenate((image, pred_seg, blend_pred, mask, depth, normal_plot), axis=1)

        cv2.imshow('image', image)
        cv2.waitKey(0)


if __name__ == '__main__':
    assert LooseVersion(torch.__version__) >= LooseVersion('0.4.0'), \
        'PyTorch>=0.4.0 is required'

    ex.add_config('./configs/predict.yaml')
    ex.run_commandline()
