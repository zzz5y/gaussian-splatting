#
# Copyright (C) 2023, Inria
# GRAPHDECO research group, https://team.inria.fr/graphdeco
# All rights reserved.
#
# This software is free for non-commercial, research and evaluation use 
# under the terms of the LICENSE.md file.
#
# For inquiries contact  george.drettakis@inria.fr
#

import os
import sys
from PIL import Image
from typing import NamedTuple
from scene.colmap_loader import read_extrinsics_text, read_intrinsics_text, qvec2rotmat, \
    read_extrinsics_binary, read_intrinsics_binary, read_points3D_binary, read_points3D_text
from utils.graphics_utils import getWorld2View2, focal2fov, fov2focal
import numpy as np
import json
from pathlib import Path
from plyfile import PlyData, PlyElement
from utils.sh_utils import SH2RGB
from scene.gaussian_model import BasicPointCloud
import os,imageio
import imageio.v2 as imageio

class CameraInfo(NamedTuple):
    uid: int
    R: np.array
    T: np.array
    FovY: np.array
    FovX: np.array
    image: np.array
    image_path: str
    image_name: str
    width: int
    height: int

class SceneInfo(NamedTuple):
    point_cloud: BasicPointCloud
    train_cameras: list
    test_cameras: list
    nerf_normalization: dict
    ply_path: str

def getNerfppNorm(cam_info):
    def get_center_and_diag(cam_centers):
        cam_centers = np.hstack(cam_centers)
        avg_cam_center = np.mean(cam_centers, axis=1, keepdims=True)
        center = avg_cam_center
        dist = np.linalg.norm(cam_centers - center, axis=0, keepdims=True)
        diagonal = np.max(dist)
        return center.flatten(), diagonal

    cam_centers = []

    for cam in cam_info:
        W2C = getWorld2View2(cam.R, cam.T)
        C2W = np.linalg.inv(W2C)
        cam_centers.append(C2W[:3, 3:4])

    center, diagonal = get_center_and_diag(cam_centers)
    radius = diagonal * 1.1

    translate = -center

    return {"translate": translate, "radius": radius}

def readColmapCameras(cam_extrinsics, cam_intrinsics, images_folder):
    cam_infos = []
    for idx, key in enumerate(cam_extrinsics):
        sys.stdout.write('\r')
        # the exact output you're looking for:
        sys.stdout.write("Reading camera {}/{}".format(idx+1, len(cam_extrinsics)))
        sys.stdout.flush()

        extr = cam_extrinsics[key]
        intr = cam_intrinsics[extr.camera_id]
        height = intr.height
        width = intr.width

        uid = intr.id
        R = np.transpose(qvec2rotmat(extr.qvec))
        T = np.array(extr.tvec)

        if intr.model=="SIMPLE_PINHOLE":
            focal_length_x = intr.params[0]
            FovY = focal2fov(focal_length_x, height)
            FovX = focal2fov(focal_length_x, width)
        elif intr.model=="PINHOLE":
            focal_length_x = intr.params[0]
            focal_length_y = intr.params[1]
            FovY = focal2fov(focal_length_y, height)
            FovX = focal2fov(focal_length_x, width)
        else:
            assert False, "Colmap camera model not handled: only undistorted datasets (PINHOLE or SIMPLE_PINHOLE cameras) supported!"

        image_path = os.path.join(images_folder, os.path.basename(extr.name))
        image_name = os.path.basename(image_path).split(".")[0]
        image = Image.open(image_path)

        cam_info = CameraInfo(uid=uid, R=R, T=T, FovY=FovY, FovX=FovX, image=image,
                              image_path=image_path, image_name=image_name, width=width, height=height)
        cam_infos.append(cam_info)
    sys.stdout.write('\n')
    return cam_infos

def fetchPly(path,return_normals=True):
    plydata = PlyData.read(path)
    vertices = plydata['vertex']
    positions = np.vstack([vertices['x'], vertices['y'], vertices['z']]).T
    colors = np.vstack([vertices['red'], vertices['green'], vertices['blue']]).T / 255.0
    if return_normals:
        normals = np.vstack([vertices['nx'], vertices['ny'], vertices['nz']]).T
    else:
        normals = None
    return BasicPointCloud(points=positions, colors=colors, normals=normals)

