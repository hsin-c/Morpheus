# SPDX-FileCopyrightText: Copyright (c) 2022-2023, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Original Source: https:#github.com/AlliedToasters/dfencoder
#
# Original License: BSD-3-Clause license, included below

# Copyright (c) 2019, Michael Klear.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met:
#
#     * Redistributions of source code must retain the above copyright
#        notice, this list of conditions and the following disclaimer.
#
#     * Redistributions in binary form must reproduce the above
#        copyright notice, this list of conditions and the following
#        disclaimer in the documentation and/or other materials provided
#        with the distribution.
#
#     * Neither the name of the dfencoder Developers nor the names of any
#        contributors may be used to endorse or promote products derived
#        from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import gc
import logging
from collections import OrderedDict
from collections import defaultdict

import numpy as np
import pandas as pd
import torch
import tqdm

from .ae_module import AEModule
from .dataframe import EncoderDataFrame
from .dataloader import DatasetFromDataframe
from .distributed_ae import DistributedAutoEncoder
from .logging import BasicLogger
from .logging import IpynbLogger
from .logging import TensorboardXLogger
from .scalers import GaussRankScaler
from .scalers import ModifiedScaler
from .scalers import NullScaler
from .scalers import StandardScaler

LOG = logging.getLogger(__name__)


def _ohe(input_vector, dim, device="cpu"):
    """Does one-hot encoding of input vector.

    Parameters
    ----------
    input_vector : torch.Tensor
        The input tensor to be one-hot encoded.
    dim : int
        The dimension of the one-hot encoded output.
    device : str, optional
        The device on which to place the output tensor, by default "cpu".

    Returns
    -------
    torch.Tensor
        The one-hot encoded output tensor of shape (batch_size, dim).
    """
    batch_size = len(input_vector)
    nb_digits = dim

    y = input_vector.reshape(-1, 1)
    y_onehot = torch.FloatTensor(batch_size, nb_digits).to(device)

    y_onehot.zero_()
    y_onehot.scatter_(1, y, 1)

    return y_onehot


