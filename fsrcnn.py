import os
import torch.nn as nn
import torch.optim as optim
from torch.autograd import Variable
from base_networks import *
from torch.utils.data import DataLoader
from data import get_training_set, get_test_set
import utils
from logger import Logger
from torchvision.transforms import *


class Net(torch.nn.Module):
    def __init__(self, num_channels, scale_factor, d, s, m):
        super(Net, self).__init__()

        # Feature extraction
        self.first_part = ConvBlock(num_channels, d, 5, 1, 0, activation='prelu', norm=None)

        self.layers = []
        # Shrinking
        self.layers.append(ConvBlock(d, s, 1, 1, 0, activation='prelu', norm=None))
        # Non-linear Mapping
        for _ in range(m):
            self.layers.append(ConvBlock(s, s, 3, 1, 1, activation=None, norm=None))
        self.layers.append(nn.PReLU())
        # Expanding
        self.layers.append(ConvBlock(s, d, 1, 1, 0, activation='prelu', norm=None))

        self.mid_part = torch.nn.Sequential(*self.layers)

        # Deconvolution
        self.last_part = nn.ConvTranspose2d(d, num_channels, 9, scale_factor, 3, output_padding=1)
        # self.last_part = torch.nn.Sequential(
        #     Upsample2xBlock(d, d, upsample='rnc', activation=None, norm=None),
        #     Upsample2xBlock(d, num_channels, upsample='rnc', activation=None, norm=None)
        # )

    def forward(self, x):
        out = self.first_part(x)
        out = self.mid_part(out)
        out = self.last_part(out)
        return out

    def weight_init(self, mean=0.0, std=0.02):
        for m in self.modules():
            # utils.weights_init_normal(m, mean=mean, std=std)
            if isinstance(m, nn.Conv2d):
                m.weight.data.normal_(mean, std)
                if m.bias is not None:
                    m.bias.data.zero_()
            if isinstance(m, nn.ConvTranspose2d):
                m.weight.data.normal_(0.0, 0.0001)
                if m.bias is not None:
                    m.bias.data.zero_()


