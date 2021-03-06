import torch
import pickle
import numpy as np
import os
import argparse
import datetime
from tensorboardX import SummaryWriter
import time

from dcll.pytorch_libdcll import device
from dcll.experiment_tools import mksavedir, save_source, annotate
from dcll.pytorch_utils import grad_parameters, named_grad_parameters, NetworkDumper, tonumpy
from networks import ConvNetwork, ReferenceConvNetwork, load_network_spec
from data.utils import to_one_hot


def parse_args():
    parser = argparse.ArgumentParser(description='DCLL')
    parser.add_argument('--data', type=str, default='RadioML',
                        choices=['MNIST', 'RadioML'], help='which data to use')
    parser.add_argument('--radio_ml_data_dir', type=str, default='2018.01',
                        help='path to the folder containing the RadioML HDF5 file(s)')
    parser.add_argument('--min_snr', type=int, default=6,
                        metavar='N', help='minimum SNR (inclusive) to use during data loading')
    parser.add_argument('--max_snr', type=int, default=30,
                        metavar='N', help='maximum SNR (inclusive) to use during data loading')
    parser.add_argument('--per_h5_frac', type=float, default=0.5,
                        metavar='N', help='fraction of each HDF5 data file to use')
    parser.add_argument('--train_frac', type=float, default=0.9,
                        metavar='N', help='train split (1-TRAIN_FRAC is the test split)')
    parser.add_argument('--network_spec', type=str, default='snnmodclass_ohjay/networks/radio_ml_conv.yaml',
                        metavar='S', help='path to YAML file describing net architecture')
    parser.add_argument('--ref_network_spec', type=str, default='snnmodclass_ohjay/networks/radio_ml_conv_ref.yaml',
                        metavar='S', help='path to YAML file describing reference net architecture')
    parser.add_argument('--just_ref', action='store_true',
                        help='whether we want to just train the reference network')
    parser.add_argument('--I_resolution', type=int, default=128,
                        metavar='N', help='size of I dimension (used when representing I/Q plane as image)')
    parser.add_argument('--Q_resolution', type=int, default=128,
                        metavar='N', help='size of Q dimension (used when representing I/Q plane as image)')
    parser.add_argument('--I_bounds', type=float, default=(-1, 1),
                        nargs=2, help='range of values to represent in I dimension of I/Q image')
    parser.add_argument('--Q_bounds', type=float, default=(-1, 1),
                        nargs=2, help='range of values to represent in Q dimension of I/Q image')
    parser.add_argument('--restore_path', type=str,
                        metavar='S', help='path to .pth file from which to restore')
    parser.add_argument('--burnin', type=int, default=50,
                        metavar='N', help='burnin')
    parser.add_argument('--batch_size', type=int, default=64,
                        metavar='N', help='input batch size for training')
    parser.add_argument('--batch_size_test', type=int, default=64,
                        metavar='N', help='input batch size for testing')
    parser.add_argument('--n_steps', type=int, default=10000,
                        metavar='N', help='number of steps to train')
    parser.add_argument('--no_save', type=bool, default=False,
                        metavar='N', help='disables saving into Results directory')
    parser.add_argument('--seed', type=int, default=1,
                        metavar='S', help='random seed')
    parser.add_argument('--n_test_interval', type=int, default=20,
                        metavar='N', help='how many steps to run before testing')
    parser.add_argument('--n_test_samples', type=int, default=128,
                        metavar='N', help='how many test samples to use')
    parser.add_argument('--n_iters', type=int, default=1024, metavar='N',
                        help='for how many ms do we present a sample during classification')
    parser.add_argument('--n_iters_test', type=int, default=1024, metavar='N',
                        help='for how many ms do we present a sample during classification')
    parser.add_argument('--optim_type', type=str, default='Adam',
                        metavar='S', help='which optimizer to use')
    parser.add_argument('--loss_type', type=str, default='SmoothL1Loss',
                        metavar='S', help='which loss function to use')
    parser.add_argument('--learning_rates', type=float, default=[1e-6],
                        nargs='+', metavar='N', help='learning rates for each DCLL slice')
    parser.add_argument('--ref_lr', type=float, default=1e-3,
                        metavar='N', help='learning rate for reference network')
    parser.add_argument('--alpha', type=float, default=.92,
                        metavar='N', help='Time constant for neuron')
    parser.add_argument('--alphas', type=float, default=.85,
                        metavar='N', help='Time constant for synapse')
    parser.add_argument('--alpharp', type=float, default=.65,
                        metavar='N', help='Time constant for refractory')
    parser.add_argument('--arp', type=float, default=0,
                        metavar='N', help='Absolute refractory period in ticks')
    parser.add_argument('--random_tau', type=bool, default=True,
                        help='randomize time constants in convolutional layers')
    parser.add_argument('--beta', type=float, default=.95,
                        metavar='N', help='Beta2 parameters for Adamax')
    parser.add_argument('--lc_ampl', type=float, default=0.5,
                        metavar='N', help='magnitude of local classifier init')
    parser.add_argument('--netscale', type=float, default=1.,
                        metavar='N', help='scale network size')
    parser.add_argument('--comment', type=str, default='',
                        help='comment to name tensorboard files')
    parser.add_argument('--output', type=str, default='results',
                        help='folder name for the results')
    return parser.parse_args()


