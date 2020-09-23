import os
import torch
import numpy as np
from torch import nn
from torch.distributions import Normal
from torch.nn import CrossEntropyLoss
from torch.utils.tensorboard import SummaryWriter
from torchvision import models
from torch.nn import functional as F
from utils import display_data_shape, get_named_dict, calc_iou_per_class,\
    log_data, load_config


class EncoderBlock(nn.Module):
    """ Encoder with pretrained backbone """
    def __init__(self):
        super(EncoderBlock, self).__init__()
        self.layers = nn.ModuleList(list(models.mobilenet_v2(pretrained=True)
                                    .features))
    
    def forward(self, x):
        features = []
        output_layers = [1, 3, 6, 13]
        for i, layer in enumerate(self.layers):
            x = layer(x)
            if i in output_layers:
                features.append(x)
        return x, features


def decoder_block(config, trans_in_channels, trans_out_channels, conv_out_channels, dropout=True):
    """ Sequentical group formimg a decoder block """
    layers = [
              nn.Conv2d(conv_out_channels, conv_out_channels, kernel_size=3, stride=1, padding=1),
              nn.ReLU(),
              nn.Dropout(config.dropout_rate),
              nn.BatchNorm2d(conv_out_channels),
              nn.ReLU(),
              nn.ConvTranspose2d(trans_in_channels, trans_out_channels, kernel_size=4, stride=2, padding=1)
             ]
    conv_block = nn.Sequential(*layers)
    return conv_trans, conv_block


class DecoderBlock(nn.Module):
    """
    Leo Decoder
    """
    def __init__(self, config):
        super(DecoderBlock, self).__init__()
        base_chn = 8
        self.conv_trans1, self.conv_1 = decoder_block(config, 1280, base_chn, 96 + base_chn)
        self.conv_trans2, self.conv_2 = decoder_block(config, 96 + base_chn, base_chn*2, 32 + base_chn*2)
        self.conv_trans3, self.conv_3 = decoder_block(config, 32 + base_chn*2, base_chn*3, 24 + base_chn*3)
        self.conv_trans4, self.conv_4 = decoder_block(config, 24 + base_chn*3, base_chn*4, 16 + base_chn*4)
        self.conv_trans_final = nn.ConvTranspose2d(16 + base_chn*4, 16 + base_chn*4, kernel_size=4, stride=2, padding=1)
       
    def forward(self, x, concat_features):
        o = self.conv_trans1(x)
        o = torch.cat([o, concat_features[-1]], dim=1)
        o = self.conv_1(o)
        o = self.conv_trans2(o)
        o = torch.cat([o, concat_features[-2]], dim=1)
        o = self.conv_2(o)
        o = self.conv_trans3(o)
        o = torch.cat([o, concat_features[-3]], dim=1)
        o = self.conv_3(o)
        o = self.conv_trans4(o)
        o = torch.cat([o, concat_features[-4]], dim=1)
        o = self.conv_4(o)
        o = self.conv_trans_final(o)
        return o