def storePly(path, xyz, rgb):
    # Define the dtype for the structured array
    dtype = [('x', 'f4'), ('y', 'f4'), ('z', 'f4'),
            ('nx', 'f4'), ('ny', 'f4'), ('nz', 'f4'),
            ('red', 'u1'), ('green', 'u1'), ('blue', 'u1')]
    
    normals = np.zeros_like(xyz)

    elements = np.empty(xyz.shape[0], dtype=dtype)
    attributes = np.concatenate((xyz, normals, rgb), axis=1)
    elements[:] = list(map(tuple, attributes))

    # Create the PlyData object and write to file
    vertex_element = PlyElement.describe(elements, 'vertex')
    ply_data = PlyData([vertex_element])
    ply_data.write(path)

def readColmapSceneInfo(path, images, eval, llffhold=8):
    try:
        cameras_extrinsic_file = os.path.join(path, "sparse/0", "images.bin")
        cameras_intrinsic_file = os.path.join(path, "sparse/0", "cameras.bin")
        cam_extrinsics = read_extrinsics_binary(cameras_extrinsic_file)
        cam_intrinsics = read_intrinsics_binary(cameras_intrinsic_file)
    except:
        cameras_extrinsic_file = os.path.join(path, "sparse/0", "images.txt")
        cameras_intrinsic_file = os.path.join(path, "sparse/0", "cameras.txt")
        cam_extrinsics = read_extrinsics_text(cameras_extrinsic_file)
        cam_intrinsics = read_intrinsics_text(cameras_intrinsic_file)

    reading_dir = "images" if images == None else images
    cam_infos_unsorted = readColmapCameras(cam_extrinsics=cam_extrinsics, cam_intrinsics=cam_intrinsics, images_folder=os.path.join(path, reading_dir))
    cam_infos = sorted(cam_infos_unsorted.copy(), key = lambda x : x.image_name)

    if eval:
        train_cam_infos = [c for idx, c in enumerate(cam_infos) if idx % llffhold != 0]
        test_cam_infos = [c for idx, c in enumerate(cam_infos) if idx % llffhold == 0]
    else:
        train_cam_infos = cam_infos
        test_cam_infos = []

    nerf_normalization = getNerfppNorm(train_cam_infos)

    ply_path = os.path.join(path, "sparse/0/points3D.ply")
    bin_path = os.path.join(path, "sparse/0/points3D.bin")
    txt_path = os.path.join(path, "sparse/0/points3D.txt")
    if not os.path.exists(ply_path):
        print("Converting point3d.bin to .ply, will happen only the first time you open the scene.")
        try:
            xyz, rgb, _ = read_points3D_binary(bin_path)
        except:
            xyz, rgb, _ = read_points3D_text(txt_path)
        storePly(ply_path, xyz, rgb)
    try:
        pcd = fetchPly(ply_path)
    except:
        pcd = None

    scene_info = SceneInfo(point_cloud=pcd,
                           train_cameras=train_cam_infos,
                           test_cameras=test_cam_infos,
                           nerf_normalization=nerf_normalization,
                           ply_path=ply_path)
    return scene_info

def readCamerasFromTransforms(path, transformsfile, white_background, extension=".png"):
    cam_infos = []

    with open(os.path.join(path, transformsfile)) as json_file:
        contents = json.load(json_file)
        fovx = contents["camera_angle_x"]

        frames = contents["frames"]
        for idx, frame in enumerate(frames):
            cam_name = os.path.join(path, frame["file_path"] + extension)

            # NeRF 'transform_matrix' is a camera-to-world transform
            c2w = np.array(frame["transform_matrix"])
            # change from OpenGL/Blender camera axes (Y up, Z back) to COLMAP (Y down, Z forward)
            c2w[:3, 1:3] *= -1

            # get the world-to-camera transform and set R, T
            w2c = np.linalg.inv(c2w)
            R = np.transpose(w2c[:3,:3])  # R is stored transposed due to 'glm' in CUDA code
            T = w2c[:3, 3]

            image_path = os.path.join(path, cam_name)
            image_name = Path(cam_name).stem
            image = Image.open(image_path)

            im_data = np.array(image.convert("RGBA"))

            bg = np.array([1,1,1]) if white_background else np.array([0, 0, 0])

            norm_data = im_data / 255.0
            arr = norm_data[:,:,:3] * norm_data[:, :, 3:4] + bg * (1 - norm_data[:, :, 3:4])
            image = Image.fromarray(np.array(arr*255.0, dtype=np.byte), "RGB")

            fovy = focal2fov(fov2focal(fovx, image.size[0]), image.size[1])
            FovY = fovy 
            FovX = fovx

            cam_infos.append(CameraInfo(uid=idx, R=R, T=T, FovY=FovY, FovX=FovX, image=image,
                            image_path=image_path, image_name=image_name, width=image.size[0], height=image.size[1]))
            
    return cam_infos