if __name__ == '__main__':


    args = parse_args()
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)


# *************************** File management **************************************************************************
    current_time = datetime.datetime.now().strftime('%b%d_%H-%M-%S')
    log_dir = os.path.join('runs', args.data, current_time)
    writer = SummaryWriter(log_dir=log_dir, comment='%s Conv' % args.data)
    print('#'*120)
    print('- Logging directory: {log_dir}'.format(log_dir=log_dir))
    out_dir = os.path.join(args.output, args.data, current_time)
    os.makedirs(out_dir)
    print('- Output directory: {out_dir}'.format(out_dir=out_dir))
    print('#'*120)
# **********************************************************************************************************************


# ****************************** Define and set kwargs parameters ******************************************************
    """"
    Define and set kwargs parameters for functions get_loader and to_spike_train
    - 'get_loader' is used to load data
    - 'to_spike_train' is used to convert each I/Q sample to a spike in I/Q plane
    """

    get_loader_kwargs  = {}
    to_st_train_kwargs = {}
    to_st_test_kwargs  = {}

    n_iters = args.n_iters
    n_iters_test = args.n_iters_test


    if args.data == 'MNIST':
        im_dims = (1, 28, 28)
        ref_im_dims = (1, 28, 28)
        target_size = 10
        from data.load_mnist import get_mnist_loader as get_loader
        from data.utils import image2spiketrain as to_spike_train
        # Set "to spike train" kwargs
        for to_st_kwargs in (to_st_train_kwargs, to_st_test_kwargs):
            to_st_kwargs['input_shape'] = im_dims
            to_st_kwargs['gain'] = 100
        to_st_train_kwargs['min_duration'] = n_iters - 1
        to_st_train_kwargs['max_duration'] = n_iters
        to_st_test_kwargs['min_duration'] = n_iters_test - 1
        to_st_test_kwargs['max_duration'] = n_iters_test

    elif args.data == 'RadioML':
        im_dims = (1, args.Q_resolution, args.I_resolution)
        ref_im_dims = (2, 1, 1024)
        target_size = 24
        # 'get_radio_ml_loader' is defined in data/load_radio_ml.py and used to load data
        from data.load_radio_ml import get_radio_ml_loader as get_loader
        # 'iq2spiketrain' is defined in data/utils.py and used to convert each I/Q sample to a spike in the I/Q plane over time
        from data.utils import iq2spiketrain as to_spike_train

        # ---------------------------- Set "get loader" kwargs ---------------------------------------------------------
        get_loader_kwargs['data_dir'] = args.radio_ml_data_dir # Set saving data folder
        get_loader_kwargs['min_snr'] = args.min_snr            # Set the min SNR
        get_loader_kwargs['max_snr'] = args.max_snr            # Set the max SNR
        get_loader_kwargs['per_h5_frac'] = args.per_h5_frac    # Set fraction of each HDF5 data file to use
        get_loader_kwargs['train_frac'] = args.train_frac      # Set the split fraction of train set over (train+test) set
        # --------------------------------------------------------------------------------------------------------------

        # ---------------------------- Set "to spike train" kwargs -----------------------------------------------------
        for to_st_kwargs in (to_st_train_kwargs, to_st_test_kwargs):
            to_st_kwargs['out_w'] = args.I_resolution   # Default value: 128. Set size of I dimension (used when representing I/Q plane as image)
            to_st_kwargs['out_h'] = args.Q_resolution   # Default value: 128. Set size of Q dimension (used when representing I/Q plane as image)
            to_st_kwargs['min_I'] = args.I_bounds[0]    # Default value: -1. Set min values to be represented in I dimension of I/Q image
            to_st_kwargs['max_I'] = args.I_bounds[1]    # Default value: 1. Set max values to be represented in Q dimension of I/Q image
            to_st_kwargs['min_Q'] = args.Q_bounds[0]    # Default value: -1
            to_st_kwargs['max_Q'] = args.Q_bounds[1]    # Default value: 1
        to_st_train_kwargs['max_duration'] = n_iters      # Default value: 1024
        to_st_train_kwargs['gs_stdev'] = 0
        to_st_test_kwargs['max_duration'] = n_iters_test  # Default value: 1024
        to_st_test_kwargs['gs_stdev'] = 0
        # --------------------------------------------------------------------------------------------------------------
