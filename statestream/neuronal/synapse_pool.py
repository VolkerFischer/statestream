# -*- coding: utf-8 -*-
# Copyright (c) 2017 - for information on the respective copyright owner
# see the NOTICE file and/or the repository https://github.com/VolkerFischer/statestream
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.



import numpy as np

from statestream.utils.helper import is_scalar_shape
from statestream.meta.synapse_pool import sp_shm_layout, sp_init, sp_get_dict
from statestream.meta.neuron_pool import np_state_shape

import theano
import theano.tensor as T

from statestream.neuronal.activations import selu, relu, Id, gaussian, elu, tanh, sigmoid, softmax, exp, neglog, log



"""
    Specification parameters:
        "source"        [[str, .., str],    List of lists of all inputs to
                        ...                 this synaptic pool.
                        [str, .., str]]     At the moment only [[str]].
        "dilation"      [[int, .., int],    This list of list of ints gives
                        ...                 the dilation parameter for each input.
                         [int, .., int]]    The default is 1 for each input.
        "rf"            [[int, .., int],    This list of list of ints gives
                        ...                 the receptive field size for each input.
                         [int, .., int]]    The default is a dense layer.
        "noise"         string              Additive noise for this sp.
                                            Available: "normal", "uniform"
                                            Default: None
        "act"           [str, .., str]      This list of strings holds one activation
                                            function for each factor, which is 
                                            applied at the end of the initial 
                                            concat + convolution
        "avgout"        int                 The avg-out feature dimension factor.
        "maxout"        int                 The max-out feature dimension factor.
        "factor_shapes" [str, .., str]      This list contains a factor shape specification
                                            for each factor. If shape is specified for one
                                            factor it must be specified for all.
                                            Possible specifications are:
                                                "full": default, corresponds to [*,*,*]
                                                "feature": feature factor [*,1,1]
                                                "spatial": spatial factor [1,*,*]
                                                "scalar": scalar factor [1,1,1]
        "target_shapes" [[str, .., str],    This is the shape directly after the convolution
                        ...                 with W_i_j. See also factor_shapes for available
                         [str, .., str]]    values.
        "weight_shapes" [[str, .., str],    This is the weight shape for each W_i_j. E.g.
                        ...                 scalar means that all coefficience in W_i_j
                         [str, .., str]]    share the same value.
                                                "full":             [tgt_c, src_c, rf_x, rf_y]
                                                "feature":          [tgt_c, src_c, 1, 1]
                                                "spatial":          [1, 1, rf_x, rf_y]
                                                "scalar":           [1, 1, 1, 1]
                                                "src_feature":      [1, src_c, 1, 1]
                                                "tgt_feature":      [tgt_c, 1, 1, 1]
                                                "src_spatial":      [1, src_c, rf_x, rf_y]
                                                "tgt_spatial":      [tgt_c, 1, rf_x, rf_y]
        "bias_shapes"   [str, .., str]      Bias shape for each factor.
        "ppp"           [[int, .., int],    Pre-Processing Projection dimensions for each
                        ...                 source neuron-pool. Default is 0 meaning no
                         [int, .., int]]    pre-processing for this source np.
        "weight_fnc"    [[str, .., str],    This is the weight function for every W_i_j.
                        ...
                         [str, .., str]]    
    
    Case source = [[string]]:
        Case downsampling (or no sampling):
            conv2d (dilation, rf)
        Case upsampling:
            repeat (srcX/trtX)
            conv2d (dilation, rf)
        maxout
        activation
        noise
    Case source = [[string, ..., string]]
"""