def readNerfSyntheticInfo(path, white_background, eval, extension=".png"):
    print("Reading Training Transforms")
    train_cam_infos = readCamerasFromTransforms(path, "transforms_train.json", white_background, extension)
    print("Reading Test Transforms")
    test_cam_infos = readCamerasFromTransforms(path, "transforms_test.json", white_background, extension)
    
    if not eval:
        train_cam_infos.extend(test_cam_infos)
        test_cam_infos = []

    nerf_normalization = getNerfppNorm(train_cam_infos)

    ply_path = os.path.join(path, "points3d.ply")
    if not os.path.exists(ply_path):
        # Since this data set has no colmap data, we start with random points
        num_pts = 100_000
        print(f"Generating random point cloud ({num_pts})...")
        
        # We create random points inside the bounds of the synthetic Blender scenes
        xyz = np.random.random((num_pts, 3)) * 2.6 - 1.3
        shs = np.random.random((num_pts, 3)) / 255.0
        pcd = BasicPointCloud(points=xyz, colors=SH2RGB(shs), normals=np.zeros((num_pts, 3)))

        storePly(ply_path, xyz, SH2RGB(shs) * 255)
    try:
        pcd = fetchPly(ply_path)
    except:
        pcd = None

    scene_info = SceneInfo(point_cloud=pcd,
                           train_cameras=train_cam_infos,
                           test_cameras=test_cam_infos,
                           nerf_normalization=nerf_normalization,
                           ply_path=ply_path)
    return scene_info


