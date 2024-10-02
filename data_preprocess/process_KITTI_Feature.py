from util.trajectory import read_trajectory
from model import load_model
import MinkowskiEngine as ME
from util.pointcloud import make_open3d_point_cloud, make_open3d_feature
from tqdm import tqdm
import open3d as o3d
import numpy as np
import pickle
import torch
import glob
import os
os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
os.environ["CUDA_VISIBLE_DEVICES"] = "6"


device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def apply_transform(pts, trans):
    R = trans[:3, :3]
    T = trans[:3, 3]
    pts = pts @ R.T + T
    return pts


def get_video_odometry(drive, indices=None, ext='.txt', return_all=False):
    data_path = root + '/poses/%02d.txt' % drive
    if data_path not in kitti_cache:
        kitti_cache[data_path] = np.genfromtxt(data_path)
    if return_all:
        return kitti_cache[data_path]
    else:
        return kitti_cache[data_path][indices]


def odometry_to_positions(odometry):
    T_w_cam0 = odometry.reshape(3, 4)
    T_w_cam0 = np.vstack((T_w_cam0, [0, 0, 0, 1]))
    return T_w_cam0


def _get_velodyne_fn(drive, t):
    fname = root + '/sequences/%02d/velodyne/%06d.bin' % (drive, t)
    return fname