class AutoEncoder(torch.nn.Module):

    def __init__(
            self,
            *,
            encoder_layers=None,
            decoder_layers=None,
            encoder_dropout=None,
            decoder_dropout=None,
            encoder_activations=None,
            decoder_activations=None,
            activation='relu',
            min_cats=10,
            swap_p=.15,
            lr=0.01,
            batch_size=256,
            eval_batch_size=1024,
            optimizer='adam',
            amsgrad=False,
            momentum=0,
            betas=(0.9, 0.999),
            dampening=0,
            weight_decay=0,
            lr_decay=None,
            nesterov=False,
            verbose=False,
            device=None,
            distributed_training=False,
            logger='basic',
            logdir='logdir/',
            run=None,
            scaler='standard',
            patience=5,
            preset_cats=None,
            preset_numerical_scaler_params=None,
            binary_feature_list=None,
            loss_scaler='standard',  # scaler for the losses (z score)
            **kwargs):
        super().__init__(**kwargs)

        self.numeric_fts = OrderedDict()
        self.binary_fts = OrderedDict()
        self.categorical_fts = OrderedDict()
        self.cyclical_fts = OrderedDict()
        self.feature_loss_stats = dict()

        if device is None:
            self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        else:
            self.device = device

        self.distributed_training = distributed_training

        self.model = AEModule(
            verbose=verbose,
            encoder_layers=encoder_layers,
            decoder_layers=decoder_layers,
            encoder_dropout=encoder_dropout,
            decoder_dropout=decoder_dropout,
            encoder_activations=encoder_activations,
            decoder_activations=decoder_activations,
            activation=activation,
            device=self.device,
            **kwargs,
        )
        self.optimizer = optimizer
        self.optim = None
        self.lr = lr
        self.lr_decay = lr_decay

        self.min_cats = min_cats
        self.preset_cats = preset_cats
        self.preset_numerical_scaler_params = preset_numerical_scaler_params

        self.swap_p = swap_p
        self.batch_size = batch_size
        self.eval_batch_size = eval_batch_size

        self.numeric_output = None
        self.binary_output = None

        # `num_names` is a list of column names that contain numeric data (int & float fields).
        self.num_names = []
        self.bin_names = binary_feature_list

        self.amsgrad = amsgrad
        self.momentum = momentum
        self.betas = betas
        self.dampening = dampening
        self.weight_decay = weight_decay
        self.nesterov = nesterov

        self.mse = torch.nn.modules.loss.MSELoss(reduction='none')
        self.bce = torch.nn.modules.loss.BCELoss(reduction='none')
        self.cce = torch.nn.modules.loss.CrossEntropyLoss(reduction='none')

        self.verbose = verbose
        if self.verbose:
            LOG.setLevel(logging.DEBUG)
        else:
            LOG.setLevel(logging.INFO)

        self.logger = logger
        self.logdir = logdir
        self.run = run
        self.scaler = scaler
        self.patience = patience

        # scaler class used to scale losses and collect loss stats
        self.loss_scaler_str = loss_scaler
        self.loss_scaler = self._get_scaler(loss_scaler)

    def _get_scaler(self, name):
        scalers = {
            'standard': StandardScaler,
            'gauss_rank': GaussRankScaler,
            'modified': ModifiedScaler,
            None: NullScaler,
            'none': NullScaler
        }
        return scalers[name]

    def _init_numeric(self, df=None):
        """Initializes the numerical features of the model by either using preset numerical scaler parameters
        or by using the input data.

        Parameters
        ----------
        df : pandas DataFrame, optional
            The input data to be used to initialize the numerical features, by default None.
            If not provided, self.preset_numerical_scaler_params must be provided.

        Raises
        ------
        ValueError
            If both df and self.preset_numerical_scaler_params are not provided.
        """
        if df is None and self.preset_numerical_scaler_params is None:
            raise ValueError("Either `df` or `self.preset_numerical_scaler_params` needs to be provided.")

        if self.preset_numerical_scaler_params:
            LOG.debug("Using self.preset_numerical_scaler_params to override the numerical scalers...")
            for ft, scaler_params in self.preset_numerical_scaler_params.items():
                # scaler_params should include the following keys: scaler_type, scaler_attr_dict, mean, std
                scaler = self._get_scaler(scaler_params.get("scaler_type", "gauss_rank"))()
                for k, v in scaler_params["scaler_attr_dict"].items():
                    # scaler_params['scaler_attr_dict'] should be a dict including all the class attributes of a fitted scaler class
                    setattr(scaler, k, v)
                feature = {
                    "mean": scaler_params["mean"],
                    "std": scaler_params["std"],
                    "scaler": scaler,
                }
                self.numeric_fts[ft] = feature
        else:
            # initialize using a dataframe
            numeric = list(df.select_dtypes(include=[int, float]).columns)

            if isinstance(self.scaler, str):
                scalers = {ft: self.scaler for ft in numeric}
            elif isinstance(self.scaler, dict):
                scalers = self.scaler

            for ft in numeric:
                Scaler = self._get_scaler(scalers.get(ft, "gauss_rank"))
                feature = {
                    "mean": df[ft].mean(),
                    "std": df[ft].std(),
                    "scaler": Scaler(),
                }
                feature["scaler"].fit(df[ft][~df[ft].isna()].values)
                self.numeric_fts[ft] = feature

        self.num_names = list(self.numeric_fts.keys())

    def _init_cats(self, df):
        objects = list(df.select_dtypes(include=object).columns)
        for ft in objects:
            feature = {}
            vl = df[ft].value_counts()
            cats = list(vl[vl >= self.min_cats].index)
            feature['cats'] = cats
            self.categorical_fts[ft] = feature

    def _init_binary(self, df=None):
        """Initializes the binary features of the model.

        Parameters
        ----------
        df : pandas.DataFrame, optional
            The input data to be used to initialize the binary features, by default None.

        Raises
        ------
        ValueError
            If both df and self.bin_names are not provided.
        """
        if df is None and self.bin_names is None:
            raise ValueError(
                "Need to provide one of the two params (df or binary_features). "
                "If there is no binary feartures, try providing the parameter `binary_feature_list=[]` during class init."
            )

        if self.bin_names is not None:
            LOG.debug("Using the preset binary feature list `self.bin_names` to initialize the binary features...")
            binaries = self.bin_names
        else:
            binaries = list(df.select_dtypes(include=bool).columns)
            self.bin_names = binaries

        for ft in self.binary_fts:
            feature = self.binary_fts[ft]
            for i, cat in enumerate(feature['cats']):
                feature[cat] = bool(i)
        for ft in binaries:
            feature = dict()
            feature['cats'] = [True, False]
            feature[True] = True
            feature[False] = False
            self.binary_fts[ft] = feature

    def _init_features(self, df=None):
        """Initializea the features of different types.
        `df` is required if any of `preset_cats`, `preset_numerical_scaler_params`, and `binary_feature_list` are not provided 
        at model initialization.
        
        Parameters
        ----------
        df : pandas.DataFrame, optional
            dataframe used to compute and extract feature information, by default None
            
        Raises
        ------
        ValueError
            if any of `preset_cats`, `preset_numerical_scaler_params`, and `binary_feature_list` are not provided at model initialization
        """
        if df is None:
            # all feature information needs to be fed into the model at initialization in order to build the 
            # model without `df` as an input 
            if self.preset_cats is None or self.bin_names is None or self.preset_numerical_scaler_params is None:
                raise ValueError('Fail to intitialize the features without an input dataframe. '
                'All of `preset_cats`, `preset_numerical_scaler_params`, and `binary_feature_list` need to be provided during model '
                'initialization for this function to work without an input `df`.'
                )

        if self.preset_cats is not None:
            LOG.debug('Using the preset categories `self.preset_cats` to initialize the categories features...')
            self.categorical_fts = self.preset_cats
        else:
            self._init_cats(df)
        self._init_numeric(df)
        self._init_binary(df)

    def prepare_df(self, df):
        """Does data preparation on copy of input dataframe.

        Parameters
        ----------
        df : pandas.DataFrame
            The pandas dataframe to process

        Returns
        -------
        pandas.DataFrame
            A processed copy of df.
        """
        output_df = EncoderDataFrame()
        for ft in self.numeric_fts:
            feature = self.numeric_fts[ft]
            col = df[ft].fillna(feature['mean'])
            trans_col = feature['scaler'].transform(col.values)
            trans_col = pd.Series(index=df.index, data=trans_col)
            output_df[ft] = trans_col

        for ft in self.binary_fts:
            feature = self.binary_fts[ft]
            output_df[ft] = df[ft].apply(lambda x: feature.get(x, False))

        for ft in self.categorical_fts:
            feature = self.categorical_fts[ft]
            col = pd.Categorical(df[ft], categories=feature['cats'] + ['_other'])
            col = col.fillna('_other')
            output_df[ft] = col

        return output_df

    def _build_model(self, df=None, rank=None):
        """Builds the autoencoder model using either the given dataframe or the preset feature information for metadata.
        If distributed training is enabled (self.distributed_training is True), wraps the pytorch module with DDP.
        User should not need to call this function directly as it's called before training in the fit() functions.

        Parameters
        ----------
        df : pandas.DataFrame, optional
            The input dataframe to be used to infer metadata, by default None.
        rank : int, optional
            Rank of the process being used for distributed training. Used only if self.distributed_training is True, by default None.

        Raises
        ------
        ValueError
            If rank is nor provided in distributed training mode.
        """
        LOG.debug('Building model...')

        # get metadata from features
        self._init_features(df)

        self.model.build(self.numeric_fts, self.binary_fts, self.categorical_fts)
        if self.distributed_training:
            if rank is None:
                raise ValueError('`rank` missing. `rank` is required for distributed training.')

            self.model._ddp_params_and_buffers_to_ignore = []
            if len(self.numeric_fts) == 0:
                # if there is no numeric feature, ignore this layer to avoid errors while syncing parameters across gpus
                self.model._ddp_params_and_buffers_to_ignore.append('numeric_output.weight')
            if len(self.binary_fts) == 0:
                # if there is no binary feature, ignore this layer to avoid errors while syncing parameters across gpus
                self.model._ddp_params_and_buffers_to_ignore.append('binary_output.weight')

            self.model = DistributedAutoEncoder(self.model, device_ids=[rank], output_device=rank)

        self._build_optimizer()
        if self.lr_decay is not None:
            self.lr_decay = torch.optim.lr_scheduler.ExponentialLR(self.optim, self.lr_decay)

        self._build_logger()

        LOG.debug('done!')

    def _build_optimizer(self):
        lr = self.lr
        params = self.model.parameters()
        if self.optimizer == 'adam':
            optim = torch.optim.Adam(params,
                                     lr=self.lr,
                                     amsgrad=self.amsgrad,
                                     weight_decay=self.weight_decay,
                                     betas=self.betas)
        elif self.optimizer == 'sgd':
            optim = torch.optim.SGD(
                params,
                lr,
                momentum=self.momentum,
                nesterov=self.nesterov,
                dampening=self.dampening,
                weight_decay=self.weight_decay,
            )
        else:
            raise ValueError('Provided optimizer unsupported. Supported optimizers include: [adam, sgd].')
        self.optim = optim

    def _build_logger(self):
        """ Initializes the logger to be used for training the model."""
        cat_names = list(self.categorical_fts.keys())
        fts = self.num_names + self.bin_names + cat_names
        if self.logger == 'basic':
            self.logger = BasicLogger(fts=fts)
        elif self.logger == 'ipynb':
            self.logger = IpynbLogger(fts=fts)
        elif self.logger == 'tensorboard':
            self.logger = TensorboardXLogger(logdir=self.logdir, run=self.run, fts=fts)

    def compute_targets(self, df):
        num = torch.tensor(df[self.num_names].values).float().to(self.device)
        bin = torch.tensor(df[self.bin_names].astype(int).values).float().to(self.device)
        codes = []
        for ft in self.categorical_fts:
            code = torch.tensor(df[ft].cat.codes.astype(int).values).to(self.device)
            codes.append(code)
        return num, bin, codes

    def _encode_input(self, df):
        """
        Handles raw df inputs.
        Passes categories through embedding layers.
        """
        num, bin, codes = self.compute_targets(df)
        embeddings = []
        for i, embedding_layer in enumerate(self.model.categorical_embedding.values()):
            emb = embedding_layer(codes[i])
            embeddings.append(emb)
        return [num], [bin], embeddings

    def build_input_tensor(self, df):
        num, bin, embeddings = self._encode_input(df)
        x = torch.cat(num + bin + embeddings, dim=1)
        return x

    def preprocess_train_data(self, df, shuffle_rows_in_batch=True):
        """ Wrapper function round `self.preprocess_data` feeding in the args suitable for a training set."""
        return self.preprocess_data(
            df,
            shuffle_rows_in_batch=shuffle_rows_in_batch,
            include_original_input_tensor=False,
            include_swapped_input_by_feature_type=False,
        )

    def preprocess_validation_data(self, df, shuffle_rows_in_batch=False):
        """ Wrapper function round `self.preprocess_data` feeding in the args suitable for a validation set."""
        return self.preprocess_data(
            df,
            shuffle_rows_in_batch=shuffle_rows_in_batch,
            include_original_input_tensor=True,
            include_swapped_input_by_feature_type=True,
        )

    def preprocess_data(
        self,
        df,
        shuffle_rows_in_batch,
        include_original_input_tensor,
        include_swapped_input_by_feature_type,
    ):
        """Preprocesses a pandas dataframe `df` for input into the autoencoder model.

        Parameters
        ----------
        df : pandas.DataFrame
            The input dataframe to preprocess.
        shuffle_rows_in_batch : bool
            Whether to shuffle the rows of the dataframe before processing.
        include_original_input_tensor : bool
            Whether to process the df into an input tensor without swapping and include it in the returned data dict.
            Note. Training required only the swapped input tensor while validation can use both.
        include_swapped_input_by_feature_type : bool
            Whether to process the swapped df into num/bin/cat feature tensors and include them in the returned data dict. 
            This is useful for baseline performance evaluation for validation.

        Returns
        -------
        Dict[str, Union[int, torch.Tensor]]
            A dict containing the preprocessed input data and targets by feature type.
        """
        df = self.prepare_df(df)
        if shuffle_rows_in_batch:
            df = df.sample(frac=1.0)
        df = EncoderDataFrame(df)
        swapped_df = df.swap(likelihood=self.swap_p)
        swapped_input_tensor = self.build_input_tensor(swapped_df)
        num_target, bin_target, codes = self.compute_targets(df)

        preprocessed_data = {
            'input_swapped': swapped_input_tensor,
            'num_target': num_target,
            'bin_target': bin_target,
            'cat_target': codes,
            'size': len(df),
        }

        if include_original_input_tensor:
            preprocessed_data['input_original'] = self.build_input_tensor(df)

        if include_swapped_input_by_feature_type:
            num_swapped, bin_swapped, codes_swapped = self.compute_targets(swapped_df)
            preprocessed_data['num_swapped'] = num_swapped
            preprocessed_data['bin_swapped'] = bin_swapped
            preprocessed_data['cat_swapped'] = codes_swapped

        return preprocessed_data

    def _compute_loss_from_targets(self, num, bin, cat, num_target, bin_target, cat_target, should_log=True, _id=False):
        """Computes the loss from targets.

        Parameters
        ----------
        num : torch.Tensor
            numerical data tensor
        bin : torch.Tensor
            binary data tensor
        cat : List[torch.Tensor]
            list of categorical data tensors
        num_target : torch.Tensor
            target numerical data tensor
        bin_target : torch.Tensor
            target binary data tensor
        cat_target : List[torch.Tensor]
            list of target categorical data tensors
        should_log : bool, optional
            whether to log the loss in self.logger, by default True
        _id : bool, optional
            whether the current step is an id validation step (for logging), by default False

        Returns
        -------
        Tuple[Union[float, List[float]]]
            A tuple containing the mean mse/bce losses, list of mean cce losses, and mean net loss
        """
        if should_log:
            if self.logger is not None:
                should_log = True
            else:
                should_log = False
        net_loss = []
        mse_loss = self.mse(num, num_target)
        net_loss += list(mse_loss.mean(dim=0).cpu().detach().numpy())
        mse_loss = mse_loss.mean()
        bce_loss = self.bce(bin, bin_target)

        net_loss += list(bce_loss.mean(dim=0).cpu().detach().numpy())
        bce_loss = bce_loss.mean()
        cce_loss = []
        for i, ft in enumerate(self.categorical_fts):
            loss = self.cce(cat[i], cat_target[i])
            loss = loss.mean()
            cce_loss.append(loss)
            val = loss.cpu().item()
            net_loss += [val]
        if should_log:
            if self.training:
                self.logger.training_step(net_loss)
            elif _id:
                self.logger.id_val_step(net_loss)
            elif not self.training:
                self.logger.val_step(net_loss)

        net_loss = np.array(net_loss).mean()
        return mse_loss, bce_loss, cce_loss, net_loss

    def _do_backward(self, mse, bce, cce):
        # running `backward()` seperately on mse/bce/cce is equivalent to summing them up and run `backward()` once
        loss_fn = mse + bce
        for ls in cce:
            loss_fn += ls
        loss_fn.backward()

    def _create_stat_dict(self, a):
        scaler = self.loss_scaler()
        scaler.fit(a)
        return {'scaler': scaler}

    def fit(
        self,
        train_data,
        epochs=1,
        val_data=None,
        run_validation=False,
        use_val_for_loss_stats=False,
        rank=None,
        world_size=None,
    ):
        """ Does training with the provided parameters in the specified mode (self.distrivuted_training).
        Actions:
        1. Performs value checks on the input
        2. Converts the train_data to supported type if necessary
        3. Build the model if it hasn't been built
        4. Call the core `_fit_data` function to train the model.

        Parameters
        ----------
        train_data : pandas.DataFrame or torch.utils.data.Dataset or torch.utils.data.DataLoader 
            Data for training.
        epochs : int, optional
            Number of epochs to run training, by default 1.
        val_data : pandas.DataFrame (centralized) or torch.utils.data.DataLoader (distributed), optional
            Data for validation and computing loss stats, by default None.
        run_validation : bool, optional
            Whether to collect validation loss for each epoch during training, by default False.
        use_val_for_loss_stats : bool, optional
            whether to use the validation set for loss statistics collection (for z score calculation), by default False.
        rank : int, optional
            The rank of the current process, by default None. Required for distributed training.
        world_size : int, optional
            The total number of processes, by default None. Required for distributed training.

        Raises
        ------
        TypeError
            If `train_data` is not a pandas DataFrame, a DataLoader, or a Dataset.
        ValueError
            If `rank` and `world_size` are not provided in distributed training mode.
            If `rank` and `world_size` are not `None` in centralized training mode.
            If `run_validation` or `use_val_for_loss_stats` is `True` but `val_data` is not provided.
        """
        if not isinstance(train_data, (pd.DataFrame, torch.utils.data.DataLoader, torch.utils.data.Dataset)):
            raise TypeError(
                "`train_data` needs to be a pandas DataFrame, a DataLoader, or a Dataset."
                f" `train_data` is currently of type: {type(train_data)}")
        
        if self.distributed_training and (rank is None or world_size is None):
            raise ValueError('`rank` and `world_size` must be provided for distributed training.')

        if not self.distributed_training and (rank is not None or world_size is not None):
            raise ValueError('`rank` and `world_size` must be None for centralized training.')

        if run_validation and val_data is None:
            raise ValueError("`run_validation` is set to True but the validation set (val_data) is not provided.")

        if use_val_for_loss_stats and val_data is None:
            raise ValueError("`use_val_for_loss_stats` is set to True but the validation set (val_data) is not provided.")

        # If train_data is in the format of a pandas df, wrap it by a dataset
        train_df = None
        if isinstance(train_data, pd.DataFrame):
            train_df = train_data
            train_data = DatasetFromDataframe.get_train_dataset(self, train_data)

        # If val_data is in the format of a pandas df, wrap it by a dataset
        if isinstance(val_data, pd.DataFrame):
            val_data = DatasetFromDataframe.get_validation_dataset(self, val_data)
        
        rank = rank if self.distributed_training else 0  # set rank to 0 in centralized training mode
        world_size = world_size if self.distributed_training else 1  # set rank to 0 in centralized training mode

        if self.optim is None:
            self._build_model(df=train_df, rank=rank)

        self._fit_data(
            train_data=train_data,
            epochs=epochs,
            val_data=val_data,
            run_validation=run_validation,
            use_val_for_loss_stats=use_val_for_loss_stats,
            rank=rank,
            world_size=world_size,
        )
    
    def _fit_data(
        self,
        train_data,
        rank,
        world_size,
        epochs=1,
        val_data=None,
        run_validation=False,
        use_val_for_loss_stats=True,
    ):
        """ Fit the model with early stopping based on validation loss. Train in the specified mode (indicated 
        by self.distributed_training).
        If run_validation is True, the val_data will be used for validation during training and early stopping 
        will be applied based on patience argument.

        Parameters
        ----------
        train_data : torch.utils.data.Dataset or torch.utils.data.DataLoader
            data object of training data
        rank : int
            the rank of the current process
        world_size : int
            the total number of processes
        epochs : int, optional
            the number of epochs to train for, by default 1
        val_data : torch.utils.data.Dataset or torch.utils.data.DataLoader, optional
            the validation data (with __iter__() that yields a batch at a time), by default None
        run_validation : bool, optional
            whether to perform validation during training, by default False
        use_val_for_loss_stats : bool, optional
            whether to populate loss stats in the main process (rank 0) for z-score calculation using the validation set.
            If set to False, loss stats would be populated using the train_dataloader, which can be slow due to data size.
            By default True as using the validation set to populate loss stats is strongly recommended (for both efficiency 
            and model efficacy).
        """
        rank_str = f"[Rank {rank}] " if self.distributed_training else "" # for logging

        is_main_process = rank == 0
        should_run_validation = (run_validation and val_data is not None)
        if self.patience and not should_run_validation:
            LOG.warning(
                f"Not going to perform early-stopping. self.patience(={self.patience}) is provided for early-stopping"
                " but validation is not enabled. Please set `run_validation` to True and provide a `val_data` to"
                " enable early-stopping.")

        if is_main_process and should_run_validation:
            LOG.debug('Validating during training. Computing baseline performance...')
            baseline = self._compute_baseline_performance_from_dataset(val_data)

            if isinstance(self.logger, BasicLogger):
                self.logger.baseline_loss = baseline

            LOG.debug(f'Baseline loss: {round(baseline, 4)}')

        # early stopping
        count_es = 0
        last_val_loss = float('inf')
        should_early_stop = False
        for epoch in range(epochs):
            LOG.debug(f'{rank_str}Training epoch {epoch + 1}...')

            if self.distributed_training:
                # if we are using DistributedSampler, we have to tell it which epoch this is
                train_data.sampler.set_epoch(epoch)

            train_loss_sum = 0
            train_loss_count = 0
            for data_d in train_data:
                loss = self._fit_batch(**data_d['data'])

                train_loss_count += 1
                train_loss_sum += loss

            if self.lr_decay is not None:
                self.lr_decay.step()

            if is_main_process and should_run_validation:
                # run validation
                curr_val_loss = self._validate_dataset(val_data, rank)
                LOG.debug(f'{rank_str}Loss: {round(last_val_loss, 4)}->{round(curr_val_loss, 4)}')

                if self.patience:  # early stopping
                    if curr_val_loss > last_val_loss:
                        count_es += 1
                        LOG.debug(f'\t{rank_str}Loss went up. Early stop count: {count_es}')

                        if count_es >= self.patience:
                            LOG.debug(f'\t{rank_str}Early stopping: early stop count({count_es}) >= patience({self.patience})')
                            should_early_stop = True
                    else:
                        LOG.debug(f'\t{rank_str}Loss went down. Reset count for earlystop to 0')
                        count_es = 0

                    last_val_loss = curr_val_loss

            self.logger.end_epoch()

            if self.distributed_training:
                # sync early stopping info so the early stopping decision can be passed from the main process to other processes
                # making a list to create enough room to store the collected objects
                early_stpping_state = [None for _ in range(world_size)]  
                torch.distributed.all_gather_object(early_stpping_state, should_early_stop)
                should_early_stop_synced = early_stpping_state[0]  # take the state of the main process
                LOG.debug(f'\t{rank_str}(Epoch {epoch+1}) Synced early stopping state: {should_early_stop_synced}')
            else:
                # no need to sync for centralized training
                should_early_stop_synced = should_early_stop

            if should_early_stop_synced is True:
                LOG.debug(f'{rank_str}Early stopped.')
                break

        if is_main_process:
            # Run loss collection only on the main process (currently do not support distributed loss collection)
            if use_val_for_loss_stats:
                dataset_for_loss_stats = val_data
            # use training set for loss stats collection
            elif isinstance(train_data, torch.utils.data.DataLoader):
                dataset_for_loss_stats = train_data.dataset  # grab only the dataset to avoid distriburted sampling
                dataset_for_loss_stats.convert_to_validation(self)
            else:
                dataset_for_loss_stats = train_data
                dataset_for_loss_stats.convert_to_validation(self)
            self._populate_loss_stats_from_dataset(dataset_for_loss_stats)

    def _fit_batch(self, input_swapped, num_target, bin_target, cat_target, **kwargs):
        """Forward pass on the input_swapped, then computes the losses from the predicted outputs and actual targets, performs 
        backpropagation, updates the model parameters, and returns the net loss.

        Parameters
        ----------
        input_swapped : torch.Tensor
            input tensor of shape (batch_size, feature vector size), some values are randomly swapped for denoising
        num_target : torch.Tensor
            tensor of shape (batch_size, numerical feature count) with numerical targets
        bin_target : torch.Tensor
            tensor of shape (batch_size, binary feature count) with binary targets
        cat_target : List[torch.Tensor]
            list of size (categorical feature count), each entry is a 1-d tensor of shape (batch_size) containing the categorical 
            targets

        Returns
        -------
        float
            total loss computed as the weighted sum of the mse, bce and cce losses
        """
        self.train()
        num, bin, cat = self.model(input_swapped)
        mse, bce, cce, net_loss = self._compute_loss_from_targets(
            num=num,
            bin=bin,
            cat=cat,
            num_target=num_target,
            bin_target=bin_target,
            cat_target=cat_target,
            should_log=True,
        )
        self._do_backward(mse, bce, cce)
        self.optim.step()
        self.optim.zero_grad()
        return net_loss

    def _compute_baseline_performance_from_dataset(self, val_dataset):
        self.eval()
        loss_sum = 0
        sample_count = 0
        with torch.no_grad():
            for data_d in val_dataset:
                curr_batch_size = data_d['data']['size']
                loss = self._compute_batch_baseline_performance(**data_d['data'])
                loss_sum += loss
                sample_count += curr_batch_size

        baseline = loss_sum / sample_count
        return baseline

    def _compute_batch_baseline_performance(
            self,
            num_swapped,
            bin_swapped,
            cat_swapped,
            num_target,
            bin_target,
            cat_target,
            **kwargs,  # ignore other unused kwargs
    ):
        bin_swapped += ((bin_swapped == 0).float() * 0.05)
        bin_swapped -= ((bin_swapped == 1).float() * 0.05)
        codes_swapped_ohe = []
        for cd, feature in zip(cat_swapped, self.categorical_fts.values()):
            dim = len(feature['cats']) + 1
            cd_ohe = _ohe(cd, dim, device=self.device) * 5
            codes_swapped_ohe.append(cd_ohe)

        _, _, _, net_loss = self._compute_loss_from_targets(
            num=num_swapped,
            bin=bin_swapped,
            cat=codes_swapped_ohe,
            num_target=num_target,
            bin_target=bin_target,
            cat_target=cat_target,
            should_log=False
        )
        return net_loss

    def _validate_dataset(self, val_dataset, rank=None):
        """Runs a validation loop on the given validation dataset, computing and returning the average loss of both the original
        input and the input with swapped values.

        Parameters
        ----------
        val_dataset : torch.utils.data.Dataset
            validation dataset to be used for validation
        rank : int, optional
            optional rank of the process being used for distributed training, used only for logging, by default None

        Returns
        -------
        float
            the average loss of the original input in the validation dataset
        """
        self.eval()
        with torch.no_grad():
            swapped_loss = []
            id_loss = []
            for data_d in val_dataset:
                orig_net_loss, net_loss = self._validate_batch(**data_d['data'])
                id_loss.append(orig_net_loss)
                swapped_loss.append(net_loss)

            swapped_loss = np.array(swapped_loss).mean()
            id_loss = np.array(id_loss).mean()

            rank_str = '' if rank is None else f'R{rank} '
            LOG.debug(f'\t{rank_str}Swapped loss: {round(swapped_loss, 4)}, Orig. loss: {round(id_loss, 4)}')
        return id_loss

    def _validate_batch(self, input_original, input_swapped, num_target, bin_target, cat_target, **kwargs):
        """Forward pass on the validation inputs, then computes the losses from the predicted outputs and actual targets,
        and returns the net loss.

        Parameters
        ----------
        input_original : torch.Tensor
            input tensor of shape (batch_size, feature vector size)
        input_swapped : torch.Tensor
             input tensor of shape (batch_size, feature vector size), some values are randomly swapped for denoising
        num_target : torch.Tensor
            tensor of shape (batch_size, numerical feature count) with numerical targets
        bin_target : torch.Tensor
            tensor of shape (batch_size, binary feature count) with binary targets
        cat_target : List[torch.Tensor]
            list of size (categorical feature count), each entry is a 1-d tensor of shape (batch_size) containing the categorical targets

        Returns
        -------
        Tuple[float]
            A tuple containing two floats:
            - orig_net_loss: the net loss when passing `input_original` through the model
            - net_loss: the net loss when passing the `input_swapped` through the model
        """
        orig_num, orig_bin, orig_cat = self.model(input_original)
        _, _, _, orig_net_loss = self._compute_loss_from_targets(
            num=orig_num,
            bin=orig_bin,
            cat=orig_cat,
            num_target=num_target,
            bin_target=bin_target,
            cat_target=cat_target,
            should_log=True,
            _id=True,
        )

        num, bin, cat = self.model(input_swapped)
        _, _, _, net_loss = self._compute_loss_from_targets(
            num=num,
            bin=bin,
            cat=cat,
            num_target=num_target,
            bin_target=bin_target,
            cat_target=cat_target,
            should_log=True,
        )
        return orig_net_loss, net_loss

    def _populate_loss_stats_from_dataset(self, dataset):
        """Populates the `self.feature_loss_stats` dict with feature losses computed using the provided dataset.

        Parameters
        ----------
        dataset : torch.utils.data.Dataset
            dataset to compute the feature losses for
        """
        self.eval()
        feature_losses = self._get_feature_losses_from_dataset(dataset)
        # populate loss stats
        for ft, losses in feature_losses.items():
            loss = losses.cpu().numpy()
            self.feature_loss_stats[ft] = self._create_stat_dict(loss)

    def _get_feature_losses_from_dataset(self, dataset):
        """Computes the feature losses for each feature in the model for a given dataset.

        Parameters
        ----------
        dataset : torch.utils.data.Dataset
            dataset to compute the feature losses for

        Returns
        -------
        Dict[str, torch.Tensor]
            a dict mapping feature names to a tensor of losses
        """
        feature_losses = defaultdict(list)
        with torch.no_grad():
            for data_d in dataset:
                batch_feature_losses = self._get_batch_feature_losses(**data_d['data'])
                for ft, loss_l in batch_feature_losses.items():
                    feature_losses[ft].append(loss_l)
        return {ft: torch.cat(tensor_l, dim=0) for ft, tensor_l in feature_losses.items()}

    def _get_batch_feature_losses(self, input_original, num_target, bin_target, cat_target, **kwargs):
        """Calculates the feature-wise losses for a batch of input data.

        Parameters
        ----------
        input_original : torch.Tensor
            input tensor of shape (batch_size, feature vector size)
        num_target : torch.Tensor
            tensor of shape (batch_size, numerical feature count) with numerical targets
        bin_target : torch.Tensor
            tensor of shape (batch_size, binary feature count) with binary targets
        cat_target : List[torch.Tensor]
            list of size (categorical feature count), each entry is a 1-d tensor of shape (batch_size) containing the categorical targets

        Returns
        -------
        Dict[str, torch.Tensor]
            a dict mapping feature names to a tensor of losses for the batch
        """
        batch_feature_losses = {}

        num, bin, cat = self.model(input_original)
        mse_loss = self.mse(num, num_target)
        for i, ft in enumerate(self.numeric_fts):
            batch_feature_losses[ft] = mse_loss[:, i]

        bce_loss = self.bce(bin, bin_target)
        for i, ft in enumerate(self.binary_fts):
            batch_feature_losses[ft] = bce_loss[:, i]

        for i, ft in enumerate(self.categorical_fts):
            loss = self.cce(cat[i], cat_target[i])
            batch_feature_losses[ft] = loss

        return batch_feature_losses

    def get_results_from_dataset(self, dataset, preloaded_df, return_abs=False):
        """Returns a pandas dataframe of inference results and losses for a given dataset.
        Note. this function requires the whole inference set to be in loaded into memory as a pandas df

        Parameters
        ----------
        dataset : torch.utils.data.Dataset
            dataset for inference
        preloaded_df : pd.DataFrame
            a pandas dataframe that contains the original data
        return_abs : bool, optional
            whether the absolute value of the loss scalers should be returned, by default False

        Returns
        -------
        pd.DataFrame
            inference result with losses of each feature
        """
        result = pd.DataFrame()

        LOG.debug(f'Getting inference results... (total of {len(dataset)} batches)')

        self.eval()
        feature_losses = defaultdict(list)
        output_df = []
        with torch.no_grad():
            for step, data_d in enumerate(dataset):
                LOG.debug(f'\tInferencing batch {step}...')

                batch_feature_losses = self._get_batch_feature_losses(**data_d['data'])
                for ft, loss_l in batch_feature_losses.items():
                    feature_losses[ft].append(loss_l)

                num, bin, cat = self.model(data_d['data']['input_original'])
                batch_output_df = self._decode_outputs_to_df(num=num, bin=bin, cat=cat)
                output_df.append(batch_output_df)

        LOG.debug(f'\tDone running inference. Making output df...')

        feature_losses = {ft: torch.cat(tensor_l, dim=0) for ft, tensor_l in feature_losses.items()}
        output_df = pd.concat(output_df).reset_index(drop=True)

        for ft, loss_tensor in feature_losses.items():
            result[ft] = preloaded_df[ft]
            result[ft + '_pred'] = output_df[ft]
            result[ft + '_loss'] = loss_tensor.cpu().numpy()
            z_loss = self.feature_loss_stats[ft]['scaler'].transform(loss_tensor)
            if return_abs:
                z_loss = abs(z_loss)
            result[ft + '_z_loss'] = z_loss.cpu().numpy()

        result['max_abs_z'] = result[[f'{ft}_z_loss' for ft in feature_losses]].max(axis=1)
        result['mean_abs_z'] = result[[f'{ft}_z_loss' for ft in feature_losses]].mean(axis=1)

        # add a column describing the scaler of the losses
        if self.loss_scaler_str == 'standard':
            output_scaled_loss_str = 'z'
        elif self.loss_scaler_str == 'modified':
            output_scaled_loss_str = 'modz'
        else:
            # in case other custom scaling is used
            output_scaled_loss_str = f'{self.loss_scaler_str}_scaled'
        result['z_loss_scaler_type'] = output_scaled_loss_str

        return result

    def get_anomaly_score(self, df):
        """
        Returns a per-row loss of the input dataframe.
        Does not corrupt inputs.
        """
        mse, bce, cce = self._get_anomaly_score_losses(df)

        combined_loss = torch.cat([mse, bce, cce], dim=1)

        net_loss = combined_loss.mean(dim=1).cpu().numpy()

        return net_loss

    def _decode_outputs_to_df(self, num, bin, cat):
        """
        Converts the model outputs of the numerical, binary, and categorical features
        back into a pandas dataframe.
        """
        row_count = len(num)
        index = range(row_count)

        num_cols = [x for x in self.numeric_fts.keys()]
        num_df = pd.DataFrame(data=num.cpu().numpy(), index=index)
        num_df.columns = num_cols
        for ft in num_df.columns:
            feature = self.numeric_fts[ft]
            col = num_df[ft]
            trans_col = feature['scaler'].inverse_transform(col.values)
            result = pd.Series(index=index, data=trans_col)
            num_df[ft] = result

        bin_cols = [x for x in self.binary_fts.keys()]
        bin_df = pd.DataFrame(data=bin.cpu().numpy(), index=index)
        bin_df.columns = bin_cols
        bin_df = bin_df.apply(lambda x: round(x)).astype(bool)
        for ft in bin_df.columns:
            feature = self.binary_fts[ft]
            map = {False: feature['cats'][0], True: feature['cats'][1]}
            bin_df[ft] = bin_df[ft].apply(lambda x: map[x])

        cat_df = pd.DataFrame(index=index)
        for i, ft in enumerate(self.categorical_fts):
            feature = self.categorical_fts[ft]
            cats = feature['cats']

            if (len(cats) > 0):
                # get argmax excluding NaN column (impute with next-best guess)
                codes = torch.argmax(cat[i][:, :-1], dim=1).cpu().numpy()
            else:
                # Only one option
                codes = torch.argmax(cat[i], dim=1).cpu().numpy()
            cat_df[ft] = codes
            cats = feature['cats'] + ["_other"]
            cat_df[ft] = cat_df[ft].apply(lambda x: cats[x])

        # concat
        output_df = pd.concat([num_df, bin_df, cat_df], axis=1)

        return output_df

    def _get_anomaly_score_losses(self, df):
        """
        Run the input dataframe `df` through the autoencoder to get the recovery losses by feature type
        (numerical/boolean/categorical).
        """
        self.eval()

        n_batches = len(df) // self.batch_size
        if len(df) % self.batch_size > 0:
            n_batches += 1

        mse_loss_slices, bce_loss_slices, cce_loss_slices = [], [], []
        with torch.no_grad():
            for i in range(n_batches):
                start = i * self.batch_size
                stop = (i + 1) * self.batch_size

                df_slice = df.iloc[start:stop]
                data_slice = self.prepare_df(df_slice)
                num_target, bin_target, codes = self.compute_targets(data_slice)

                input_slice = self.build_input_tensor(data_slice)

                num, bin, cat = self.model(input_slice)
                mse_loss_slice: torch.Tensor = self.mse(num, num_target)
                bce_loss_slice: torch.Tensor = self.bce(bin, bin_target)
                # each entry in `cce_loss_slice_of_each_feat` is the cce loss of a feature, ordered by the feature list self.categorical_fts
                cce_loss_slice_of_each_feat = []

                for i, ft in enumerate(self.categorical_fts):
                    loss = self.cce(cat[i], codes[i])
                    # Convert to 2 dimensions
                    cce_loss_slice_of_each_feat.append(loss.data.reshape(-1, 1))
                # merge the tensors into one (n_records * n_features) tensor
                cce_loss_slice = torch.cat(cce_loss_slice_of_each_feat, dim=1)

                mse_loss_slices.append(mse_loss_slice)
                bce_loss_slices.append(bce_loss_slice)
                cce_loss_slices.append(cce_loss_slice)

        mse_loss = torch.cat(mse_loss_slices, dim=0)
        bce_loss = torch.cat(bce_loss_slices, dim=0)
        cce_loss = torch.cat(cce_loss_slices, dim=0)
        return mse_loss, bce_loss, cce_loss

    def _scale_losses(self, mse, bce, cce):

        # Create outputs
        mse_scaled = torch.zeros_like(mse)
        bce_scaled = torch.zeros_like(bce)
        cce_scaled = torch.zeros_like(cce)

        for i, ft in enumerate(self.numeric_fts):
            mse_scaled[:, i] = self.feature_loss_stats[ft]['scaler'].transform(mse[:, i])

        for i, ft in enumerate(self.binary_fts):
            bce_scaled[:, i] = self.feature_loss_stats[ft]['scaler'].transform(bce[:, i])

        for i, ft in enumerate(self.categorical_fts):
            cce_scaled[:, i] = self.feature_loss_stats[ft]['scaler'].transform(cce[:, i])

        return mse_scaled, bce_scaled, cce_scaled

    def get_results(self, df, return_abs=False):
        pdf = pd.DataFrame()
        self.eval()

        data = self.prepare_df(df)

        with torch.no_grad():
            num, bin, embeddings = self._encode_input(data)
            x = torch.cat(num + bin + embeddings, dim=1)
            num, bin, cat = self.model(x)
            output_df = self._decode_outputs_to_df(num=num, bin=bin, cat=cat)

        # set the index of the prediction df to match the input df
        output_df.index = df.index

        mse, bce, cce = self._get_anomaly_score_losses(df)
        mse_scaled, bce_scaled, cce_scaled = self._scale_losses(mse, bce, cce)

        if (return_abs):
            mse_scaled = abs(mse_scaled)
            bce_scaled = abs(bce_scaled)
            cce_scaled = abs(cce_scaled)

        combined_loss = torch.cat([mse_scaled, bce_scaled, cce_scaled], dim=1)

        for i, ft in enumerate(self.numeric_fts):
            pdf[ft] = df[ft]
            pdf[ft + '_pred'] = output_df[ft]
            pdf[ft + '_loss'] = mse[:, i].cpu().numpy()
            pdf[ft + '_z_loss'] = mse_scaled[:, i].cpu().numpy()

        for i, ft in enumerate(self.binary_fts):
            pdf[ft] = df[ft]
            pdf[ft + '_pred'] = output_df[ft]
            pdf[ft + '_loss'] = bce[:, i].cpu().numpy()
            pdf[ft + '_z_loss'] = bce_scaled[:, i].cpu().numpy()

        for i, ft in enumerate(self.categorical_fts):
            pdf[ft] = df[ft]
            pdf[ft + '_pred'] = output_df[ft]
            pdf[ft + '_loss'] = cce[:, i].cpu().numpy()
            pdf[ft + '_z_loss'] = cce_scaled[:, i].cpu().numpy()

        pdf['max_abs_z'] = combined_loss.max(dim=1)[0].cpu().numpy()
        pdf['mean_abs_z'] = combined_loss.mean(dim=1).cpu().numpy()

        # add a column describing the scaler of the losses
        if self.loss_scaler_str == 'standard':
            output_scaled_loss_str = 'z'
        elif self.loss_scaler_str == 'modified':
            output_scaled_loss_str = 'modz'
        else:
            # in case other custom scaling is used
            output_scaled_loss_str = f'{self.loss_scaler_str}_scaled'
        pdf['z_loss_scaler_type'] = output_scaled_loss_str

        return pdf