def readKitti360Info(datadir, white_background=False, eval=False, factor=8):
    """
    Read and process KITTI-360 dataset into a format compatible with the 3DGS framework.

    Args:
        datadir (str): Directory containing the KITTI-360 data.
        white_background (bool): If True, sets background to white.
        eval (bool): If True, separates cameras into training and testing sets.
        factor (int): Downsampling factor for images.

    Returns:
        SceneInfo: A NamedTuple containing point cloud, train/test camera info, nerf normalization, and ply path.
    """

    # Load the data
    print("Loading KITTI-360 data...")
    poses, imgs, render_pose, img_shape, i_test = load_kitti360_data(datadir, factor)
    height, width, focal = img_shape

    # Initialize camera information
    cam_infos = []
    for idx, (pose, img) in enumerate(zip(poses, imgs)):
        # Convert pose to rotation (R) and translation (T)
        w2c = pose
        R = np.transpose(w2c[:3, :3])  # R is stored transposed due to 'glm' in CUDA code
        T = w2c[:3, 3]

        # Load and preprocess the image
        image = Image.fromarray((img * 255).astype(np.uint8))

        # Set background color
        bg = np.array([1, 1, 1]) if white_background else np.array([0, 0, 0])
        norm_data = np.array(image.convert("RGBA")) / 255.0
        arr = norm_data[:, :, :3] * norm_data[:, :, 3:4] + bg * (1 - norm_data[:, :, 3:4])
        image = Image.fromarray((arr * 255.0).astype(np.uint8), "RGB")

        # Calculate FOV from focal length and image dimensions
        fovx = focal2fov(focal, width)
        fovy = focal2fov(focal, height)

        cam_info = CameraInfo(
            uid=idx,
            R=R,
            T=T,
            FovY=fovy,
            FovX=fovx,
            image=image,
            image_path="",
            image_name=f"kitti360_{idx:04d}",
            width=width,
            height=height
        )
        cam_infos.append(cam_info)

    # Separate train and test cameras if evaluation mode is enabled
    if eval:
        train_cam_infos = [cam for i, cam in enumerate(cam_infos) if i not in i_test]
        test_cam_infos = [cam for i, cam in enumerate(cam_infos) if i in i_test]
    else:
        train_cam_infos = cam_infos
        test_cam_infos = []

    # Get normalization for NeRF++ training
    nerf_normalization = getNerfppNorm(train_cam_infos)

    # Generate random point cloud since KITTI-360 data doesn't include one by default
    ply_path = os.path.join(datadir, "points3d.ply")
    if not os.path.exists(ply_path):
        num_pts = 100_000
        print(f"Generating random point cloud ({num_pts})...")
        xyz = np.random.random((num_pts, 3)) * 2.6 - 1.3
        shs = np.random.random((num_pts, 3)) / 255.0
        pcd = BasicPointCloud(points=xyz, colors=SH2RGB(shs), normals=np.zeros((num_pts, 3)))
        storePly(ply_path, xyz, SH2RGB(shs) * 255)
    try:
        pcd = fetchPly(ply_path)
        if os.path.exists("/media/ry/483BED215A2D2EBA/KITTI-360/colmap_points/points3D.ply"):
            colmap_pcd = fetchPly(ply_path,return_normals=False)
            pcd=colmap_pcd
            #pcd_return=merge_point_clouds(pcd,colmap_pcd)
            pcd_return=pcd
        else:
            pcd_return=pcd
    except:
        pcd_return = None

    # Return SceneInfo
    scene_info = SceneInfo(
        point_cloud=pcd_return,
        train_cameras=train_cam_infos,
        test_cameras=test_cam_infos,
        nerf_normalization=nerf_normalization,
        ply_path=ply_path
    )

    return scene_info

'''
Output: images, poses, bds, render_pose, itest;

poses (是指的 c2w 的pose)
'''

def load_kitti360_data(datadir, factor=8):
    #poses, imgs, K, i_test =_load_data(datadir)
    poses, imgs, K, i_test = _load_data_single(datadir)
    H,W = imgs.shape[1:3]
    focal = K[0][0]

    ## 设第一张相机的Pose 是单位矩阵，对其他相机的Pose 需要进行调整为相对于第一帧的Pose 相对位姿
    poses = Normailize_T(poses)   ## 对于 平移translation 进行归一化

    render_pose = np.stack(poses[i] for i in i_test)

    return poses,imgs,render_pose,[H,W,focal],i_test