class LEO(nn.Module):
    """
    contains functions to perform latent embedding optimization
    """
    def __init__(self, config, mode="meta_train"):
        super(LEO, self).__init__()
        self.config = config
        self.mode = mode
        self.img_dims = (img_dims.channels, img_dims.height, img_dims.width)
        self.encoder = EncoderBlock()
        self.decoder = DecoderBlock(config.hyperparameters)
        self.device = torch.device("cuda:0" if torch.cuda.is_available()
                                   and config.use_gpu else "cpu")
        seg_network = nn.Conv2d(16 + 8*4 + 3 , 2, kernel_size=3, stride=1, padding=1)
        self.seg_weight = seg_network.weight.detach().to(self.device)
        self.seg_weight.requires_grad = True
        self.loss_fn = CrossEntropyLoss()
        self.optimizer_decoder = torch.optim.Adam(
            self.decoder.parameters(), lr=config.hyperparameters.outer_loop_lr)
        self.optimizer_seg_network = torch.optim.Adam(
            [self.seg_weight], lr=config.hyperparameters.outer_loop_lr)

    def freeze_encoder(self):
        """ Freeze encoder weights """
        for name, param in self.named_parameters():
            if "encoder" in name:
                param.requires_grad = False

    def forward_encoder(self, x):
        """ Performs forward pass through the encoder """
        encoder_outputs = self.encoder(x)
        if not encoder_outputs[-1].requires_grad:
            encoder_outputs[-1].requires_grad = True
        return encoder_outputs

    def forward_decoder(self, encoder_outputs):
        """Performs forward pass through the decoder"""
        output = self.decoder(encoder_outputs)
        return output

    def forward_segnetwork(self, decoder_out, x, weight):
        """  Receives features from the decoder
             Concats the features with input image
             Convolution layer acts on the concatenated input
            Args:
                decoder_out (torch.Tensor): decoder output features
                x (torch.Tensor): input images
                weight(tf.tensor): kernels for the segmentation network
            Returns:
                pred(tf.tensor): predicted logits
        """
        o = torch.cat([decoder_out, x], dim=1)
        pred = F.conv2d(o, weight, bias, padding=1)
        return pred

    def forward(self, x, latents=None):
        """ Performs a forward pass through the entire network
            - The Autoencoder generates features using the inputs
            - Features are concatenated with the inputs
            - The concatenated features are segmented
            Args:
                x (torch.Tensor): input image
                latents(torch.Tensor): output of the bottleneck
            Returns:
                latents(torch.Tensor): output of the bottleneck
                features(torch.Tensor): output of the decoder
                pred(torch.Tensor): predicted logits
        """
        encoder_outputs = self.forward_encoder(x)
        if latents is not None:
            encoder_outputs = encoder_outputs[:4] + [latents]
        else:
            latents = encoder_outputs[-1]
        features = self.forward_decoder(encoder_outputs)
        pred = self.forward_segnetwork(features, x, self.seg_weight)
        return latents, features, pred
    
        def leo_inner_loop(self, x, y):
            """ Performs innerloop optimization
                - It updates the latents taking gradients wrt the training loss
                - It generates better features after the latents are updated

                Args:
                    x(torch.Tensor): input training image
                    y(torch.Tensor): input training mask

                Returns:
                    seg_weight_grad(torch.Tensor): The last gradient of the
                     training loss wrt to the segmenation weights
                    features(torch.Tensor): The last generated features
            """    
            inner_lr = self.config.hyperparameters.inner_loop_lr
            latents, _, pred = self.forward(x)
            tr_loss = self.loss_fn(y, pred)
            for _ in range(self.config.hyperparameters.num_adaptation_steps):
                latents_grad = torch.autograd.grad(tr_loss, [latents],
                                                   create_graph=False)[0]
                with torch.no_grad():
                    latents -= inner_lr * latents_grad
                latents, features, pred = self.forward(x, latents)
                tr_loss = self.loss_fn(pred, y.long())
            seg_weight_grad = torch.autograd.grad(tr_loss, [self.seg_weight],
                                                  create_graph=False)[0]
            return seg_weight_grad, features

    def finetuning_inner_loop(self, data_dict, tr_features, seg_weight_grad,
                              transformers, mode):
        """ Finetunes the segmenation weights/kernels by performing MAML
            Args:
                data_dict (dict): contains tr_imgs, tr_masks, val_imgs, val_masks
                tr_features (torch.Tensor): tensor containing decoder features
                segmentation_grad (torch.Tensor): gradients of the training
                                                loss to the segmenation weights
            Returns:
                val_loss (torch.Tensor): validation loss
                seg_weight_grad (torch.Tensor): gradient of validation loss
                                                wrt segmentation weights
                decoder_grads (torch.Tensor): gradient of validation loss
                                                wrt decoder weights
                transformers(tuple): tuple of image and mask transformers
                weight (torch.Tensor): segmentation weights
        """
        img_transformer, mask_transformer = transformers
        finetuning_lr = self.config.hyperparameters.finetuning_lr
        num_steps = self.config.hyperparameters.num_finetuning_steps
        weight = self.seg_weight - finetuning_lr * seg_weight_grad
        for _ in range(num_steps - 1):
            pred = self.forward_segnetwork(tr_features, data_dict.tr_imgs, weight)
            tr_loss = self.loss_fn(pred, data_dict.tr_masks)
            seg_weight_grad = torch.autograd.grad(tr_loss, [weight],
                                                  create_graph=False)[0]
            weight -= finetuning_lr * seg_weight_grad

            if mode == "meta_train":
                encoder_outputs = self.forward_encoder(data_dict.val_imgs)
                features = self.forward_decoder(encoder_outputs)
                pred = self.forward_segnetwork(features, data_dict.val_imgs, weight)
                val_loss = self.loss_fn(pred, data_dict.val_masks)
                seg_weight_grad, decoder_grads = torch.autograd.
                grad(val_loss, [weight, self.decoder.parameters()],
                     create_graph=False)
                mean_iou = calc_iou_per_class(pred, data_dict.val_masks)
                return val_loss, seg_weight_grad, decoder_grads, mean_iou
            else:
                mean_ious = []
                val_losses = []
                val_img_paths = data.val_imgs
                val_mask_paths = data.val_masks
                for _img_path, _mask_path in tqdm(zip(val_img_paths, val_mask_paths)):
                    input_img = list_to_tensor(_img_path, img_transformer)
                    input_mask = list_to_tensor(_mask_path, mask_transformer)
                    encoder_outputs = self.forward_encoder(input_img)
                    features = self.forward_decoder(encoder_outputs)
                    prediction = self.forward_segnetwork(features, input_img, weight)
                    val_loss = self.loss_fn(input_mask, prediction)
                    mean_iou = calc_iou_per_class(prediction, input_mask)
                    mean_ious.append(mean_iou)
                    val_losses.append(val_loss)
                mean_iou = np.mean(mean_ious)
                val_loss = np.mean(val_losses)
                return val_loss, None, None, mean_iou
        
    def compute_loss(self, metadata, train_stats, transformers, mode="meta_train"):
        """ Performs meta optimization across tasks
            returns the meta validation loss across tasks
            Args:
                metadata(dict): dictionary containing training data
                train_stats(object): object that stores training statistics
                transformers(tuple): tuple of image and mask transformers
                mode(str): meta_train, meta_val or meta_test
            Returns:
                total_val_loss(float32): meta-validation loss
                train_stats(object): object that stores training statistics
        """
        num_tasks = len(metadata[0])
        if train_stats.episode % self.config.display_stats_interval == 1:
            display_data_shape(metadata)

        classes = metadata[4]
        total_val_loss = []
        mean_iou_dict = {}
        total_grads = None
        for batch in range(num_tasks):
            data = get_named_dict(metadata, batch)
            seg_weight_grad, features = self.leo_inner_loop(data.tr_imgs, data.tr_masks)
            val_loss, seg_weight_grad, decoder_grads, mean_iou = \
                self.finetuning_inner_loop(data, features, seg_weight_grad,
                                           transformers, mode)
            if mode == "meta_train":
                decoder_grads = [grad/num_tasks for grad in decoder_grads]
                if total_grads is None:
                    total_grads = decoder_grads
                    seg_weight_grad = seg_weight_grad/num_tasks
                else:
                    total_grads = [total_grads[i] + decoder_grads[i]\
                                   for i in range(len(decoder_grads))]
                    seg_weight_grad += seg_weight_grad/num_tasks
            self.optimizer_decoder.zero_grad()
            self.optimizer_seg_network.zero_grad()
            i = 0
            for name, params in self.decoder.parameters():
                params.grad = total_grads[i]
                i += 1
            self.seg_weight.grad = seg_weight_grad
            self.optimizer_decoder.step()
            self.optimizer_seg_network.step()
            mean_iou_dict[classes[batch]] = mean_iou
            total_val_loss.append(val_loss)
            
        total_val_loss = float(sum(total_val_loss)/len(total_val_loss))
        stats_data = {
            "mode": mode,
            "total_val_loss": total_val_loss,
            "mean_iou_dict": mean_iou_dict
        }
        train_stats.update_stats(**stats_data)
        return total_val_loss, train_stats