# **********************************************************************************************************************


# **********************************************************************************************************************
    # 'np.ceil' returns the min integer i, i>=x
    # 'args.n_test_samples': default value: 128. Nb of test samples to be used
    # 'args.batch_size_test': default value: 64. Input batch size for testing
    n_test = np.ceil(float(args.n_test_samples) /
                     args.batch_size_test).astype(int)
    # 'args.n_steps': default value: 10000. Nb of steps to train
    # 'args.n_test_interval': default value: 20. Nb of steps to run before testing
    n_tests_total = np.ceil(float(args.n_steps) /
                            args.n_test_interval).astype(int)
# **********************************************************************************************************************


# ************************* Define optimizer & loss func ***************************************************************
    """Assign the optimizer method as 'args.optim_type' """
    opt = getattr(torch.optim, args.optim_type)
    opt_param = {
        'betas': [0.0, args.beta],  # 'args.beta': default value: 0.95. Beta2 parameters for Adamax
        'weight_decay': 10.0,
    }
    ref_opt_param = {
        'lr': args.ref_lr,
        'betas': [0.0, args.beta],
    }
    # Assign the loss func as 'args.loss_type'
    loss = getattr(torch.nn, args.loss_type)
# **********************************************************************************************************************


# ************************* Set up for other networks except reference network *****************************************
    """If we don't want to train only the reference network"""
    if not args.just_ref:
        burnin = args.burnin     # Default value: 50
        # 'load_network_spec' is defined in networks/__init__.py
        # Load network from path of .yaml file ('args.network_spec') describing network architecture
        convs = load_network_spec(args.network_spec)

        # 'ConvNetwork' is defined in networks/__init__.py
        net = ConvNetwork(args, im_dims, args.batch_size, convs, target_size,
                          act=torch.nn.Sigmoid(), loss=loss, opt=opt, opt_param=opt_param,
                          learning_rates=args.learning_rates, burnin=burnin)

        # ------------------------ Load weights to model variable 'net' -------------------------------------------------
        if args.restore_path:
            print('-' * 120)
            if not os.path.isfile(args.restore_path):
                print('ERROR: Cannot load file `%s`.' % args.restore_path)
                print('File does not exist! Exit loading...')
            else:
                state_dict = torch.load(args.restore_path)
                net.load_state_dict(state_dict)
                print('Loaded the weights of trained SNN model from `%s`.' % args.restore_path)
            print('-' * 120)
        # --------------------------------------------------------------------------------------------------------------

        # -------------------------- Model set up -----------------------------------------------------------------------
        net = net.to(device)
        net.reset(True)   # Reset all the trained weight of model 'net'
        # Create a new 3D array has size [n_tests_total, n_test, len(net.dcll_slices)]
        acc_test = np.empty([n_tests_total, n_test, len(net.dcll_slices)])
        # 'NetworkDumper' is class defined in dcll/pytorch_utils.py
        dumper = NetworkDumper(writer, net)
        # --------------------------------------------------------------------------------------------------------------
# **********************************************************************************************************************