"""Class of synapse-pools.
"""
class SynapsePool(object):
    def __init__(self, name, net, param, mn, source_nps, target_np):
        """Constructor for synapse pool class.
        
        Parameters
        ----------
        name : string
            Unique string identifier for synapse pool from spec file.
        net : 
            Imported global specification file.
        source_nps : list of neuron pool refs
            List of all source neuron pools.
        target_np : neuron pool ref
            Target neuron pool.
            
        Returns
        -------
        """
        # Get standard structures.
        self.name = name
        self.net = net
        self.param = param
        self.mn = mn
        # Get synapse pool dictionary.
        self.p = self.net["synapse_pools"][self.name]
        # Get sources / targets.
        self.source_np = source_nps
        self.target_np = target_np
        # Get local representation of shared memory.
        self.dat_layout = sp_shm_layout(self.name, self.net, self.param)
        self.dat = {}
        for t in ["parameter", "variables"]:
            self.dat[t] = {}
            for i,i_l in self.dat_layout[t].items():
                if i_l.type == "th":
                    if is_scalar_shape(i_l.shape):
                        self.dat[t][i] = theano.shared(theano._asarray(0.0, dtype=theano.config.floatX),
                                                                    borrow = True,
                                                                    name = name + " " + i)
                    else:
                        if i_l.broadcastable is not None:
                            self.dat[t][i] = theano.shared(np.zeros(i_l.shape, dtype=theano.config.floatX),
                                                                        borrow=True,
                                                                        name=name + " " + i, broadcastable=i_l.broadcastable)
                        else:
                            self.dat[t][i] = theano.shared(np.zeros(i_l.shape, dtype=theano.config.floatX),
                                                                        borrow=True,
                                                                        name=name + " " + i)
                elif i_l.type == "np":
                    if is_scalar_shape(i_l.shape):
                        self.dat[t][i] = np.array([1,], dtype=i_l.dtype)
                    else:
                        self.dat[t][i] = np.array(i_l.shape, dtype=i_l.dtype)

        # Initialize sp parameter dependent on type.
        # ---------------------------------------------------------------------
        # Determine number of factors.
        self.no_factors = len(self.p["source"])
        # Get / set overall activation function.
        self.ACT = self.p.get("ACT", "Id")
        # Get activation functions for all factors.
        self.act = self.p.get("act", ["Id" for i in range(self.no_factors)])
        # Get / set noise.
        self.noise = self.p.get("noise", None)
        if self.noise != None:
            self.noise_dist = theano.tensor.shared_randomstreams.RandomStreams(np.random.RandomState(42).randint(99999))
        
        # Get factor shape (all concat inputs of a factor must have this same shape).
        if "factor_shapes" in self.p:
            self.factor_shapes = self.p["factor_shapes"]
        else:
            self.factor_shapes = []
            for f in range(self.no_factors):
                self.factor_shapes.append("full")
        # Get / set target_shapes for all inputs.
        if "target_shapes" in self.p:
            self.target_shapes = self.p["target_shapes"]
        else:
            self.target_shapes = []
            for f in range(self.no_factors):
                self.target_shapes.append([])
                for i in range(len(self.p["source"][f])):
                    self.target_shapes[-1].append(self.factor_shapes[f])
        # Get / set weight_shapes for all inputs.
        self.weight_shapes = sp_get_dict(self.p, "weight_shapes", "full")
        # Get / set pre-processing projection dimensions.
        self.ppp_dims = sp_get_dict(self.p, "ppp", 0)
        # Get / set bias shape.
        if "bias_shapes" in self.p:
            self.bias_shapes = self.p["bias_shapes"]
        else:
            self.bias_shapes = []
            for f in range(self.no_factors):
                self.bias_shapes.append(None)
        # Get / set unshare flag for all inputs.
        self.W_unshare = []
        self.P_unshare = []
        self.b_unshare = []
        for f in range(self.no_factors):
            self.W_unshare.append([])
            self.P_unshare.append([])
            self.b_unshare.append(False)
            for i in range(len(self.p["source"][f])):
                self.W_unshare[-1].append(False)
                self.P_unshare[-1].append(False)
        if "unshare" in self.p:
            for f in range(self.no_factors):
                if "b_" + str(f) in self.p["unshare"]:
                    self.b_unshare[f] = True
                for i in range(len(self.p["source"][f])):
                    if "W_" + str(f) + "_" + str(i) in self.p["unshare"]:
                        self.W_unshare[f][i] = True
                    if "P_" + str(f) + "_" + str(i) in self.p["unshare"]:
                        self.P_unshare[f][i] = True
        # Get / set dilation for all inputs.
        self.dilation = sp_get_dict(self.p, "dilation", 1)
        # Get / set avg/max out feature factor.
        self.avgout = self.p.get("avgout", 1)
        self.maxout = self.p.get("maxout", 1)
        # Get / set weight functions.
        if "weight_fnc" in self.p:
            self.weight_fnc = self.p["weight_fnc"]
        else:
            self.weight_fnc = []
            for f in range(self.no_factors):
                self.weight_fnc.append([])
                for i in range(len(self.p["source"][f])):
                    self.weight_fnc[-1].append("Id")
        # Get rf sizes from already specified parameters W.
        self.rf_size_orig = sp_get_dict(self.p, "rf", 0)
        self.rf_size = []
        self.pad = []
        self.pooling = []
        for f in range(self.no_factors):
            self.rf_size.append([])
            self.pad.append([])
            self.pooling.append([])
            for i in range(len(self.p["source"][f])):
                par_name = "W_" + str(f) + "_" + str(i)
                if self.W_unshare[f][i]:
                    self.rf_size[-1].append([self.dat_layout["parameter"][par_name].shape[3], 
                                             self.dat_layout["parameter"][par_name].shape[4]])
                    self.pad[-1].append([self.dat_layout["parameter"][par_name].shape[3] // 2, 
                                         self.dat_layout["parameter"][par_name].shape[4] // 2])
                else:                    
                    self.rf_size[-1].append([self.dat_layout["parameter"][par_name].shape[2], 
                                             self.dat_layout["parameter"][par_name].shape[3]])
                    self.pad[-1].append([self.dat_layout["parameter"][par_name].shape[2] // 2, 
                                         self.dat_layout["parameter"][par_name].shape[3] // 2])
                # Determine pooling for each input.
                self.pooling[-1].append(0)
                if self.rf_size[-1][-1][0] != self.source_np[f][i].shape[2]:
                    if self.source_np[f][i].shape[2] == self.target_np.shape[2]:
                        self.pooling[-1][-1] = 1
                    elif self.source_np[f][i].shape[2] > self.target_np.shape[2]:
                        self.pooling[-1][-1] = int(self.source_np[f][i].shape[2] / self.target_np.shape[2])
                    elif self.target_np.shape[2] > self.source_np[f][i].shape[2]:
                        self.pooling[-1][-1] = -int(self.target_np.shape[2] / self.source_np[f][i].shape[2])

        #print("\n" + self.name + " rf: " + str(self.rf_size) + " pool: " + str(self.pooling))
            
        # Initialize post synaptics with empty.
        self.post_synaptic = []
        # Append this sp to its target np as source.
        target_np.sources.append(self)



    def compute_post_synaptic(self, as_empty=False):
        """Method computes the post synaptic response for this synapse pool
        and adds these computations to the theano computation graph.
        
        Parameters
        ----------
        as_empty : bool
            Determines if the post synaptic response has really to be
            computed. For "sparse" theano graphs this is sometimes not
            necessary.
            
        Returns
        -------
        """
        if as_empty:
            self.post_synaptic.append(None)
        else:
            # Determine number of features in target np.
            tgt_shape = np_state_shape(self.net, self.p["target"])
            # Handle transformer exception.
            if "tags" in self.p:
                if "TRANSFORMER" in self.p["tags"]:
                    from statestream.neuronal.spatial_transformer_dense import warp_transform
                    self.post_synaptic.append(warp_transform(self.source_np[0][0].state[-1], 
                                                             tgt_shape,
                                                             self.source_np[1][0].state[-1][:,[0],:,:],
                                                             self.source_np[1][0].state[-1][:,[1],:,:]))
                    return
            # Generate graph to compute all factor outputs.
            # _SCALED_CONV will be a 2D list of theano variables holding
            # all target activations.
            _SCALED_CONV = []
            for f in range(self.no_factors):
                _SCALED_CONV.append([])
                for i in range(len(self.p["source"][f])):
                    src_shape = np_state_shape(self.net, self.p["source"][f][i])
                    W_name = "W_" + str(f) + "_" + str(i)
                    P_name = "P_" + str(f) + "_" + str(i)
                    # Check for pre-processing projection and compute
                    # preprocessed state if needed.
                    ppp_state = None
                    if self.ppp_dims[f][i] > 0:
                        if self.P_unshare[f][i]:
                            out_state = []
                            for a in range(self.net["agents"]):
                                in_state = T.unbroadcast(self.source_np[f][i].state[-1][a,:,:,:][np.newaxis,:,:,:], 0)
                                out_state.append(T.nnet.conv2d(in_state,
                                                               self.dat["parameter"][P_name][a,:,:,:,:],
                                                               border_mode=(0, 0),
                                                               subsample=(1, 1)))
                            ppp_state = T.concatenate(out_state)
                        else:
                            ppp_state = T.nnet.conv2d(self.source_np[f][i].state[-1], 
                                                      self.dat["parameter"][P_name],
                                                      border_mode=(0, 0),
                                                      subsample=(1, 1))
                    else:
                        ppp_state = self.source_np[f][i].state[-1]
                    # Check for unshared weights.
                    unshared_preshape = []
                    if self.W_unshare[f][i]:
                        unshared_preshape = [self.net["agents"]]
                    # Apply weight function.
                    if self.weight_fnc[f][i] != "Id":
                        weights_raw = eval(self.weight_fnc[f][i])(self.dat["parameter"][W_name])
                    else:
                        weights_raw = self.dat["parameter"][W_name]
                    # Broadcast weights if needed.
                    if self.weight_shapes[f][i] is not "full":
                        if self.ppp_dims[f][i] == 0:
                            src_features = np_state_shape(self.net, self.p["source"][f][i])[1]
                        else:
                            src_features = self.ppp_dims[f][i]
                        # Determine real (broadcasted) weight matrix shape.
                        if self.target_shapes[f][i] in ["full", "features"]:
                            shape = unshared_preshape + [tgt_shape[1], 
                                     src_features, 
                                     self.rf_size[f][i][0], 
                                     self.rf_size[f][i][1]]
                        else:
                            shape = unshared_preshape + [1, 
                                     src_features, 
                                     self.rf_size[f][i][0], 
                                     self.rf_size[f][i][1]]
                        # Generate matrix of ones with original weights shape.
                        ones_weight_mat = theano.shared(np.ones(shape, dtype=np.float32))
                        # Multiply coefficient-wise.
                        weights = weights_raw * ones_weight_mat
                    else:
                        weights = weights_raw
                        
                    # 1D convolution.
                    if self.source_np[f][i].shape[3] == 1:
                        if self.pooling[f][i] >= 1:
                            # Do convolution.
                            if self.dilation[f][i] > 1:
                                if self.W_unshare[f][i]:
                                    # In case of unshared parameters, no weight sharing
                                    # across agents (samples in batch).
                                    out_state = []
                                    for a in range(self.net["agents"]):
                                        in_state = T.unbroadcast(ppp_state[a,:,:,:][np.newaxis,:,:,:], 0)
                                        out_state.append(T.nnet.conv2d(in_state,
                                                                       weights[a,:,:,:,:],
                                                                       border_mode="half",
                                                                       subsample=(self.pooling[f][i], 1),
                                                                       filter_dilation=(self.dilation[f][i], 1)))
                                    _SCALED_CONV[-1].append(T.concatenate(out_state))
                                else:
                                    _SCALED_CONV[-1].append(T.nnet.conv2d(ppp_state, 
                                                                weights,
                                                                border_mode="half",
                                                                subsample=(self.pooling[f][i], 1),
                                                                filter_dilation=(self.dilation[f][i], 1)))
                            else:
                                if self.W_unshare[f][i]:
                                    out_state = []
                                    for a in range(self.net["agents"]):
                                        in_state = T.unbroadcast(ppp_state[a,:,:,:][np.newaxis,:,:,:], 0)
                                        out_state.append(T.nnet.conv2d(in_state,
                                                                       weights[a,:,:,:,:],
                                                                       border_mode=(self.pad[f][i][0], 0),
                                                                       subsample=(self.pooling[f][i], 1)))
                                    _SCALED_CONV[-1].append(T.concatenate(out_state))
                                else:
                                    _SCALED_CONV[-1].append(T.nnet.conv2d(ppp_state, 
                                                                          weights,
                                                                          border_mode=(self.pad[f][i][0], 0),
                                                                          subsample=(self.pooling[f][i], 1)))
                        elif self.pooling[f][i] <= -1:
                            # 1D upsampling.
                            upsampled = T.extra_ops.repeat(ppp_state,
                                                           -self.pooling[f][i],
                                                           2)
                            if self.dilation[f][i] > 1:
                                if self.W_unshare[f][i]:
                                    out_state = []
                                    for a in range(self.net["agents"]):
                                        in_state = T.unbroadcast(upsampled[a,:,:,:][np.newaxis,:,:,:], 0)
                                        out_state.append(T.nnet.conv2d(in_state,
                                                                       weights[a,:,:,:,:],
                                                                       border_mode="half",
                                                                       subsample=(1, 1),
                                                                       filter_dilation=(self.dilation[f][i], 1)))
                                    _SCALED_CONV[-1].append(T.concatenate(out_state))
                                else:
                                    _SCALED_CONV[-1].append(T.nnet.conv2d(upsampled, 
                                                                          weights,
                                                                          border_mode="half",
                                                                          subsample=(1, 1),
                                                                          filter_dilation=(self.dilation[f][i], 1)))
                            else:
                                if self.W_unshare[f][i]:
                                    out_state = []
                                    for a in range(self.net["agents"]):
                                        in_state = T.unbroadcast(upsampled[a,:,:,:][np.newaxis,:,:,:], 0)
                                        out_state.append(T.nnet.conv2d(in_state,
                                                                       weights[a,:,:,:,:],
                                                                       border_mode=(self.pad[f][i][0], 0),
                                                                       subsample=(1, 1)))
                                    _SCALED_CONV[-1].append(T.concatenate(out_state))
                                else:
                                    _SCALED_CONV[-1].append(T.nnet.conv2d(upsampled, 
                                                                         weights,
                                                                         border_mode=(self.pad[f][i][0], 0),
                                                                         subsample=(1, 1)))
                        else:
                            # Fully connected (pooling = 0).
                            if self.W_unshare[f][i]:
                                out_state = []
                                for a in range(self.net["agents"]):
                                    in_state = T.unbroadcast(ppp_state[a,:,:,:][np.newaxis,:,:,:], 0)
                                    out_state.append(T.nnet.conv2d(in_state,
                                                                   weights[a,:,:,:,:],
                                                                   border_mode=(0, 0),
                                                                   subsample=(1, 1)))
                                _SCALED_CONV[-1].append(T.concatenate(out_state))
                            else:
                                _SCALED_CONV[-1].append(T.nnet.conv2d(ppp_state, 
                                                                      weights,
                                                                      border_mode=(0, 0),
                                                                      subsample=(1, 1)))
                            
                    # 2D convolution.
                    else:
                        if self.pooling[f][i] >= 1:
                            # Do convolution dependent on dilation and un-sharing.
                            if self.dilation[f][i] > 1:
                                if self.W_unshare[f][i]:
                                    out_state = []
                                    for a in range(self.net["agents"]):
                                        in_state = T.unbroadcast(ppp_state[a,:,:,:][np.newaxis,:,:,:], 0)
                                        out_state.append(T.nnet.conv2d(in_state,
                                                                       weights[a,:,:,:,:],
                                                                       border_mode="half",
                                                                       subsample=(self.pooling[f][i], self.pooling[f][i]),
                                                                       filter_dilation=(self.dilation[f][i], self.dilation[f][i])))
                                    _SCALED_CONV[-1].append(T.concatenate(out_state))
                                else:
                                    _SCALED_CONV[-1].append(T.nnet.conv2d(ppp_state, 
                                                                          weights,
                                                                          border_mode="half",
                                                                          subsample=(self.pooling[f][i], self.pooling[f][i]),
                                                                          filter_dilation=(self.dilation[f][i], self.dilation[f][i])))
                            else:
                                if self.W_unshare[f][i]:
                                    out_state = []
                                    for a in range(self.net["agents"]):
                                        in_state = T.unbroadcast(ppp_state[a,:,:,:][np.newaxis,:,:,:], 0)
                                        out_state.append(T.nnet.conv2d(in_state,
                                                                       weights[a,:,:,:,:],
                                                                       border_mode=(self.pad[f][i][0], self.pad[f][i][1]),
                                                                       subsample=(self.pooling[f][i], self.pooling[f][i])))
                                    _SCALED_CONV[-1].append(T.concatenate(out_state))
                                else:
                                    _SCALED_CONV[-1].append(T.nnet.conv2d(ppp_state, 
                                                                          weights,
                                                                          border_mode=(self.pad[f][i][0], self.pad[f][i][1]),
                                                                          subsample=(self.pooling[f][i], self.pooling[f][i])))
                        elif self.pooling[f][i] <= -1:
                            # 2D upsampling.
                            upsampled = T.extra_ops.repeat(T.extra_ops.repeat(ppp_state,
                                                                              -self.pooling[f][i],
                                                                              2),
                                                             -self.pooling[f][i],
                                                             3)
                            if self.dilation[f][i] > 1:
                                if self.W_unshare[f][i]:
                                    out_state = []
                                    for a in range(self.net["agents"]):
                                        in_state = T.unbroadcast(upsampled[a,:,:,:][np.newaxis,:,:,:], 0)
                                        out_state.append(T.nnet.conv2d(in_state,
                                                                       weights[a,:,:,:,:],
                                                                       border_mode="half",
                                                                       subsample=(1, 1),
                                                                       filter_dilation=(self.dilation[f][i], self.dilation[f][i])))
                                    _SCALED_CONV[-1].append(T.concatenate(out_state))
                                else:
                                    _SCALED_CONV[-1].append(T.nnet.conv2d(upsampled, 
                                                                          weights,
                                                                          border_mode="half",
                                                                          subsample=(1, 1),
                                                                          filter_dilation=(self.dilation[f][i], self.dilation[f][i])))
                            else:
                                if self.W_unshare[f][i]:
                                    out_state = []
                                    for a in range(self.net["agents"]):
                                        in_state = T.unbroadcast(upsampled[a,:,:,:][np.newaxis,:,:,:], 0)
                                        out_state.append(T.nnet.conv2d(in_state,
                                                                       weights[a,:,:,:,:],
                                                                       border_mode=(self.pad[f][i][0], self.pad[f][i][1]),
                                                                       subsample=(1, 1)))
                                    _SCALED_CONV[-1].append(T.concatenate(out_state))

                                else:
                                    _SCALED_CONV[-1].append(T.nnet.conv2d(upsampled, 
                                                                          weights,
                                                                          border_mode=(self.pad[f][i][0], self.pad[f][i][1]),
                                                                          subsample=(1, 1)))
                        else:
                            # Fully connected (pooling = 0).
                            if self.W_unshare[f][i]:
                                out_state = []
                                for a in range(self.net["agents"]):
                                    in_state = T.unbroadcast(ppp_state[a,:,:,:][np.newaxis,:,:,:], 0)
                                    out_state.append(T.nnet.conv2d(in_state,
                                                                   weights[a,:,:,:,:],
                                                                   border_mode=(0, 0),
                                                                   subsample=(1, 1)))
                                _SCALED_CONV[-1].append(T.concatenate(out_state))
                            else:
                                _SCALED_CONV[-1].append(T.nnet.conv2d(ppp_state, 
                                                                      weights,
                                                                      border_mode=(0, 0),
                                                                      subsample=(1, 1)))

            # Sum over inputs to factors (targets).
            _SCALED_CONV_SUM = []
            for f in range(self.no_factors):
                if self.bias_shapes[f] is not None:
                    b_name = "b_" + str(f)
                # Dependent on factors bias shape initialize sum with correct shape.
                sum_init = False
                init_with = -1
                if not sum_init:
                    # If one target input has sufficient shape, we use it for initialization.
                    for i in range(len(self.p["source"][f])):
                        if self.factor_shapes[f] == self.target_shapes[f][i]:
                            _SCALED_CONV_SUM.append(_SCALED_CONV[f][i])
                            init_with = i
                            sum_init = True
                            break
                # Finally, we have to init the factor with zeros.
                if not sum_init:
                    # Determine real (broadcasted) factor shape.
                    if self.factor_shapes[f] == "full":
                        shape = tgt_shape
                    elif self.factor_shapes[f] == "feature":
                        shape = [tgt_shape[0],
                                 tgt_shape[1],
                                 1,
                                 1]
                    elif self.factor_shapes[f] == "spatial":
                        shape = [tgt_shape[0],
                                 1,
                                 tgt_shape[2],
                                 tgt_shape[3]]
                    elif self.factor_shapes[f] == "scalar":
                        shape = [tgt_shape[0],
                                 1,
                                 1,
                                 1]
                    # Generate zero state of factor shape
                    zeros_state = theano.shared(np.zeros(shape, dtype=np.float32))
                    # Initialize factor with zero of correct shape.
                    _SCALED_CONV_SUM.append(zeros_state)
                # Sum up all inputs for factor f (except for the
                # potentially used for initialization).
                for i in range(len(self.p["source"][f])):
                    if init_with != i:
                        if self.target_shapes[f][i] == "full":
                            _SCALED_CONV_SUM[f] += _SCALED_CONV[f][i]
                        elif self.target_shapes[f][i] == "feature":
                            _SCALED_CONV_SUM[f] += _SCALED_CONV[f][i].dimshuffle(0,1).dimshuffle(0,1,"x","x")
                        elif self.target_shapes[f][i] == "spatial":
                            _SCALED_CONV_SUM[f] += _SCALED_CONV[f][i].dimshuffle(0,2,3).dimshuffle(0,"x",1,2)
                        elif self.target_shapes[f][i] == "scalar":
                            _SCALED_CONV_SUM[f] += _SCALED_CONV[f][i].dimshuffle(0).dimshuffle(0,"x","x","x")
                # (Add bias and) evaluate factor activation function.
                if self.bias_shapes[f] is None:
                    _SCALED_CONV_SUM[f] = eval(self.act[f])(_SCALED_CONV_SUM[f])
                else:
                    if self.bias_shapes[f] == "full":
                        _SCALED_CONV_SUM[f] = eval(self.act[f])(_SCALED_CONV_SUM[f] \
                                                                + self.dat["parameter"][b_name].dimshuffle("x",0,1,2))
                    elif self.bias_shapes[f] == "feature":
                        _SCALED_CONV_SUM[f] = eval(self.act[f])(_SCALED_CONV_SUM[f] \
                                                                + self.dat["parameter"][b_name].dimshuffle("x",0,"x","x"))
                    elif self.bias_shapes[f] == "spatial":
                        _SCALED_CONV_SUM[f] = eval(self.act[f])(_SCALED_CONV_SUM[f] \
                                                                + self.dat["parameter"][b_name].dimshuffle("x","x",0,1))
                    elif self.bias_shapes[f] == "scalar":
                        _SCALED_CONV_SUM[f] = eval(self.act[f])(_SCALED_CONV_SUM[f] \
                                                                + self.dat["parameter"][b_name][0])

            # Initialize product.
            init_with = -1
            # Try to init with existing factor.
            for f in range(self.no_factors):
                if self.factor_shapes[f] == "full":
                    scaled_conv = _SCALED_CONV_SUM[f]
                    init_with = f
                    break
            # Alternatively we have to init it with a ones state.
            if init_with == -1:
                # Generate ones state like target state.
                scaled_conv = theano.shared(np.ones(tgt_shape, dtype=np.float32))
            for f in range(self.no_factors):
                if init_with != f:
                    if self.factor_shapes[f] == "full":
                        scaled_conv = scaled_conv * _SCALED_CONV_SUM[f]
                    elif self.factor_shapes[f] == "feature":
                        scaled_conv = scaled_conv * _SCALED_CONV_SUM[f][:,:,0,0].dimshuffle(0,1,"x","x")
                    elif self.factor_shapes[f] == "spatial":
                        scaled_conv = scaled_conv * _SCALED_CONV_SUM[f][:,0,:,:].dimshuffle(0,"x",1,2)
                    elif self.factor_shapes[f] == "scalar":
                        scaled_conv = scaled_conv * _SCALED_CONV_SUM[f][:,0,0,0].dimshuffle(0,"x","x","x")

            # Apply avgout.
            if self.avgout == 1:
                scaled_conv_avgout = scaled_conv
            else:
                scaled_conv_avgout = None
                for i in np.arange(self.avgout):
                    t = scaled_conv[:,i::self.avgout,:,:]
                    if scaled_conv_avgout == None:
                        scaled_conv_avgout = t
                    else:
                        scaled_conv_avgout = scaled_conv_avgout + t

            # Apply maxout.
            if self.maxout == 1:
                scaled_conv_maxout = scaled_conv_avgout
            else:
                scaled_conv_maxout = None
                for i in np.arange(self.maxout):
                    t = scaled_conv_avgout[:,i::self.maxout,:,:]
                    if scaled_conv_maxout == None:
                        scaled_conv_maxout = t
                    else:
                        scaled_conv_maxout = T.maximum(scaled_conv_maxout, t)

            # activation
            if self.ACT != "Id":
                self.post_synaptic.append(eval(self.ACT)(scaled_conv_maxout))
            else:
                self.post_synaptic.append(scaled_conv_maxout)

            
            # Add noise.
            if self.noise == "normal":
                self.post_synaptic[-1] += self.dat["parameter"]["noise_mean"] \
                                          + self.dat["parameter"]["noise_std"] \
                                          * self.noise_dist.normal(self.target_np.shape)
            elif self.noise == "uniform":
                self.post_synaptic[-1] += (self.dat["parameter"]["noise_max"] - self.dat["parameter"]["noise_min"]) \
                                          * self.noise_dist.normal(self.target_np.shape) \
                                          - self.dat["parameter"]["noise_min"]

                    


    # initialize weights randomly (only a wrapper for sp_init)
    def init_parameter(self, mode = None, params = None):
        # initialize as specified in global spec file
        if mode is None:
            # loop over all parameter
            for par in self.parameter_shape:
                par_value = sp_init(self.net, self.param, self.name, par)            
                # set parameter
                self.dat["parameter"][par].set_value(par_value)
                
        else:
            pass

        
    def set_params(self, params):
        for p in params:
            self.dat["parameter"][p].set_value(params[p])


    