class FSRCNN(object):
    def __init__(self, args):
        # parameters
        self.model_name = args.model_name
        self.train_dataset = args.train_dataset
        self.test_dataset = args.test_dataset
        self.crop_size = args.crop_size
        self.num_threads = args.num_threads
        self.num_channels = args.num_channels
        self.scale_factor = args.scale_factor
        self.num_epochs = args.num_epochs
        self.save_epochs = args.save_epochs
        self.batch_size = args.batch_size
        self.test_batch_size = args.test_batch_size
        self.lr = args.lr
        self.data_dir = args.data_dir
        self.save_dir = args.save_dir
        self.gpu_mode = args.gpu_mode

    def load_dataset(self, dataset='train'):
        if self.num_channels == 1:
            is_gray = True
        else:
            is_gray = False

        if dataset == 'train':
            print('Loading train datasets...')
            train_set = get_training_set(self.data_dir, self.train_dataset, self.crop_size, self.scale_factor, is_gray=is_gray,
                                         normalize=False)
            return DataLoader(dataset=train_set, num_workers=self.num_threads, batch_size=self.batch_size,
                              shuffle=True)
        elif dataset == 'test':
            print('Loading test datasets...')
            test_set = get_test_set(self.data_dir, self.test_dataset, self.scale_factor, is_gray=is_gray,
                                    normalize=False)
            return DataLoader(dataset=test_set, num_workers=self.num_threads,
                              batch_size=self.test_batch_size,
                              shuffle=False)

    def train(self):
        # networks
        self.model = Net(num_channels=self.num_channels, scale_factor=self.scale_factor, d=56, s=12, m=4)

        # weigh initialization
        self.model.weight_init(mean=0.0, std=0.02)

        # optimizer
        self.momentum = 0.9
        self.optimizer = optim.SGD(self.model.parameters(), lr=self.lr, momentum=self.momentum)

        # loss function
        if self.gpu_mode:
            self.model.cuda()
            self.MSE_loss = nn.MSELoss().cuda()
        else:
            self.MSE_loss = nn.MSELoss()

        print('---------- Networks architecture -------------')
        utils.print_network(self.model)
        print('----------------------------------------------')

        # load dataset
        train_data_loader = self.load_dataset(dataset='train')
        test_data_loader = self.load_dataset(dataset='test')

        # set the logger
        log_dir = os.path.join(self.save_dir, 'logs')
        if not os.path.exists(log_dir):
            os.mkdir(log_dir)
        logger = Logger(log_dir)

        ################# Train #################
        print('Training is started.')
        avg_loss = []
        step = 0

        # test image
        test_input, test_target = test_data_loader.dataset.__getitem__(2)
        test_input = test_input.unsqueeze(0)
        test_target = test_target.unsqueeze(0)

        self.model.train()
        for epoch in range(self.num_epochs):

            epoch_loss = 0
            for iter, (input, target) in enumerate(train_data_loader):
                # input data (low resolution image)
                if self.gpu_mode:
                    x_ = Variable(utils.shave(target, border_size=2*self.scale_factor).cuda())
                    y_ = Variable(input.cuda())
                else:
                    x_ = Variable(utils.shave(target, border_size=2*self.scale_factor))
                    y_ = Variable(input)

                # update network
                self.optimizer.zero_grad()
                recon_image = self.model(y_)
                loss = self.MSE_loss(recon_image, x_)
                loss.backward()
                self.optimizer.step()

                # log
                epoch_loss += loss.data[0]
                print("Epoch: [%2d] [%4d/%4d] loss: %.8f" % ((epoch + 1), (iter + 1), len(train_data_loader), loss.data[0]))

                # tensorboard logging
                logger.scalar_summary('loss', loss.data[0], step + 1)
                step += 1

            # avg. loss per epoch
            avg_loss.append(epoch_loss / len(train_data_loader))

            # prediction
            recon_imgs = self.model(Variable(test_input.cuda()))
            recon_img = recon_imgs[0].cpu().data
            gt_img = utils.shave(test_target[0], border_size=2 * self.scale_factor)
            lr_img = utils.shave(test_input[0], border_size=2)
            bc_img = utils.shave(utils.img_interp(test_input[0], self.scale_factor), border_size=2 * self.scale_factor)

            # calculate psnrs
            bc_psnr = utils.PSNR(bc_img, gt_img)
            recon_psnr = utils.PSNR(recon_img, gt_img)

            # save result images
            result_imgs = [gt_img, lr_img, bc_img, recon_img]
            psnrs = [None, None, bc_psnr, recon_psnr]
            utils.plot_test_result(result_imgs, psnrs, epoch + 1, save_dir=self.save_dir, is_training=True)

            print("Saving training result images at epoch %d" % (epoch + 1))

            # Save trained parameters of model
            if (epoch + 1) % self.save_epochs == 0:
                self.save_model(epoch + 1)

        # Plot avg. loss
        utils.plot_loss([avg_loss], self.num_epochs, save_dir=self.save_dir)
        print("Training is finished.")

        # Save final trained parameters of model
        self.save_model(epoch=None)

    def test(self):
        # networks
        self.model = Net(num_channels=self.num_channels, scale_factor=self.scale_factor, d=56, s=12, m=4)

        if self.gpu_mode:
            self.model.cuda()

        # load model
        self.load_model()

        # load dataset
        test_data_loader = self.load_dataset(dataset='test')

        # Test
        print('Test is started.')
        img_num = 0
        self.model.eval()
        for input, target in test_data_loader:
            # input data (low resolution image)
            if self.gpu_mode:
                y_ = Variable(input.cuda())
            else:
                y_ = Variable(input)

            # prediction
            recon_imgs = self.model(y_)
            for i, recon_img in enumerate(recon_imgs):
                img_num += 1
                recon_img = recon_imgs[i].cpu().data
                gt_img = utils.shave(target[i], border_size=2 * self.scale_factor)
                lr_img = utils.shave(input[i], border_size=2)
                bc_img = utils.shave(utils.img_interp(input[i], self.scale_factor), border_size=2 * self.scale_factor)

                # calculate psnrs
                bc_psnr = utils.PSNR(bc_img, gt_img)
                recon_psnr = utils.PSNR(recon_img, gt_img)

                # save result images
                result_imgs = [gt_img, lr_img, bc_img, recon_img]
                psnrs = [None, None, bc_psnr, recon_psnr]
                utils.plot_test_result(result_imgs, psnrs, img_num, save_dir=self.save_dir)

                print("Saving %d test result images..." % img_num)

    def test_single(self, img_fn):
        # networks
        self.model = Net(num_channels=self.num_channels, scale_factor=self.scale_factor, d=56, s=12, m=4)

        if self.gpu_mode:
            self.model.cuda()

        # load model
        self.load_model()

        # load data
        img = Image.open(img_fn)
        img = img.convert('YCbCr')
        y, cb, cr = img.split()

        input = Variable(ToTensor()(y)).view(1, -1, y.size[1], y.size[0])
        if self.gpu_mode:
            input = input.cuda()

        self.model.eval()
        recon_img = self.model(input)

        # save result images
        utils.save_img(recon_img.cpu().data, 1, save_dir=self.save_dir)

        out = recon_img.cpu()
        out_img_y = out.data[0]
        out_img_y = (((out_img_y - out_img_y.min()) * 255) / (out_img_y.max() - out_img_y.min())).numpy()
        # out_img_y *= 255.0
        # out_img_y = out_img_y.clip(0, 255)
        out_img_y = Image.fromarray(np.uint8(out_img_y[0]), mode='L')

        out_img_cb = cb.resize(out_img_y.size, Image.BICUBIC)
        out_img_cr = cr.resize(out_img_y.size, Image.BICUBIC)
        out_img = Image.merge('YCbCr', [out_img_y, out_img_cb, out_img_cr]).convert('RGB')

        # save img
        result_dir = os.path.join(self.save_dir, 'result')
        if not os.path.exists(result_dir):
            os.mkdir(result_dir)
        save_fn = result_dir + '/SR_result.png'
        out_img.save(save_fn)

    def save_model(self, epoch=None):
        model_dir = os.path.join(self.save_dir, 'model')
        if not os.path.exists(model_dir):
            os.mkdir(model_dir)
        if epoch is not None:
            torch.save(self.model.state_dict(), model_dir + '/' + self.model_name + '_param_epoch_%d.pkl' % epoch)
        else:
            torch.save(self.model.state_dict(), model_dir + '/' + self.model_name + '_param.pkl')

        print('Trained model is saved.')

    def load_model(self):
        model_dir = os.path.join(self.save_dir, 'model')

        model_name = model_dir + '/' + self.model_name + '_param.pkl'
        if os.path.exists(model_name):
            self.model.load_state_dict(torch.load(model_name))
            print('Trained generator model is loaded.')
            return True
        else:
            print('No model exists to load.')
            return False