# ************************ Set up for reference network ****************************************************************
    """
    'load_network_spec' is defined in networks/__init__.py
    Load network from path of .yaml file ('args.network_spec') describing network architecture
    """
    ref_convs = load_network_spec(args.ref_network_spec)
    # 'ReferenceConvNetwork' is class defined in networks/__init__.py
    # Load reference convolution network
    ref_net = ReferenceConvNetwork(args, ref_im_dims, ref_convs, loss, opt, ref_opt_param, target_size)
    ref_net = ref_net.to(device)
    # Create a new 3D array has size [n_tests_total, n_test, len(net.dcll_slices)]
    acc_test_ref = np.empty([n_tests_total, n_test])
# **********************************************************************************************************************

# *************************Save info of 'log_dir' and 'args' *********************************************************************************************
    """
    Save information of logging directory and 'args' to .txt file in results directory.
    """
    if not args.no_save:
        # 'annotate()' is defined in dcll/experiment_tools.py
        # Create a text file 'filename' in directory 'out_dir', which saves content 'text'
        annotate(out_dir, text=log_dir, filename='log_dir.txt')
        annotate(out_dir, text=str(args), filename='args.txt')
        with open(os.path.join(out_dir, 'args.pkl'), 'wb') as fp:
            # 'pickle.dump' serializes a python object hierarchy and returns the bytes object of the serialized object.
            # The 'vars()' function returns the __dict__ attribute of the given object.
            pickle.dump(vars(args), fp)
        save_source(out_dir)
# **********************************************************************************************************************

# **************************** Load train and test data from .hdf5 file ************************************************
    """
    - 'get_loader' is func 'get_radio_ml_loader' defined in data/load_radio_ml.py
    - '**kwargs' as a dictionary saving keyworded variable which can be extracted based on keyword 
    - 'gen_train' has size (575.016, 1024, 2). 90% of truncated data (2048 ex each) of 24*13 [class,SNR] pairs
    - 'gen_test' has size (63960, 1024, 2). 10% of truncated data (2048 ex each) of 24*13 [class,SNR] pairs
    """
    train_data = get_loader(args.batch_size, train=True, **get_loader_kwargs)
    gen_train = iter(train_data)
    gen_test = iter(get_loader(args.batch_size_test, train=False, **get_loader_kwargs))
# **********************************************************************************************************************


    # 'next()' is used to fetch next item from the collection
    all_test_data = [next(gen_test) for i in range(n_test)]
    all_test_data = [(samples, to_one_hot(labels, target_size)) for (samples, labels) in all_test_data]

    label_train_counts = np.zeros(target_size, dtype=int)       # 'target_size = 24'


