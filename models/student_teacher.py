from __future__ import print_function
import os
import torch
import numpy as np
import torch.nn as nn
import torch.nn.functional as F
import torch.distributions as D
from torch.autograd import Variable
from copy import deepcopy

from helpers.distributions import nll
from helpers.utils import expand_dims, long_type, squeeze_expand_dim, \
    ones_like, float_type, pad, inv_perm, one_hot_np, \
    zero_pad_smaller_cat, check_or_create_dir
from models.vae.parallelly_reparameterized_vae import ParallellyReparameterizedVAE
from models.vae.sequentially_reparameterized_vae import SequentiallyReparameterizedVAE


def detach_from_graph(param_map):
    for _, v in param_map.items():
        if isinstance(v, dict):
            detach_from_graph(v)
        else:
            v = v.detach_()


def kl_categorical_categorical(dist_a, dist_b, rnd_perm, from_index=0):
    # invert the shuffle for the KL calculation
    if rnd_perm is not None:
        dist_a_logits = inv_perm(dist_a['logits'], rnd_perm)
        dist_b_logits = inv_perm(dist_b['logits'], rnd_perm)
    else:
        dist_a_logits, dist_b_logits = dist_a['logits'], dist_b['logits']

    # https://github.com/tensorflow/tensorflow/blob/r1.1/tensorflow/contrib/distributions/python/ops/categorical.py
    dist_a_softmax     = F.softmax(    dist_a_logits[from_index:], dim=-1)
    dist_a_log_softmax = F.log_softmax(dist_a_logits[from_index:], dim=-1)
    dist_b_log_softmax = F.log_softmax(dist_b_logits[from_index:], dim=-1)

    # zero pad the smaller categorical
    dist_a_log_softmax, dist_b_log_softmax = zero_pad_smaller_cat(dist_a_log_softmax, dist_b_log_softmax)
    dist_a_softmax, dist_b_log_softmax     = zero_pad_smaller_cat(dist_a_softmax, dist_b_log_softmax)

    delta_log_probs1 = dist_a_log_softmax - dist_b_log_softmax
    return torch.sum(dist_a_softmax * delta_log_probs1, dim=-1)


def kl_isotropic_gauss_gauss(dist_a, dist_b, rnd_perm, from_index=0):
    if rnd_perm is not None:
        mu0     = inv_perm(dist_a['mu']    , rnd_perm)[from_index:]
        mu1     = inv_perm(dist_b['mu']    , rnd_perm)[from_index:]
        logvar0 = inv_perm(dist_a['logvar'], rnd_perm)[from_index:]
        logvar1 = inv_perm(dist_b['logvar'], rnd_perm)[from_index:]
    else:
        mu0, logvar0 = dist_a['mu'][from_index:], dist_a['logvar'][from_index:]
        mu1, logvar1 = dist_b['mu'][from_index:], dist_b['logvar'][from_index:]

    n0 = D.Normal(mu0, logvar0)
    n1 = D.Normal(mu1, logvar1)
    return torch.sum(D.kl_divergence(n0, n1), dim=-1)


def lazy_generate_modules(model, img_shp, batch_size, cuda):
    ''' Super hax, but needed for building lazy modules '''
    model.eval()
    data = float_type(cuda)(batch_size, *img_shp).normal_()
    model(Variable(data))