def _load_data(datadir,end_iterion=424,sequence ='2013_05_28_drive_0000_sync'):
    '''Load intrinstic matrix'''
    intrinstic_file = os.path.join(os.path.join(datadir, 'calibration'), 'perspective.txt')
    with open(intrinstic_file) as f:
        lines = f.readlines()
        for line in lines:
            lineData = line.strip().split()
            if lineData[0] == 'P_rect_00:':
                K_00 = np.array(lineData[1:]).reshape(3,4).astype(np.float64)
            elif lineData[0] == 'P_rect_01:':
                K_01 = np.array(lineData[1:]).reshape(3,4).astype(np.float64)
            elif lineData[0] == 'R_rect_01:':
                R_rect_01 = np.eye(4)
                R_rect_01[:3,:3] = np.array(lineData[1:]).reshape(3,3).astype(np.float64)

    '''Load extrinstic matrix'''
    CamPose_00 = {}
    CamPose_01 = {}
    extrinstic_file = os.path.join(datadir,os.path.join('data_poses',sequence))
    cam2world_file_00 = os.path.join(extrinstic_file,'cam0_to_world.txt')
    pose_file = os.path.join(extrinstic_file,'poses.txt')


    ''' Camera_00  to world coordinate '''
    with open(cam2world_file_00,'r') as f:
        lines = f.readlines()
        for line in lines:
            lineData = list(map(float,line.strip().split()))
            CamPose_00[lineData[0]] = np.array(lineData[1:]).reshape(4,4)

    ''' Camera_01 to world coordiante '''
    CamToPose_01 = loadCameraToPose(os.path.join(os.path.join(datadir, 'calibration'),'calib_cam_to_pose.txt'))
    poses = np.loadtxt(pose_file)
    frames = poses[:, 0]
    poses = np.reshape(poses[:, 1:], [-1, 3, 4])
    for frame, pose in zip(frames, poses):
        pose = np.concatenate((pose, np.array([0., 0., 0., 1.]).reshape(1, 4)))
        pp = np.matmul(pose, CamToPose_01)
        CamPose_01[frame] = np.matmul(pp, np.linalg.inv(R_rect_01))



    ''' Load corrlected images camera 00--> index    camera 01----> index+1'''
    def imread(f):
        if f.endswith('png'):
            #return imageio.imread(f, ignoregamma=True)
            return imageio.imread(f, format="PNG-PIL", ignoregamma=True)
        else:
            #return imageio.imread(f)
            return imageio.imread(f, format="PNG-PIL", ignoregamma=True)
    imgae_dir = os.path.join(datadir,sequence)
    image_00 = os.path.join(imgae_dir,'image_00/data_rect')
    image_01 = os.path.join(imgae_dir,'image_01/data_rect')

    start_index = 3463
    #num = 8
    num = 262
    all_images = []
    all_poses = []

    for idx in range(start_index,start_index+num,1):
        ## read image_00
        image = imread(os.path.join(image_00,"{:010d}.png").format(idx))/255.0
        all_images.append(image)
        all_poses.append(CamPose_00[idx])

        ## read image_01
        image = imread(os.path.join(image_01, "{:010d}.png").format(idx))/255.0
        all_images.append(image)
        all_poses.append(CamPose_01[idx])
    #
    # imga_file = [os.path.join(imgae_dir,f"{'%010d'% idx}.png") for idx in imgs_idx ]  ##"010d“ 将 idx前面补成10位
    # # length = len(imga_file)
    # imgs = [imread(f)[...,:3]/255. for f in imga_file]
    # for i,idx in enumerate(imgs_idx):
    #     cv.imwrite(f"train/{'%010d'% idx}.png", imgs[i] * 255)

    imgs = np.stack(all_images,-1)
    imgs = np.moveaxis(imgs, -1, 0)
    c2w = np.stack(all_poses)

    '''Generate test file'''
    #i_test = np.array([4,10])

    # # 获取 all_images 的长度
    # n = len(all_images)
    #
    # # 创建一个包含所有索引的列表
    # indices = list(range(n))
    #
    # # 从索引列表中随机选择 30 个不重复的索引
    # selected_indices = random.sample(indices, min(30, n))
    #
    # # 根据选中的索引获取元素
    # i_test = sorted(selected_indices)

    #i_test =[1, 3, 5, 7, 9, 11, 13, 15, 17, 19, 21, 23, 25, 27, 29, 31, 33, 35, 37, 39, 41, 43, 45, 47, 49, 51, 53, 55, 57, 59]
    i_test = [i for i in range(262) if i % 3 == 0]
    return c2w,imgs, K_00,i_test


# 处理法向量的拼接函数
def merge_point_clouds(pcd1, pcd2):
    # 获取第一个点云的点、颜色、法向量信息
    points1, colors1, normals1 = pcd1.points, pcd1.colors, pcd1.normals
    # 获取第二个点云的点、颜色、法向量信息
    points2, colors2, normals2 = pcd2.points, pcd2.colors, pcd2.normals

    # 拼接点和颜色信息
    merged_points = np.vstack([points1, points2])
    merged_colors = np.vstack([colors1, colors2])

    if normals1 is not None and normals2 is not None:
        # 如果两个点云都有法向量，直接拼接
        merged_normals = np.vstack([normals1, normals2])
    elif normals1 is not None:
        # 只有第一个点云有法向量，第二个点云的法向量设为零向量或空值
        zero_normals = np.zeros_like(points2)
        merged_normals = np.vstack([normals1, zero_normals])
    elif normals2 is not None:
        # 只有第二个点云有法向量，第一个点云的法向量设为零向量或空值
        zero_normals = np.zeros_like(points1)
        merged_normals = np.vstack([zero_normals, normals2])
    else:
        # 两个点云都没有法向量，法向量设为None
        merged_normals = None

    # 创建一个新的点云对象，并将拼接后的数据赋值给它
    merged_pcd = BasicPointCloud(points=merged_points, colors=merged_colors, normals=merged_normals)

    return merged_pcd