# **********************************************************************************************************************
    # 'arg.n_steps' is nb of training loops. Default value: 10000
    for step in range(args.n_steps):
    # -----------------------Adjust leaning rate------------------------------------------------------------------------
        if ((step + 1) % 1000) == 0:
            if not args.just_ref:
                for i in range(len(net.dcll_slices)):
                    net.dcll_slices[i].optimizer.param_groups[-1]['lr'] /= 2
                net.dcll_slices[-1].optimizer2.param_groups[-1]['lr'] /= 2
            ref_net.optim.param_groups[-1]['lr'] /= 2
            print('- Adjusting learning rates')
    # ------------------------------------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
        """  
        - 'gen_train' has size (575.016, 1024, 2). 90% of truncated data (2048 ex each) of 24*13 [class,SNR] pairs
        - 'input' will have size tensor(64, 2, 1, 1024)
        - 'label' will have size tensor(64,24)
        """
        try:
            input, labels = next(gen_train) # Slice over 'gen_train'
        except StopIteration:
            gen_train = iter(train_data) # iter() function creates an object which can be iterated one element at a time
            input, labels = next(gen_train)
        for label in labels:
            label_train_counts[label] += 1
        labels = to_one_hot(labels, target_size)
    # ------------------------------------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
        if not args.just_ref:
            # 'n_iters': how many ms do we present a sample during classification. Default value: 1024
            n_iters_sampled = n_iters  # np.random.randint(args.burnin + 1, n_iters + 1)
            to_st_train_kwargs['max_duration'] = n_iters_sampled

            """Convert input data to spiking input"""
            # 'to_spike_train' is used to convert each I/Q sample to a spike in the I/Q plane over time
            input_spikes, labels_spikes = to_spike_train(input, labels, **to_st_train_kwargs)
            input_spikes = torch.Tensor(input_spikes).to(device)
            labels_spikes = torch.Tensor(labels_spikes).to(device)
            """-----------------------------------"""

            """TRAINING"""
            net.reset()
            net.train()
            # input_spikes size: torch.Size([1024, 64, 1, 128, 128])
            # labels_spikes size: torch.Size([1024, 64, 24])
            for sim_iteration in range(n_iters_sampled): # 'n_iters_sampled' = 1024
                net.learn(x=input_spikes[sim_iteration],
                          labels=labels_spikes[sim_iteration])
            acc_train = net.accuracy(labels_spikes)
            step_str = str(step).zfill(5)   # not important, used to create a 5-digit number for print
            print('- [TRAINING] Loop {}, \t Accuracy {}'.format(step_str, acc_train))
            """-----------------------------------"""

        """ - 'input' will have size tensor(64, 2, 1, 1024)
            - 'label' will have size tensor(64,24) """
        ref_input = torch.Tensor(input).to(device).reshape(-1, *ref_im_dims)   # 'ref_im_dims' = (2,1,1024)
                                                                               # resize ref_input to (...,2,1,1024)
        ref_label = torch.Tensor(labels).to(device)

        ref_net.train()
        ref_net.learn(x=ref_input, labels=ref_label)

        """ TESTING """
        """ Do the test after 20 loop of training """
        if (step % args.n_test_interval) == 0:          # 'args.n_test_interval' = 20
            test_idx = step // args.n_test_interval     # 'step' range from 1 to 10000
            for i, test_data in enumerate(all_test_data):
                if not args.just_ref:
                    # ---------------------Create a spike input of test dataset-----------------------------------------
                    # 'test_input' size torch.Size([1024, 64, 1, 128, 128])
                    # 'test_labels' size torch.Size([1024, 64, 24])
                    test_input, test_labels = to_spike_train(*test_data,
                                                             **to_st_test_kwargs)

                    # test_input = torch.Tensor(test_input)
                    # test_labels = torch.Tensor(test_labels)
                    # print('test_input size',test_input.size())
                    # print('test_labels size',test_labels.size())
                    # --------------------------------------------------------------------------------------------------

                    # --------------------------------------------------------------------------------------------------
                    try:
                        test_input = torch.Tensor(test_input).to(device)
                    except RuntimeError as e:
                        print('- Exception: ' + str(e) +
                              '. Try to decrease your batch_size_test with the --batch_size_test argument.')
                        raise
                    test_labels = torch.Tensor(test_labels).to(device)
                    # --------------------------------------------------------------------------------------------------

                    # --------------------------------------------------------------------------------------------------
                    net.reset()
                    net.eval()
                    for sim_iteration in range(n_iters_test):
                        net.test(x=test_input[sim_iteration])
                    acc_test[test_idx, i, :] = net.accuracy(test_labels)
                    # --------------------------------------------------------------------------------------------------
                    if i == 0:
                        net.write_stats(writer, step, comment='_batch_'+str(i))

                test_ref_input = torch.Tensor(test_data[0]).to(device).reshape(-1, *ref_im_dims)
                test_ref_label = torch.Tensor(test_data[1]).to(device)

                ref_net.eval()
                ref_net.test(test_ref_input)
                acc_test_ref[test_idx, i] = ref_net.accuracy(test_ref_label)

                if i == 0:
                    ref_net.write_stats(writer, step)


            if not args.just_ref and not args.no_save:
                np.save(os.path.join(out_dir, 'acc_test.npy'), acc_test)
                np.save(os.path.join(out_dir, 'acc_test_ref.npy'), acc_test_ref)

                # Save network parameters
                save_path = os.path.join(out_dir, 'parameters_{}.pth'.format(step))
                torch.save(net.cpu().state_dict(), save_path)
                net = net.to(device)
                print('-' * 120)
                print('- Saved network parameters to `%s`.' % save_path)
                print('-' * 120)

            if not args.just_ref:
                acc = np.mean(acc_test[test_idx], axis=0)
            else:
                acc = 'N/A'
            acc_ref = np.mean(acc_test_ref[test_idx], axis=0)
            step_str = str(step).zfill(5)
            print('-' * 120)
            print('- [TESTING]  Loop {}, \t Accuracy {}, \t Accuracy_Ref {}'.format(step_str, acc, acc_ref))
            label_train_percentages = label_train_counts / np.sum(label_train_counts) * 100
            print('- Label train percentages: ', np.array2string(label_train_percentages, max_line_width=300, precision=1))
            print('-' * 120)

    writer.close()
