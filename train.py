import re, gc
import torch.optim as optim
from matplotlib import pyplot as plt
from torch.optim import lr_scheduler
from torch.utils.data import Dataset
import time
import os
from os import path
from utils import data_loading_funcs as load_func
import SimpleITK as sitk
import ours_fusion as gens
from datetime import datetime
import argparse
import random
import numpy as np
from torch import nn
import torch
from utils.lossl1 import My_loss
import wandb
from sklearn.svm import SVR
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

random.seed(4)
# 设置NumPy的随机种子
np.random.seed(4)
# 设置PyTorch的随机种子
torch.manual_seed(4)
torch.cuda.manual_seed(4)
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = False


################
print(torch.__version__)
torch.cuda.empty_cache()
gc.collect()
torch.cuda.empty_cache()
desc = 'Training registration generator'
parser = argparse.ArgumentParser(description=desc)
parser.add_argument('-i', '--init_mode',
                    type=str,
                    help="mode of training with different transformation matrics",
                    # default='load')
                    default="load")


parser.add_argument('-l', '--learning_rate',
                    type=float,
                    help='Learning rate',
                    default=0.001)  # we used 0.001

parser.add_argument('-d', '--device_no',
                    type=int,
                    choices=[0, 1, 2, 3, 4, 5, 6, 7],
                    help='GPU device number [0-7]nvid',
                    default=1)

parser.add_argument('-e', '--epochs',
                    type=int,
                    help='number of training epochs',
                    default=300)  # we used 300 on our dataset

parser.add_argument('-n', '--network_type',
                    type=str,
                    help='choose different network architectures',
                    default='AttentionReg')
# default='FeatureReg')

parser.add_argument('-info', '--information',
                    type=str,
                    help='information of this round of experiment',
                    default='None')
parser.add_argument('-batch', '--batch_size',
                    type=int,
                    help='batch size of this round of experiment',
                    default=3)

net = 'Generator'

# we used 8 or 16 in our experiments

current_epoch = 0
args = parser.parse_args()
batch_size = args.batch_size
print('batch size = ', batch_size)
device_no = args.device_no
epochs = args.epochs
device = torch.device("cuda:{}".format(device_no))
print("device", device)


def filename_list(dir):
    images = []
    dir = os.path.expanduser(dir)
    # print('dir {}'.format(dir))
    for filename in os.listdir(dir):
        # print(filename)
        file_path = path.join(dir, filename)
        images.append(file_path)
        # print(file_path)
    # print(images)
    return images


def normalize_volume(input_volume):
    # print('input_volume shape {}'.format(input_volume.shape))
    mean = np.mean(input_volume)
    std = np.std(input_volume)

    normalized_volume = (input_volume - mean) / std
    # print('normalized shape {}'.format(normalized_volume.shape))
    # time.sleep(30)
    return normalized_volume


def scale_volume(input_volume, upper_bound=255, lower_bound=0):
    max_value = np.max(input_volume)
    min_value = np.min(input_volume)

    k = (upper_bound - lower_bound) / (max_value - min_value)
    scaled_volume = k * (input_volume - min_value) + lower_bound
    # print('min of scaled {}'.format(np.min(scaled_volume)))
    # print('max of scaled {}'.format(np.max(scaled_volume)))
    return scaled_volume


class MR_TRUS_4D(Dataset):

    def __init__(self, root_dir, initialization):
        """
        """
        samples = filename_list(root_dir)

        """list with all samples"""
        if root_dir[-3:] == 'val':
            self.status = 'val'
        else:
            self.status = 'train'
        self.samples = samples
        self.initialization = initialization

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        """
        :param idx:
        :return:
        """
        case_folder = self.samples[idx]
        case_id = re.findall(r"\d+", case_folder)
        # number = list(map(int, case_id))
        case_id = case_id[0]
        # print(case_id)

        index = int(case_id)

        ''' Load ground-truth registration '''

        gt_trans_fn = path.join('us_trans_final', 'gt.txt')
        # gt_trans_fn = path.join('sample_origin/gt.txt')

        gt_mat = np.loadtxt(gt_trans_fn)

        """generated random purtabation"""
        if self.initialization == 'load':
            # To randomly generate the transformation matrices
            base_mat = np.loadtxt('{}/initialization_{}.txt'.format(case_folder, case_id))

        elif self.initialization == 'random_uniform':

            base_mat, params_rand = generate_random_transform(gt_mat)
            base_mat = load_func.construct_matrix_degree(params=params_rand,
                                                         initial_transform=gt_mat)

        else:
            print('!' * 10 + ' Initialization mode <{}> not supported!'.format(self.initialization))
            return

        """loading MR and US images. In our experiments, we read images from mhd files and resample them with MR segmentation."""
        sample4D = np.zeros((2, 32, 96, 96), dtype=np.ubyte)

        sample4D[0, :, :, :] = np.load(path.join(case_folder, 'CT_{}.npy'.format(case_id)))
        # sample4D[0, :, :, :] = np.load(path.join(case_folder, 'MR_{}.npy'.format(case_id)))
        sample4D[1, :, :, :] = np.load(path.join(case_folder, 'US_{}.npy'.format(case_id)))
        sample4D = normalize_volume(sample4D)

        mat_diff = gt_mat.dot(np.linalg.inv(base_mat))
        target = load_func.decompose_matrix_degree(mat_diff)

        return sample4D, target, index, base_mat