if __name__ == '__main__':
    files = []
    kitti_cache = {}
    MIN_DIST = 10

    velo2cam = np.array([[7.533745e-03,  1.480249e-02,  9.998621e-01,  0.000000e+00],
                        [-9.999714e-01,  7.280733e-04,
                            7.523790e-03,  0.000000e+00],
                         [-6.166020e-04, -9.998902e-01,
                             1.480755e-02,  0.000000e+00],
                         [-4.069766e-03, -7.631618e-02, -2.717806e-01,  1.000000e+00]])

    root = '/media/HDD0/lzl/workspace/FCGF/data/kitti/dataset'
    DATA_FILES = {
        'train': './config/train_kitti.txt',
        'val': './config/val_kitti.txt',
        'test': './config/test_kitti.txt'
    }

    phases = ['test', 'train']
    for phase in phases:
        # seques for training or testing
        subset_names = open(DATA_FILES[phase]).read().split()

        for dirname in subset_names:
            drive_id = int(dirname)
            fnames = glob.glob(
                root + '/sequences/%02d/velodyne/*.bin' % drive_id)
            assert len(
                fnames) > 0, f"Make sure that the path {root} has data {dirname}"
            inames = sorted([int(os.path.split(fname)[-1][:-4])
                            for fname in fnames])

            all_odo = get_video_odometry(drive_id, return_all=True)
            all_pos = np.array([odometry_to_positions(odo)
                               for odo in all_odo])  # all_pos: T_w_cam0
            Ts = all_pos[:, :3, 3]
            pdist = (Ts.reshape(1, -1, 3) - Ts.reshape(-1, 1, 3))**2
            pdist = np.sqrt(pdist.sum(-1))
            valid_pairs = pdist > MIN_DIST
            curr_time = inames[0]
            while curr_time in inames:
                # Find the min index
                next_time = np.where(
                    valid_pairs[curr_time][curr_time:curr_time + 100])[0]
                if len(next_time) == 0:
                    curr_time += 1
                else:
                    # Follow https://github.com/yewzijian/3DFeatNet/blob/master/scripts_data_processing/kitti/process_kitti_data.m#L44
                    next_time = next_time[0] + curr_time - 1

                if next_time in inames:
                    files.append((drive_id, curr_time, next_time))
                    curr_time = next_time + 1

        # Remove problematic sequence
        for item in [(8, 15, 58),]:
            if item in files:
                files.pop(files.index(item))

        ######################################################################################
        num_feats = 1
        model_n_out = 32
        bn_momentum = 0.05
        normalize_feature = True
        conv1_kernel_size = 5
        model_checkpoint = '/media/HDD0/lzl/workspace/FCGF/checkpoints/KITTI-32-v0.3-ResUNetBN2C-conv1-5-nout32.pth'
        out_folder = '/media/HDD0/lzl/workspace/FCGF/data/KITTI_Feature_32'

        # load the trained model
        checkpoint = torch.load(model_checkpoint)
        Model = load_model('ResUNetBN2C')
        model = Model(
            num_feats,
            model_n_out,
            bn_momentum=bn_momentum,
            normalize_feature=normalize_feature,
            conv1_kernel_size=conv1_kernel_size)

        checkpoint = torch.load(model_checkpoint)
        model.load_state_dict(checkpoint['state_dict'])
        model = model.to(device)
        model.eval()

        for idx in tqdm(range(len(files))):
            result = {}

            drive = files[idx][0]
            t0, t1 = files[idx][1], files[idx][2]
            all_odometry = get_video_odometry(drive, [t0, t1])
            positions = [odometry_to_positions(
                odometry) for odometry in all_odometry]  # pose: 4*4
            fname0 = _get_velodyne_fn(drive, t0)
            fname1 = _get_velodyne_fn(drive, t1)

            # XYZ and reflectance
            xyzr0 = np.fromfile(fname0, dtype=np.float32).reshape(-1, 4)
            xyzr1 = np.fromfile(fname1, dtype=np.float32).reshape(-1, 4)
            # XYZ
            xyz0 = xyzr0[:, :3]
            xyz1 = xyzr1[:, :3]

            key = '%d_%d_%d' % (drive, t0, t1)
            # get pose_gt
            if phase == 'test':
                _, sel0 = ME.utils.sparse_quantize(
                    xyz0 / 0.05, return_index=True)
                _, sel1 = ME.utils.sparse_quantize(
                    xyz1 / 0.05, return_index=True)

                M = (velo2cam @ positions[0].T @ np.linalg.inv(positions[1].T)
                     @ np.linalg.inv(velo2cam)).T
                xyz0_t = apply_transform(xyz0[sel0], M)
                pcd0 = make_open3d_point_cloud(xyz0_t)
                pcd1 = make_open3d_point_cloud(xyz1[sel1])
                reg = o3d.pipelines.registration.registration_icp(
                    pcd0, pcd1, 0.2, np.eye(4),
                    o3d.pipelines.registration.TransformationEstimationPointToPoint(),
                    o3d.pipelines.registration.ICPConvergenceCriteria(max_iteration=200))
                pcd0.transform(reg.transformation)
                trans = M @ reg.transformation
                result['pose_gt'] = trans

            # Get feature
            feats_0, feats_1 = [], []
            feats_0.append(np.ones((len(xyz0), 1)))
            feats_0 = np.hstack(feats_0)
            feats_1.append(np.ones((len(xyz1), 1)))
            feats_1 = np.hstack(feats_1)

            with torch.no_grad():
                coords_0 = np.floor(xyz0 / 0.3)
                coords_0, inds_0 = ME.utils.sparse_quantize(
                    coords_0, return_index=True)
                coords_0 = ME.utils.batched_coordinates([coords_0])
                return_coords_0 = xyz0[inds_0]
                feats_0 = feats_0[inds_0]
                # Make point clouds using voxelized points
                pcd0 = make_open3d_point_cloud(xyz0[inds_0])
                feats_0 = torch.tensor(feats_0, dtype=torch.float32)
                coords_0 = torch.tensor(coords_0, dtype=torch.int32)
                stensor_0 = ME.SparseTensor(
                    feats_0, coordinates=coords_0, device=device)
                feat0 = model(stensor_0)

                coords_1 = np.floor(xyz1 / 0.3)
                coords_1, inds_1 = ME.utils.sparse_quantize(
                    coords_1, return_index=True)
                coords_1 = ME.utils.batched_coordinates([coords_1])
                return_coords_1 = xyz1[inds_1]
                feats_1 = feats_1[inds_1]
                # Make point clouds using voxelized points
                pcd1 = make_open3d_point_cloud(xyz1[inds_1])
                feats_1 = torch.tensor(feats_1, dtype=torch.float32)
                coords_1 = torch.tensor(coords_1, dtype=torch.int32)
                stensor_1 = ME.SparseTensor(
                    feats_1, coordinates=coords_1, device=device)
                feat1 = model(stensor_1)

            # # get predicted relative pose
            # feat0_ = make_open3d_feature(feat0.F.detach(), 32, feat0.shape[0])
            # feat1_ = make_open3d_feature(feat1.F.detach(), 32, feat1.shape[0])
            # distance_threshold = 0.3
            # ransac_result = o3d.pipelines.registration.registration_ransac_based_on_feature_matching(
            #     pcd0, pcd1, feat0_, feat1_, True, distance_threshold,
            #     o3d.pipelines.registration.TransformationEstimationPointToPoint(False), 4, [
            #         o3d.pipelines.registration.CorrespondenceCheckerBasedOnEdgeLength(
            #             0.9),
            #         o3d.pipelines.registration.CorrespondenceCheckerBasedOnDistance(
            #             distance_threshold)
            #     ], o3d.pipelines.registration.RANSACConvergenceCriteria(4000000, 10000))
            # T_ransac = torch.from_numpy(
            #     ransac_result.transformation.astype(np.float32)) # T_ransac: predicted pose

            result['seq_file0_file1'] = key
            result['xyz_0'] = return_coords_0
            result['xyz_1'] = return_coords_1
            result['feat_0'] = feat0.F.detach().cpu().numpy()
            result['feat_1'] = feat1.F.detach().cpu().numpy()
            # result['predict_pose'] = T_ransac.detach().cpu().numpy()

            # output
            out_path = os.path.join(out_folder, '%s_kitti' % phase)
            if not os.path.exists(out_path):
                os.makedirs(out_path)

            pcd_path = os.path.join(out_path, '%s.pkl' % idx)
            with open(pcd_path, 'wb') as f:
                pickle.dump(result, f, pickle.HIGHEST_PROTOCOL)
