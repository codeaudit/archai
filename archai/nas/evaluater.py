# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from typing import Optional
import importlib
import sys
import string
import os

import torch
from torch import nn

from overrides import overrides, EnforceOverrides

from archai.common.trainer import Trainer
from archai.common.config import Config
from archai.common.common import logger
from archai.datasets import data
from archai.nas.model_desc import ModelDesc
from archai.nas.model_desc_builder import ModelDescBuilder
from archai.nas import nas_utils
from archai.common import ml_utils, utils
from archai.common.metrics import EpochMetrics, Metrics
from archai.nas.model import Model
from archai.common.checkpoint import CheckPoint


class Evaluater(EnforceOverrides):
    def evaluate(self, conf_eval:Config, model_desc_builder:ModelDescBuilder)->Metrics:
        logger.pushd('eval_arch')

        # region conf vars
        conf_checkpoint = conf_eval['checkpoint']
        resume = conf_eval['resume']

        model_filename    = conf_eval['model_filename']
        metric_filename    = conf_eval['metric_filename']
        # endregion

        model = self.create_model(conf_eval, model_desc_builder)

        checkpoint = nas_utils.create_checkpoint(conf_checkpoint, resume)
        train_metrics = self.train_model(conf_eval, model, checkpoint)
        train_metrics.save(metric_filename)

        # save model
        if model_filename:
            model_filename = utils.full_path(model_filename)
            ml_utils.save_model(model, model_filename)

        logger.info({'model_save_path': model_filename})

        logger.popd()

        return train_metrics

    def train_model(self, conf_train:Config, model:nn.Module,
                    checkpoint:Optional[CheckPoint])->Metrics:
        conf_loader = conf_train['loader']
        conf_train = conf_train['trainer']

        # get data
        train_dl, _, test_dl = data.get_data(conf_loader)
        assert train_dl is not None and test_dl is not None

        trainer = Trainer(conf_train, model, checkpoint)
        train_metrics = trainer.fit(train_dl, test_dl)
        return train_metrics

    def _default_module_name(self, dataset_name:str, function_name:str)->str:
        """Select PyTorch pre-defined network to support manual mode"""
        module_name = ''
        # TODO: below detection code is too week, need to improve, possibly encode image size in yaml and use that instead
        if dataset_name.startswith('cifar'):
            if function_name.startswith('res'): # support resnext as well
                module_name = 'archai.cifar10_models.resnet'
            elif function_name.startswith('dense'):
                module_name = 'archai.cifar10_models.densenet'
        elif dataset_name.startswith('imagenet') or dataset_name.startswith('sport8'):
            module_name = 'torchvision.models'
        if not module_name:
            raise NotImplementedError(f'Cannot get default module for {function_name} and dataset {dataset_name} because it is not supported yet')
        return module_name

    def create_model(self, conf_eval:Config, model_desc_builder:ModelDescBuilder,
                      final_desc_filename=None, full_desc_filename=None)->nn.Module:
        # region conf vars
        dataset_name = conf_eval['loader']['dataset']['name']

        # if explicitly passed in then don't get from conf
        if not final_desc_filename:
            final_desc_filename = conf_eval['final_desc_filename']
            full_desc_filename = conf_eval['full_desc_filename']
        model_factory_spec = conf_eval['model_factory_spec']
        conf_model_desc   = conf_eval['model_desc']
        # endregion

        if model_factory_spec: # use pre-built model for evaluation
            return self.model_from_factory(model_factory_spec, dataset_name)
        else:
            # load model desc file to get template model
            template_model_desc = ModelDesc.load(final_desc_filename)
            model_desc = model_desc_builder.build(conf_model_desc,
                                                template=template_model_desc)

            # save desc for reference
            model_desc.save(full_desc_filename)

            model = self.model_from_desc(model_desc)

            logger.info({'model_factory':False,
                        'cells_len':len(model.desc.cell_descs()),
                        'init_node_ch': conf_model_desc['init_node_ch'],
                        'n_cells': conf_model_desc['n_cells'],
                        'n_reductions': conf_model_desc['n_reductions'],
                        'n_nodes': conf_model_desc['n_nodes']})

        return model

    def model_from_factory(self, model_factory_spec:str, dataset_name:str)->Model:
        splitted = model_factory_spec.rsplit('.', 1)
        function_name = splitted[-1]

        if len(splitted) > 1:
            module_name = splitted[0]
        else:
            module_name = self._default_module_name(dataset_name, function_name)

        module = importlib.import_module(module_name) if module_name else sys.modules[__name__]
        function = getattr(module, function_name)
        model = function()

        logger.info({'model_factory':True,
                    'module_name': module_name,
                    'function_name': function_name,
                    'params': ml_utils.param_size(model)})

        return model

    def model_from_desc(self, model_desc)->Model:
        return Model(model_desc, droppath=True, affine=True)