#

# ----- #
def _get_random_value(r, center, hasSign):
    randNumber = random.random() * r + center

    if hasSign:
        sign = random.random() > 0.5
        if sign == False:
            randNumber *= -1

    return randNumber


# ----- #
def get_array_from_itk_matrix(itk_mat):
    mat = np.reshape(np.asarray(itk_mat), (3, 3))
    return mat


# ----- #
def create_transform(aX, aY, aZ, tX, tY, tZ, mat_base=None):
    if mat_base is None:
        mat_base = np.identity(3)

    t_all = np.asarray((tX, tY, tZ))

    # Get the transform
    rotX = sitk.VersorTransform((1, 0, 0), aX / 180.0 * np.pi)
    matX = get_array_from_itk_matrix(rotX.GetMatrix())
    #
    rotY = sitk.VersorTransform((0, 1, 0), aY / 180.0 * np.pi)
    matY = get_array_from_itk_matrix(rotY.GetMatrix())
    #
    rotZ = sitk.VersorTransform((0, 0, 1), aZ / 180.0 * np.pi)
    matZ = get_array_from_itk_matrix(rotZ.GetMatrix())

    # Apply all the rotations
    mat_all = matX.dot(matY.dot(matZ.dot(mat_base[:3, :3])))

    return mat_all, t_all


def generate_random_transform(base_trans_mat4x4=None):
    if base_trans_mat4x4 is None:
        base_trans_mat4x4 = np.identity(4)

    # Get random rotation and translation
    # The hard coded values are based on the statistical analysis of
    # euler_angle = 13 * np.pi / 180

    signed = True

    euler_angle = 5.0
    angleX = _get_random_value(euler_angle, 0, signed)
    angleY = _get_random_value(euler_angle, 0, signed)
    angleZ = _get_random_value(euler_angle, 0, signed)

    translation_range = 6.0
    tX = _get_random_value(translation_range, 0, signed)
    tY = _get_random_value(translation_range, 0, signed)
    tZ = _get_random_value(translation_range, 0, signed)

    parameters = np.asarray([tX, tY, tZ, angleX, angleY, angleZ])
    arrTrans = load_func.construct_matrix_degree(parameters,
                                                 initial_transform=base_trans_mat4x4)

    return arrTrans, parameters


