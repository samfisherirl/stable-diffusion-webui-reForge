import torch
import copy
import inspect
import logging
import uuid
import collections
import math

import ldm_patched.modules.utils
import ldm_patched.float
import ldm_patched.modules.model_management
import ldm_patched.modules.lora
from ldm_patched.modules.types import UnetWrapperFunction

extra_weight_calculators = {}

def string_to_seed(data):
    crc = 0xFFFFFFFF
    for byte in data:
        if isinstance(byte, str):
            byte = ord(byte)
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xEDB88320
            else:
                crc >>= 1
    return crc ^ 0xFFFFFFFF

def set_model_options_patch_replace(model_options, patch, name, block_name, number, transformer_index=None):
    to = model_options["transformer_options"].copy()

    if "patches_replace" not in to:
        to["patches_replace"] = {}
    else:
        to["patches_replace"] = to["patches_replace"].copy()

    if name not in to["patches_replace"]:
        to["patches_replace"][name] = {}
    else:
        to["patches_replace"][name] = to["patches_replace"][name].copy()

    if transformer_index is not None:
        block = (block_name, number, transformer_index)
    else:
        block = (block_name, number)
    to["patches_replace"][name][block] = patch
    model_options["transformer_options"] = to
    return model_options

def set_model_options_post_cfg_function(model_options, post_cfg_function, disable_cfg1_optimization=False):
    model_options["sampler_post_cfg_function"] = model_options.get("sampler_post_cfg_function", []) + [post_cfg_function]
    if disable_cfg1_optimization:
        model_options["disable_cfg1_optimization"] = True
    return model_options

def set_model_options_pre_cfg_function(model_options, pre_cfg_function, disable_cfg1_optimization=False):
    model_options["sampler_pre_cfg_function"] = model_options.get("sampler_pre_cfg_function", []) + [pre_cfg_function]
    if disable_cfg1_optimization:
        model_options["disable_cfg1_optimization"] = True
    return model_options

def wipe_lowvram_weight(m):
    if hasattr(m, "prev_ldm_patched_cast_weights"):
        m.ldm_patched_cast_weights = m.prev_ldm_patched_cast_weights
        del m.prev_ldm_patched_cast_weights
    m.weight_function = None
    m.bias_function = None

class LowVramPatch:
    def __init__(self, key, patches):
        self.key = key
        self.patches = patches
    def __call__(self, weight):
        return ldm_patched.modules.lora.calculate_weight(self.patches[self.key], weight, self.key, intermediate_dtype=weight.dtype)