def _load_data_single(datadir, end_iterion=424, sequence='2013_05_28_drive_0000_sync'):
    '''Load intrinstic matrix'''
    intrinstic_file = os.path.join(os.path.join(datadir, 'calibration'), 'perspective.txt')
    with open(intrinstic_file) as f:
        lines = f.readlines()
        for line in lines:
            lineData = line.strip().split()
            if lineData[0] == 'P_rect_00:':
                K_00 = np.array(lineData[1:]).reshape(3, 4).astype(np.float64)

    '''Load extrinstic matrix'''
    CamPose_00 = {}
    extrinstic_file = os.path.join(datadir, os.path.join('data_poses', sequence))
    cam2world_file_00 = os.path.join(extrinstic_file, 'cam0_to_world.txt')

    ''' Camera_00  to world coordinate '''
    with open(cam2world_file_00, 'r') as f:
        lines = f.readlines()
        for line in lines:
            lineData = list(map(float, line.strip().split()))
            CamPose_00[lineData[0]] = np.array(lineData[1:]).reshape(4, 4)

    ''' Load images from camera 00 '''
    def imread(f):
        if f.endswith('png'):
            return imageio.imread(f, format="PNG-PIL", ignoregamma=True)
        else:
            return imageio.imread(f, format="PNG-PIL", ignoregamma=True)

    image_dir = os.path.join(datadir, sequence)
    image_00 = os.path.join(image_dir, 'image_00/data_rect')

    #start_index = 3353
    start_index = 3463
    num = 262
    all_images = []
    all_poses = []

    for idx in range(start_index, start_index + num, 1):
        # Read image_00
        image = imread(os.path.join(image_00, "{:010d}.png").format(idx)) / 255.0
        all_images.append(image)
        all_poses.append(CamPose_00[idx])

    imgs = np.stack(all_images, -1)
    imgs = np.moveaxis(imgs, -1, 0)
    c2w = np.stack(all_poses)

    '''Generate test file'''
    i_test = [i for i in range(262) if i % 2 == 0]
    return c2w, imgs, K_00, i_test

# def Normailize_T(poses):
#     for i,pose in enumerate(poses):
#         if i == 0:
#             inv_pose = np.linalg.inv(pose)
#             poses[i] = np.eye(4)
#         else:
#             #inv_pose = np.linalg.inv(pose)
#             poses[i] = np.dot(inv_pose,poses[i])
#
#     '''New Normalization '''
#     scale = poses[-1,2,3]
#     print(f"scale:{scale}\n")
#     for i in range(poses.shape[0]):
#         poses[i,:3,3] = poses[i,:3,3]/scale
#         print(poses[i])
#     return poses

def Normailize_T(poses):
    for i,pose in enumerate(poses):
        if i == 0:
            inv_pose = np.linalg.inv(pose)
            poses[i] = np.eye(4)
        else:
            poses[i] = np.dot(inv_pose, poses[i])

    '''New Normalization '''
    scale = poses[-1, 2, 3]
    print(f"scale: {scale}\n")
    for i in range(poses.shape[0]):
        poses[i, :3, 3] = poses[i, :3, 3] / scale
        print(poses[i])
    return poses


def loadCameraToPose(filename):
    # open file
    Tr = {}
    lastrow = np.array([0, 0, 0, 1]).reshape(1, 4)
    with open(filename, 'r') as f:
        lines = f.readlines()
        for line in lines:
            lineData = list(line.strip().split())
            if lineData[0] == 'image_01:':
                data = np.array(lineData[1:]).reshape(3,4).astype(np.float64)
                data = np.concatenate((data,lastrow), axis=0)
                Tr[lineData[0]] = data

    return Tr['image_01:']


sceneLoadTypeCallbacks = {
    "Colmap": readColmapSceneInfo,
    "Blender" : readNerfSyntheticInfo,
    "Kitti360":  readKitti360Info
}