def train_model(model, criterion, optimizer, scheduler, fn_save, num_epochs=25):
    since = time.time()

    loss_train = []
    loss_val = []
    lowest_loss = 2000

    tv_hist = {'train': [], 'val': []}

    for epoch in range(num_epochs):
        global current_epoch
        current_epoch = epoch + 1

        # Each epoch has a training and validation phase
        for phase in ['train', 'val']:
            # print('Network is in {}...'.format(phase))

            if phase == 'train':
                scheduler.step()
                model.train()  # Set model to training mode
            else:
                model.eval()  # Set model to evaluate mode

            running_loss = 0.0

            # Iterate over data.
            for inputs, labels, img_id, base_mat in dataloaders[phase]:

                labels = labels.type(torch.FloatTensor)
                inputs = inputs.type(torch.FloatTensor)

                labels = labels.to(device)
                inputs = inputs.to(device)


                labels.require_grad = True

                optimizer.zero_grad()


                with torch.set_grad_enabled(phase == 'train'):


                    outputs = model(inputs).to(device)

                    '''Weighted MSE loss function'''

                    loss = criterion(outputs, labels)

                    # print(loss)

                    # backward + optimize only if in training phase
                    if phase == 'train':

                        loss.backward()
                        optimizer.step()
                        # for name, parms in model.named_parameters():
                            # print('-->grad_requirs:', parms.requires_grad)
                            # print('-->grad_value:', parms.grad)
                            # print("===")

                running_loss += loss.data.mean() * inputs.size(0)


            epoch_loss = running_loss / dataset_sizes[phase]

            tv_hist[phase].append(epoch_loss)

            if phase == 'val' and epoch_loss <= lowest_loss: #loss version

                lowest_loss = epoch_loss
                # lowest_TRE = epoch_TRE
                best_ep = epoch
                torch.save(model.state_dict(), fn_save)
                print('**** best model updated with Loss={:.4f} ****'.format(lowest_loss))

        for param_group in optimizer.param_groups:
            print('learning rate: ',param_group['lr'])
            print('ep {}/{}: T-loss: {:.4f}, V-loss: {:.4f}'.format(epoch + 1, num_epochs,tv_hist['train'][-1],tv_hist['val'][-1]))
            loss_train.append(tv_hist['train'][-1].cpu().numpy())
            loss_val.append(tv_hist['val'][-1].cpu().numpy())
            wandb.log({"loss_train": tv_hist['train'][-1].cpu().numpy(), "loss_val":tv_hist['val'][-1].cpu().numpy()})


    time_elapsed = time.time() - since
    print('*' * 10 + 'Training complete in {:.0f}m {:.0f}s'.format(
        time_elapsed // 60, time_elapsed % 60))
    print('*' * 10 + 'Lowest val loss: {:4f} at epoch {}'.format(lowest_loss, best_ep))
    print()
    x = list(range(1, num_epochs+1))
    plt.plot(x, loss_train, 'ro-')
    plt.title('loss_train ')
    plt.show()

    plt.plot(x, loss_val, 'ro-')
    plt.title('loss_val ')
    plt.show()

    return tv_hist

if __name__ == '__main__':

    wandb.init(config=args, project='summodel')

    data_dir = 'split'
    # data_dir = 'right'
    results_dir = 'results'

    init_mode = args.init_mode
    network_type = args.network_type
    print('Transform initialization mode: {}'.format(init_mode))

    image_datasets = {x: MR_TRUS_4D(os.path.join(data_dir, x), init_mode)
                      for x in ['train', 'val']}

    dataloaders = {x: torch.utils.data.DataLoader(image_datasets[x],
                                                  batch_size=batch_size,
                                                  shuffle=True,
                                                  num_workers=0)
                   for x in ['train', 'val']}

    dataset_sizes = {x: len(image_datasets[x]) for x in ['train', 'val']}

    print('Number of training samples: {}'.format(dataset_sizes['train']))
    print('Number of validation samples: {}'.format(dataset_sizes['val']))

    if network_type == 'AttentionReg':
        model_ft = gens.AttentionReg()

    # model_ft = nn.DataParallel(model_ft)  #这段代码使用了PyTorch的nn.DataParallel模块将model_ft变量包装为一个数据并行模型 j
    # model_ft.cuda()
    model_ft = model_ft.to(device)
    lr = args.learning_rate
    print('Learning rate = {}'.format(lr))
    # optimizer = optim.AdamW(model_ft.parameters(), lr=lr)  
    optimizer = optim.Adam(model_ft.parameters(), lr=lr) 
    # optimizer = optim.Adagrad(model_ft.parameters(), lr=lr) 
    # optimizer = optim.ASGD(model_ft.parameters(), lr=lr)  

    # criterion = nn.MSELoss()
    criterion = My_loss()


    # this is the learning rate that worked best for us. The network is pretty sensitive to learning rate changes.
    exp_lr_scheduler = lr_scheduler.MultiStepLR(optimizer, milestones=[120, 170, 250], gamma=0.3)

    now = datetime.now()
    now_str = now.strftime('%m%d-%H%M%S')
    print(now_str)

    # Ready to start
    fn_best_model = path.join('results/', 'NEW1_ours_{}_{}_{}_model.pth'.format(network_type, now_str, init_mode))
    print('Start training...')
    print('This model is <summodel_{}_{}.pth>'.format(now_str, init_mode))
    txt_path = path.join('results/', 'training_progress_{}_{}_{}.txt'.format(network_type, now_str, init_mode))

    # count the parameters
    model_parameters = filter(lambda p: p.requires_grad, model_ft.parameters())
    params = sum([np.prod(p.size()) for p in model_parameters])
    print('params {}'.format(params))

    hist_ft = train_model(model_ft,
                          criterion,
                          optimizer,
                          exp_lr_scheduler,
                          fn_best_model,
                          num_epochs=epochs)

    fn_hist = os.path.join('results/', 'hist_{}_{}_{}.npy'.format(net, now_str, init_mode))
    np.save(fn_hist, hist_ft)

    now = datetime.now()
    now_stamp = now.strftime('%Y-%m-%d %H:%M:%S')
    print('#' * 15 + ' Training {} completed at {} started at {}'.format(init_mode, now_stamp, now_str) + '#' * 15)