class StudentTeacher(nn.Module):
    def __init__(self, initial_model, **kwargs):
        ''' Helper to keep the student-teacher architecture '''
        super(StudentTeacher, self).__init__()
        self.ratio    = 1.0
        self.student  = initial_model
        self.teacher  = None
        self.rnd_perm = None
        self.current_model = 0
        self.num_teacher_samples = None
        self.num_student_samples = None

        # grab the meta config and print for
        self.config = kwargs['kwargs']

    def load(self):
        # load the model if it exists
        if os.path.isdir(self.config['model_dir']):
            print("E: {} does not exist...".format(self.config['model_dir']))
            return False

        model_filename = os.path.join(self.config['model_dir'], self.get_name() + ".th")
        if os.path.isfile(model_filename):
            print("loading existing student-teacher model: {}".format(model_filename))
            lazy_generate_modules(self, self.student.input_shape, self.config['batch_size'], self.config['cuda'])
            self.load_state_dict(torch.load(model_filename), strict=True)
            return True
        else:
            print("E: {} does not exist...".format(model_filename))
            return False

    def save(self, overwrite=False):
        # save the model if it doesnt exist
        check_or_create_dir(self.config['model_dir'])
        model_filename = os.path.join(self.config['model_dir'], self.get_name() + ".th")
        if not os.path.isfile(model_filename) or overwrite:
            print("I: saving existing student-teacher model...")
            torch.save(self.state_dict(), model_filename)

    def get_name(self):
        return "{}{}_cg{}_s{}{}".format(
            str(self.config['uid'])              , str(self.current_model),
            str(self.config['consistency_gamma']), str(int(self.config['shuffle_minibatches'])),
            self.student.get_name()
        )

    def posterior_regularizer_parallel(self, q_z_given_x_t, q_z_given_x_s):
        ''' Evaluates KL(Q_{\Phi})(z | \hat{x}) || Q_{\phi})(z | \hat{x})) '''
        # TF: kl = self.kl_categorical(p=self.q_z_s_given_x_t, q=self.q_z_t_given_x_t)
        if 'discrete' in q_z_given_x_s and 'discrete' in q_z_given_x_t:
            return kl_categorical_categorical(q_z_given_x_s['discrete'], q_z_given_x_t['discrete'],
                                              self.rnd_perm, from_index=self.num_student_samples)
        elif 'gaussian' in q_z_given_x_s and 'gaussian' in q_z_given_x_t:
            # gauss kl-kl doesnt have any from-index
            return kl_isotropic_gauss_gauss(q_z_given_x_s['gaussian'], q_z_given_x_t['gaussian'],
                                            self.rnd_perm, from_index=self.num_student_samples)
        else:
            raise NotImplementedError("unknown distribution requested for kl")

    def posterior_regularizer_sequential(self, q_z_given_x_t, q_z_given_x_s):
        ''' Evaluates KL(Q_{\Phi})(z | \hat{x}) || Q_{\phi})(z | \hat{x})) over all the discrete pairs'''
        # TODO: consider logic for gauss kl
        # TF: kl = self.kl_categorical(p=self.q_z_s_given_x_t, q=self.q_z_t_given_x_t)
        num_params     = len(q_z_given_x_s) // 2
        batch_size, kl = q_z_given_x_s['z_0'].size(0), None
        for i in range(num_params):
            if 'discrete' in q_z_given_x_s['params_%d'%i]:
                kl_tmp = kl_categorical_categorical(q_z_given_x_s['params_%d'%i]['discrete'],
                                                    q_z_given_x_t['params_%d'%i]['discrete'],
                                                    self.rnd_perm, from_index=self.num_student_samples)
                kl = kl_tmp if kl is None else kl + kl_tmp

        return kl

    def posterior_regularizer(self, q_z_given_x_t, q_z_given_x_s):
        ''' helper to separate posterior regularization for seq / parallel '''
        posterior_fn_map = {
            'sequential': self.posterior_regularizer_sequential,
            'parallel': self.posterior_regularizer_parallel
        }
        return posterior_fn_map[self.config['vae_type']](q_z_given_x_t, q_z_given_x_s)

    def likelihood_regularizer(self, p_x_given_z_t_activated, p_x_given_z_s_logits):
        if self.rnd_perm is not None:
            p_x_given_z_s_logits = inv_perm(p_x_given_z_s_logits, self.rnd_perm)
            p_x_given_z_t_activated = inv_perm(p_x_given_z_t_activated, self.rnd_perm)

        img_unrolled = int(np.prod(self.config['img_shp']))
        p_x_given_z_s_logits = p_x_given_z_s_logits[self.num_student_samples:].view(-1, img_unrolled)
        p_x_given_z_t_activated = p_x_given_z_t_activated[self.num_student_samples:].view(-1, img_unrolled)
        return nll(p_x_given_z_t_activated, p_x_given_z_s_logits, self.config['nll_type'])

    def _lifelong_loss_function(self, output_map):
        ''' returns a combined loss of the VAE loss + regularizers '''
        vae_loss = self.student.loss_function(output_map['student']['x_reconstr_logits'],
                                              output_map['augmented']['data'], output_map['student']['params'])
        if 'teacher' in output_map and not self.config['disable_regularizers']:
            posterior_regularizer = self.posterior_regularizer(output_map['teacher']['params'],
                                                               output_map['student']['params'])
            diff = int(np.abs(vae_loss['loss'].size(0) - posterior_regularizer.size(0)))
            posterior_regularizer = pad(posterior_regularizer, diff, dim=0, prepend=True)

            # add the likelihood regularizer and multiply it by the const
            likelihood_regularizer = self.likelihood_regularizer(output_map['teacher']['x_reconstr'],
                                                                 output_map['student']['x_reconstr_logits'])
            likelihood_regularizer = pad(likelihood_regularizer, diff, dim=0, prepend=True)
            if self.rnd_perm is not None: # re-shuffle
                posterior_regularizer  =  posterior_regularizer[self.rnd_perm]
                likelihood_regularizer = likelihood_regularizer[self.rnd_perm]

            posterior_regularizer  = self.config['consistency_gamma'] * posterior_regularizer
            likelihood_regularizer = self.config ['likelihood_gamma'] * likelihood_regularizer
            vae_loss['loss_mean']  = torch.mean(vae_loss['loss'] + likelihood_regularizer + posterior_regularizer)
            vae_loss['posterior_regularizer_mean']  = torch.mean(posterior_regularizer)
            vae_loss['likelihood_regularizer_mean'] = torch.mean(likelihood_regularizer)
        return vae_loss

    def _ewc(self, fisher_matrix):
        losses = []
        fisher_len  = len(fisher_matrix)
        teacher_len = len(list(self.teacher.named_parameters()))
        student_len = len(list(self.student.named_parameters()))
        assert fisher_len == teacher_len == student_len, 'fisher, teacher, student params mismatch'
        for (nt, pt), (ns, ps), (nf, fish) in zip(self.teacher.named_parameters(),
                                                  self.student.named_parameters(),
                                                  fisher_matrix.items()):
            assert pt.size() != ps.size() != fish.size()
            losses.append(torch.sum(fish * (ps - pt)**2))
        return (self.config['ewc_gamma'] / 2.0) * sum(losses)


    def _ewc_loss_function(self, output_map, fisher_matrix):
        ''' returns a combined loss of the VAE loss + EWC '''
        vae_loss = self.student.loss_function(output_map['student']['x_reconstr_logits'],
                                              output_map['augmented']['data'],
                                              output_map['student']['params'])
        if 'teacher' in output_map and fisher_matrix is not None:
            ewc                   = self._ewc(fisher_matrix)
            vae_loss['ewc_mean']  = ewc
            vae_loss['loss_mean'] = torch.mean(vae_loss['loss']) + ewc
        return vae_loss


    def loss_function(self, output_map, fisher=None):
        if self.config['ewc_gamma'] > 0:
            return self._ewc_loss_function(output_map, fisher)
        return self._lifelong_loss_function(output_map)

    @staticmethod
    def disable_bn(module):
        for layer in module.children():
            if isinstance(layer, (nn.Sequential, nn.ModuleList)):
                StudentTeacher.disable_bn(layer)
            elif isinstance(layer, (nn.BatchNorm1d, nn.BatchNorm2d, nn.BatchNorm3d)):
                print("reseting {} parameters".format(layer))
                layer.reset_parameters()

    @staticmethod
    def copy_model(src, dest, disable_dst_grads=False, reset_dest_bn=True):
        src_params  = list(src.parameters())
        dest_params = list(dest.parameters())
        for i in range(len(src_params)):
            if src_params[i].size() == dest_params[i].size():
                dest_params[i].data[:] = src_params[i].data[:].clone()

            if disable_dst_grads:
                dest_params[i].requires_grad = False

        # reset batch norm layers
        if reset_dest_bn:
            StudentTeacher.disable_bn(dest)

        return src, dest

    def fork(self):
        # copy the old student into the teacher
        # dont increase discrete dim for ewc
        self.teacher = deepcopy(self.student)
        config_copy  = deepcopy(self.student.config)
        config_copy['discrete_size'] += 0 if self.config['ewc_gamma'] > 0 else self.config['discrete_size']
        del self.student

        # create a new student
        if self.config['vae_type'] == 'sequential':
            self.student = SequentiallyReparameterizedVAE(input_shape=self.teacher.input_shape,
                                                          num_current_model=self.current_model+1,
                                                          reparameterizer_strs=self.teacher.reparameterizer_strs,
                                                          **{'kwargs': config_copy}
            )
        elif self.config['vae_type'] == 'parallel':
            self.student = ParallellyReparameterizedVAE(input_shape=self.teacher.input_shape,
                                                        num_current_model=self.current_model+1,
                                                        **{'kwargs': config_copy}
            )
        else:
            raise Exception("unknown vae type requested")


        # forward pass once to build lazy modules
        data = float_type(self.config['cuda'])(self.student.config['batch_size'], *self.student.input_shape).normal_()
        self.student(Variable(data))

        # copy teacher params into student while omitting the projection weights
        self.teacher, self.student = self.copy_model(self.teacher, self.student, disable_dst_grads=False)

        # update the current model's ratio
        self.current_model += 1
        self.ratio          = self.current_model / (self.current_model + 1.0)
        num_teacher_samples = int(self.config['batch_size'] * self.ratio)
        num_student_samples = max(self.config['batch_size'] - num_teacher_samples, 1)
        print("teacher_samples: {:7d} | student_samples: {:7d}".format(num_teacher_samples, num_student_samples))

    def generate_synthetic_samples(self, model, batch_size, **kwargs):
        z_samples = model.reparameterizer.prior(
            batch_size, scale_var=self.config['generative_scale_var'], **kwargs
        )
        return model.nll_activation(model.generate(z_samples))

    def generate_synthetic_sequential_samples(self, model, num_rows=8):
        assert model.has_discrete()

        # create a grid of one-hot vectors for displaying in visdom
        # uses one row for original dimension of discrete component
        r1 = range(0                           , model.reparameterizer.config['discrete_size'],
                   self.config['discrete_size'])
        r2 = range(self.config['discrete_size'], model.reparameterizer.config['discrete_size'] + 1,
                   self.config['discrete_size'])
        discrete_indices = np.array([np.random.randint(begin, end, size=num_rows) for begin, end in zip(r1, r2)])
        discrete_indices = discrete_indices.reshape(-1)

        with torch.no_grad():
            z_samples = one_hot_np(model.reparameterizer.config['discrete_size'], discrete_indices)
            z_samples = Variable(torch.from_numpy(z_samples))
            z_samples = z_samples.type(float_type(self.config['cuda']))

            if self.config['reparam_type'] == 'mixture' and self.config['vae_type'] != 'sequential':
                ''' add in the gaussian prior '''
                z_gauss   = model.reparameterizer.gaussian.prior(z_samples.size(0))
                z_samples = torch.cat([z_gauss, z_samples], dim=-1)
            return model.nll_activation(model.generate(z_samples))

    def _augment_data(self, x):
        ''' return batch_size worth of samples that are augmented from the teacher model '''
        if self.ratio == 1.0 or not self.training or self.config['disable_augmentation']:
            return x   # base case

        batch_size = x.size(0)
        self.num_teacher_samples  = int(batch_size * self.ratio)
        self.num_student_samples  = max(batch_size - self.num_teacher_samples, 1)
        generated_teacher_samples = self.generate_synthetic_samples(self.teacher, batch_size)
        merged =  torch.cat([x[0:self.num_student_samples], generated_teacher_samples[0:self.num_teacher_samples]], 0)

        # workaround for batchnorm on multiple GPUs we shuffle the data and unshuffle it later for the posterior regularizer
        if self.config['shuffle_minibatches']:
            self.rnd_perm = torch.randperm(merged.size(0))
            if self.config['cuda']:
                self.rnd_perm = self.rnd_perm.cuda()
            return merged[self.rnd_perm]
        else:
            return merged


    def forward(self, x):
        x_augmented                      = self._augment_data(x).contiguous()
        x_recon_student, params_student  = self.student(x_augmented)
        x_reconstr_student_activated     = self.student.nll_activation(x_recon_student)
        _, q_z_given_xhat                = self.student.posterior(x_reconstr_student_activated)
        params_student['q_z_given_xhat'] = q_z_given_xhat

        ret_map = {
            'student':{
                'params': params_student,
                'x_reconstr': self.student.nll_activation(x_recon_student),
                'x_reconstr_logits': x_recon_student
            },
            'augmented': {
                'data': x_augmented,
                'num_student': self.num_student_samples,
                'num_teacher': self.num_teacher_samples
            }
        }

        # encode teacher with synthetic data
        if self.teacher is not None:
            # only teacher Q(z|x) is needed, so dont run decode step
            self.teacher.eval()
            x_recon_teacher, params_teacher = self.teacher(x_augmented)
            ret_map['teacher'] = {
                'params'           : params_teacher,
                'x_reconstr'       : self.teacher.nll_activation(x_recon_teacher),
                'x_reconstr_logits': x_recon_teacher
            }

        return ret_map