def save_model(model, optimizer, config, stats):
    """
    Save the model while training based on check point interval
    
    if episode number is not -1 then a prompt to delete checkpoints occur if 
    checkpoints for that episode number exits.
    This only occurs if the prompt_deletion flag in the experiment dictionary
    is true else checkpoints that already exists are automatically deleted

    Args:
        model - trained model       
        optimizer - optimized weights
        config - global config
        stats - dictionary containing stats for the current episode
    
    Returns:
    """
    data_to_save = {
        'mode': stats.mode,
        'episode': stats.episode,
        'model_state_dict': model.state_dict(),
        'optimizer_state_dict': optimizer.state_dict(),
        'kl_loss': stats.kl_loss,
        'total_val_loss': stats.total_val_loss
    }

    experiment = config.experiment
    model_root = os.path.join(config.data_path, "models")
    model_dir = os.path.join(model_root, "experiment_{}" \
                             .format(experiment.number))

    checkpoint_path = os.path.join(model_dir, f"checkpoint_{stats.episode}.pth.tar")
    if not os.path.exists(checkpoint_path):
        torch.save(data_to_save, checkpoint_path)
    else:
        trials = 0
        while trials < 3:
            if experiment.prompt_deletion:
                print(f"Are you sure you want to delete checkpoint: {stats.episode}")
                print(f"Type Yes or y to confirm deletion else No or n")
                user_input = input()
            else:
                user_input = "Yes"
            positive_options = ["Yes", "y", "yes"]
            negative_options = ["No", "n", "no"]
            if user_input in positive_options:
                # delete checkpoint
                os.remove(checkpoint_path)
                torch.save(data_to_save, checkpoint_path)
                log_filename = os.path.join(model_dir, "model_log.txt")
                msg = msg = f"\n*********** checkpoint {stats.episode} was deleted **************"
                log_data(msg, log_filename)
                break

            elif user_input in negative_options:
                raise ValueError("Supply the correct episode number to start experiment")
            else:
                trials += 1
                print("Wrong Value Supplied")
                print(f"You have {3 - trials} left")
                if trials == 3:
                    raise ValueError("Supply the correct answer to the question")