class ModelPatcher:
    def __init__(self, model, load_device, offload_device, size=0, weight_inplace_update=False):
        self.size = size
        self.model = model
        if not hasattr(self.model, 'device'):
            logging.debug("Model doesn't have a device attribute.")
            self.model.device = offload_device
        elif self.model.device is None:
            self.model.device = offload_device

        self.patches = {}
        self.backup = {}
        self.object_patches = {}
        self.object_patches_backup = {}
        self.model_options = {"transformer_options":{}}
        self.model_size()
        self.load_device = load_device
        self.offload_device = offload_device
        self.weight_inplace_update = weight_inplace_update
        self.patches_uuid = uuid.uuid4()

        if not hasattr(self.model, 'model_loaded_weight_memory'):
            self.model.model_loaded_weight_memory = 0

        if not hasattr(self.model, 'lowvram_patch_counter'):
            self.model.lowvram_patch_counter = 0

        if not hasattr(self.model, 'model_lowvram'):
            self.model.model_lowvram = False

    def model_size(self):
        if self.size > 0:
            return self.size
        self.size = ldm_patched.modules.model_management.module_size(self.model)
        return self.size

    def loaded_size(self):
        return self.model.model_loaded_weight_memory

    def lowvram_patch_counter(self):
        return self.model.lowvram_patch_counter

    def clone(self):
        n = ModelPatcher(self.model, self.load_device, self.offload_device, self.size, weight_inplace_update=self.weight_inplace_update)
        n.patches = {}
        for k in self.patches:
            n.patches[k] = self.patches[k][:]
        n.patches_uuid = self.patches_uuid

        n.object_patches = self.object_patches.copy()
        n.model_options = copy.deepcopy(self.model_options)
        n.backup = self.backup
        n.object_patches_backup = self.object_patches_backup
        return n

    def is_clone(self, other):
        if hasattr(other, 'model') and self.model is other.model:
            return True
        return False

    def clone_has_same_weights(self, clone):
        if not self.is_clone(clone):
            return False

        if len(self.patches) == 0 and len(clone.patches) == 0:
            return True

        if self.patches_uuid == clone.patches_uuid:
            if len(self.patches) != len(clone.patches):
                logging.warning("WARNING: something went wrong, same patch uuid but different length of patches.")
            else:
                return True

    def memory_required(self, input_shape):
        return self.model.memory_required(input_shape=input_shape)

    def set_model_sampler_cfg_function(self, sampler_cfg_function, disable_cfg1_optimization=False):
        if len(inspect.signature(sampler_cfg_function).parameters) == 3:
            self.model_options["sampler_cfg_function"] = lambda args: sampler_cfg_function(args["cond"], args["uncond"], args["cond_scale"]) #Old way
        else:
            self.model_options["sampler_cfg_function"] = sampler_cfg_function
        if disable_cfg1_optimization:
            self.model_options["disable_cfg1_optimization"] = True

    def set_model_sampler_post_cfg_function(self, post_cfg_function, disable_cfg1_optimization=False):
        self.model_options = set_model_options_post_cfg_function(self.model_options, post_cfg_function, disable_cfg1_optimization)

    def set_model_sampler_pre_cfg_function(self, pre_cfg_function, disable_cfg1_optimization=False):
        self.model_options = set_model_options_pre_cfg_function(self.model_options, pre_cfg_function, disable_cfg1_optimization)

    def set_model_unet_function_wrapper(self, unet_wrapper_function: UnetWrapperFunction):
        self.model_options["model_function_wrapper"] = unet_wrapper_function

    def set_model_vae_encode_wrapper(self, wrapper_function):
        self.model_options["model_vae_encode_wrapper"] = wrapper_function

    def set_model_vae_decode_wrapper(self, wrapper_function):
        self.model_options["model_vae_decode_wrapper"] = wrapper_function

    def set_model_vae_regulation(self, vae_regulation):
        self.model_options["model_vae_regulation"] = vae_regulation

    def set_model_denoise_mask_function(self, denoise_mask_function):
        self.model_options["denoise_mask_function"] = denoise_mask_function

    def set_model_patch(self, patch, name):
        to = self.model_options["transformer_options"]
        if "patches" not in to:
            to["patches"] = {}
        to["patches"][name] = to["patches"].get(name, []) + [patch]

    def set_model_patch_replace(self, patch, name, block_name, number, transformer_index=None):
        self.model_options = set_model_options_patch_replace(self.model_options, patch, name, block_name, number, transformer_index=transformer_index)

    def set_model_attn1_patch(self, patch):
        self.set_model_patch(patch, "attn1_patch")

    def set_model_attn2_patch(self, patch):
        self.set_model_patch(patch, "attn2_patch")

    def set_model_attn1_replace(self, patch, block_name, number, transformer_index=None):
        self.set_model_patch_replace(patch, "attn1", block_name, number, transformer_index)

    def set_model_attn2_replace(self, patch, block_name, number, transformer_index=None):
        self.set_model_patch_replace(patch, "attn2", block_name, number, transformer_index)

    def set_model_attn1_output_patch(self, patch):
        self.set_model_patch(patch, "attn1_output_patch")

    def set_model_attn2_output_patch(self, patch):
        self.set_model_patch(patch, "attn2_output_patch")

    def set_model_input_block_patch(self, patch):
        self.set_model_patch(patch, "input_block_patch")

    def set_model_input_block_patch_after_skip(self, patch):
        self.set_model_patch(patch, "input_block_patch_after_skip")

    def set_model_output_block_patch(self, patch):
        self.set_model_patch(patch, "output_block_patch")

    def add_object_patch(self, name, obj):
        self.object_patches[name] = obj

    def get_model_object(self, name):
        if name in self.object_patches:
            return self.object_patches[name]
        else:
            if name in self.object_patches_backup:
                return self.object_patches_backup[name]
            else:
                return ldm_patched.modules.utils.get_attr(self.model, name)

    def model_patches_to(self, device):
        to = self.model_options["transformer_options"]
        if "patches" in to:
            patches = to["patches"]
            for name in patches:
                patch_list = patches[name]
                for i in range(len(patch_list)):
                    if hasattr(patch_list[i], "to"):
                        patch_list[i] = patch_list[i].to(device)
        if "patches_replace" in to:
            patches = to["patches_replace"]
            for name in patches:
                patch_list = patches[name]
                for k in patch_list:
                    if hasattr(patch_list[k], "to"):
                        patch_list[k] = patch_list[k].to(device)
        if "model_function_wrapper" in self.model_options:
            wrap_func = self.model_options["model_function_wrapper"]
            if hasattr(wrap_func, "to"):
                self.model_options["model_function_wrapper"] = wrap_func.to(device)

    def model_dtype(self):
        if hasattr(self.model, "get_dtype"):
            return self.model.get_dtype()

    def add_patches(self, patches, strength_patch=1.0, strength_model=1.0):
        p = set()
        model_sd = self.model.state_dict()
        for k in patches:
            offset = None
            function = None
            if isinstance(k, str):
                key = k
            else:
                offset = k[1]
                key = k[0]
                if len(k) > 2:
                    function = k[2]

            if key in model_sd:
                p.add(k)
                current_patches = self.patches.get(key, [])
                current_patches.append((strength_patch, patches[k], strength_model, offset, function))
                self.patches[key] = current_patches

        self.patches_uuid = uuid.uuid4()
        return list(p)

    def get_key_patches(self, filter_prefix=None):
        model_sd = self.model_state_dict()
        p = {}
        for k in model_sd:
            if filter_prefix is not None:
                if not k.startswith(filter_prefix):
                    continue
            bk = self.backup.get(k, None)
            if bk is not None:
                weight = bk.weight
            else:
                weight = model_sd[k]
            if k in self.patches:
                p[k] = [weight] + self.patches[k]
            else:
                p[k] = (weight,)
        return p

    def model_state_dict(self, filter_prefix=None):
        sd = self.model.state_dict()
        keys = list(sd.keys())
        if filter_prefix is not None:
            for k in keys:
                if not k.startswith(filter_prefix):
                    sd.pop(k)
        return sd

    def patch_weight_to_device(self, key, device_to=None, inplace_update=False):
        if key not in self.patches:
            return

        weight = ldm_patched.modules.utils.get_attr(self.model, key)

        inplace_update = self.weight_inplace_update or inplace_update

        if key not in self.backup:
            self.backup[key] = collections.namedtuple('Dimension', ['weight', 'inplace_update'])(weight.to(device=self.offload_device, copy=inplace_update), inplace_update)

        if device_to is not None:
            temp_weight = ldm_patched.modules.model_management.cast_to_device(weight, device_to, torch.float32, copy=True)
        else:
            temp_weight = weight.to(torch.float32, copy=True)
        out_weight = ldm_patched.modules.lora.calculate_weight(self.patches[key], temp_weight, key)
        out_weight = ldm_patched.float.stochastic_rounding(out_weight, weight.dtype, seed=string_to_seed(key))
        if inplace_update:
            ldm_patched.modules.utils.copy_to_param(self.model, key, out_weight)
        else:
            ldm_patched.modules.utils.set_attr_param(self.model, key, out_weight)

    def patch_model(self, device_to=None, patch_weights=True):
        for k in self.object_patches:
            old = ldm_patched.modules.utils.set_attr(self.model, k, self.object_patches[k])
            if k not in self.object_patches_backup:
                self.object_patches_backup[k] = old

        if patch_weights:
            model_sd = self.model_state_dict()
            keys_sort = []
            for key in self.patches:
                if key not in model_sd:
                    logging.warning("could not patch. key doesn't exist in model: {}".format(key))
                    continue
                keys_sort.append((math.prod(model_sd[key].shape), key))
            keys_sort.sort(reverse=True)
            for ks in keys_sort:
                self.patch_weight_to_device(ks[1], device_to)

            if device_to is not None:
                self.model.to(device_to)
                self.model.device = device_to
                self.model.model_loaded_weight_memory = self.model_size()

        return self.model

    def lowvram_load(self, device_to=None, lowvram_model_memory=0, force_patch_weights=False, full_load=False):
        mem_counter = 0
        patch_counter = 0
        lowvram_counter = 0
        loading = []
        for n, m in self.model.named_modules():
            if hasattr(m, "ldm_patched_cast_weights") or hasattr(m, "weight"):
                loading.append((ldm_patched.modules.model_management.module_size(m), n, m))
        load_completely = []
        loading.sort(reverse=True)
        for x in loading:
            n = x[1]
            m = x[2]
            module_mem = x[0]
            lowvram_weight = False
            if not full_load and hasattr(m, "ldm_patched_cast_weights"):
                if mem_counter + module_mem >= lowvram_model_memory:
                    lowvram_weight = True
                    lowvram_counter += 1
                    if hasattr(m, "prev_ldm_patched_cast_weights"): #Already lowvramed
                        continue

            weight_key = "{}.weight".format(n)
            bias_key = "{}.bias".format(n)

            if lowvram_weight:
                if weight_key in self.patches:
                    if force_patch_weights:
                        self.patch_weight_to_device(weight_key)
                    else:
                        m.weight_function = LowVramPatch(weight_key, self.patches)
                        patch_counter += 1
                if bias_key in self.patches:
                    if force_patch_weights:
                        self.patch_weight_to_device(bias_key)
                    else:
                        m.bias_function = LowVramPatch(bias_key, self.patches)
                        patch_counter += 1

                m.prev_ldm_patched_cast_weights = m.ldm_patched_cast_weights
                m.ldm_patched_cast_weights = True
            else:
                if hasattr(m, "ldm_patched_cast_weights"):
                    if m.ldm_patched_cast_weights:
                        wipe_lowvram_weight(m)

                if hasattr(m, "weight"):
                    mem_counter += module_mem
                    load_completely.append((module_mem, n, m))

        load_completely.sort(reverse=True)
        for x in load_completely:
            n = x[1]
            m = x[2]
            weight_key = "{}.weight".format(n)
            bias_key = "{}.bias".format(n)
            param = list(m.parameters())
            if len(param) > 0:
                weight = param[0]
                if weight.device == device_to:
                    continue

            self.patch_weight_to_device(weight_key, device_to=device_to)
            self.patch_weight_to_device(bias_key, device_to=device_to)
            logging.debug("lowvram: loaded module regularly {} {}".format(n, m))

        for x in load_completely:
            x[2].to(device_to)

        if lowvram_counter > 0:
            logging.info("loaded partially {} {} {}".format(lowvram_model_memory / (1024 * 1024), mem_counter / (1024 * 1024), patch_counter))
            self.model.model_lowvram = True
        else:
            logging.info("loaded completely {} {}".format(lowvram_model_memory / (1024 * 1024), mem_counter / (1024 * 1024)))
            self.model.model_lowvram = False
        self.model.lowvram_patch_counter += patch_counter
        self.model.device = device_to
        self.model.model_loaded_weight_memory = mem_counter


    def patch_model_lowvram(self, device_to=None, lowvram_model_memory=0, force_patch_weights=False):
        self.patch_model(device_to, patch_weights=False)
        self.lowvram_load(device_to, lowvram_model_memory=lowvram_model_memory, force_patch_weights=force_patch_weights)
        return self.model

    def unpatch_model(self, device_to=None, unpatch_weights=True):
        if unpatch_weights:
            if self.model.model_lowvram:
                for m in self.model.modules():
                    wipe_lowvram_weight(m)

                self.model.model_lowvram = False
                self.model.lowvram_patch_counter = 0

            keys = list(self.backup.keys())

            for k in keys:
                bk = self.backup[k]
                if bk.inplace_update:
                    ldm_patched.modules.utils.copy_to_param(self.model, k, bk.weight)
                else:
                    ldm_patched.modules.utils.set_attr_param(self.model, k, bk.weight)

            self.backup.clear()

            if device_to is not None:
                self.model.to(device_to)
                self.model.device = device_to
            self.model.model_loaded_weight_memory = 0

        keys = list(self.object_patches_backup.keys())
        for k in keys:
            ldm_patched.modules.utils.set_attr(self.model, k, self.object_patches_backup[k])

        self.object_patches_backup.clear()

    def partially_unload(self, device_to, memory_to_free=0):
        memory_freed = 0
        patch_counter = 0
        unload_list = []

        for n, m in self.model.named_modules():

            shift_lowvram = False
            if hasattr(m, "ldm_patched_cast_weights"):
                module_mem = ldm_patched.modules.model_management.module_size(m)
                unload_list.append((module_mem, n, m))

        unload_list.sort()
        for unload in unload_list:
            if memory_to_free < memory_freed:
                break
            module_mem = unload[0]
            n = unload[1]
            m = unload[2]
            weight_key = "{}.weight".format(n)
            bias_key = "{}.bias".format(n)

            if m.weight is not None and m.weight.device != device_to:
                for key in [weight_key, bias_key]:
                    bk = self.backup.get(key, None)
                    if bk is not None:
                        if bk.inplace_update:
                            ldm_patched.modules.utils.copy_to_param(self.model, key, bk.weight)
                        else:
                            ldm_patched.modules.utils.set_attr_param(self.model, key, bk.weight)
                        self.backup.pop(key)

                m.to(device_to)
                if weight_key in self.patches:
                    m.weight_function = LowVramPatch(weight_key, self.patches)
                    patch_counter += 1
                if bias_key in self.patches:
                    m.bias_function = LowVramPatch(bias_key, self.patches)
                    patch_counter += 1

                m.prev_ldm_patched_cast_weights = m.ldm_patched_cast_weights
                m.ldm_patched_cast_weights = True
                memory_freed += module_mem
                logging.debug("freed {}".format(n))

        self.model.model_lowvram = True
        self.model.lowvram_patch_counter += patch_counter
        self.model.model_loaded_weight_memory -= memory_freed
        return memory_freed

    def partially_load(self, device_to, extra_memory=0):
        self.unpatch_model(unpatch_weights=False)
        self.patch_model(patch_weights=False)
        full_load = False
        if self.model.model_lowvram == False:
            return 0
        if self.model.model_loaded_weight_memory + extra_memory > self.model_size():
            full_load = True
        current_used = self.model.model_loaded_weight_memory
        self.lowvram_load(device_to, lowvram_model_memory=current_used + extra_memory, full_load=full_load)
        return self.model.model_loaded_weight_memory - current_used

    def current_loaded_device(self):
        return self.model.device
    
    def calculate_weight(self, patches, weight, key, intermediate_dtype=torch.float32):
        print("WARNING the ModelPatcher.calculate_weight function is deprecated, please use: ldm_patched.modules.lora.calculate_weight instead")
        return ldm_patched.modules.lora.calculate_weight(patches, weight, key, intermediate_dtype=intermediate_dtype)
    