def load_model(config):

    """
    Loads the model
    Args:
        config - global config
        **************************************************
        Note: The episode key in the experiment dict
        implies the checkpoint that should be loaded 
        when the model resumes training. If episode is 
        -1, then the latest model is loaded else it loads
        the checkpoint at the supplied episode
        *************************************************
    Returns:
        leo :loaded model that was saved
        optimizer: loaded weights of optimizer
        stats: stats for the last saved model
    """
    experiment = config.experiment
    model_dir  = os.path.join(config.data_path, "models", "experiment_{}"\
                 .format(experiment.number))
    
    checkpoints = os.listdir(model_dir)
    checkpoints = [i for i in checkpoints if os.path.splitext(i)[-1] == ".tar"]
    max_cp = max([int(cp.split(".")[0].split("_")[1]) for cp in checkpoints])
    #if experiment.episode == -1, load latest checkpoint
    episode = max_cp if experiment.episode == -1 else experiment.episode
    checkpoint_path = os.path.join(model_dir, f"checkpoint_{episode}.pth.tar")
    checkpoint = torch.load(checkpoint_path)

    log_filename = os.path.join(model_dir, "model_log.txt")
    msg =  f"\n*********** checkpoint {episode} was loaded **************" 
    log_data(msg, log_filename)
    
    leo = LEO(config)
    optimizer = torch.optim.Adam(leo.parameters(), lr=config.hyperparameters.outer_loop_lr)
    leo.load_state_dict(checkpoint['model_state_dict'])
    optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
    mode = checkpoint['mode']
    total_val_loss = checkpoint['total_val_loss']
    kl_loss = checkpoint['kl_loss']

    stats = {
        "mode": mode,
        "episode": episode,
        "kl_loss": kl_loss,
        "total_val_loss": total_val_loss
        }

    return leo, optimizer, stats

