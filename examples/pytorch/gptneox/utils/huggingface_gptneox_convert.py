# Copyright (c) 2021-2023, NVIDIA CORPORATION.  All rights reserved.
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

import argparse
import configparser
#import multiprocessing
import numpy as np
from pathlib import Path
import torch 

import os
import sys
from transformers import GPTNeoXForCausalLM # 4.21.1
import traceback
import json

torch.multiprocessing.set_start_method('spawn', force=True)

def get_weight_data_type(data_type):
    if data_type == "fp32":
        return np.float32
    elif data_type == "fp16":
        return np.float16
    else:
        assert False, f"Invalid weight data type {data_type}"

def prefix_prompt_convert(args, config, weight_data_type):

    saved_dir = args.saved_dir + "/%d-gpu/" % args.infer_gpu_num

    prompt_in_file_list = args.prompt_in_file_list.split(',')

    task_list = []
    for idx, prompt_in_file in enumerate(prompt_in_file_list):
        weights=torch.load(prompt_in_file)
        task_name = prompt_in_file.split("/")[-1].split(".")[-3]

        total_size = weights.nelement()
        n_layers = config['num_hidden_layers']
        n_head = config['num_attention_heads']
        size_per_head = config['hidden_size'] // n_head
        prefix_prompt_len = total_size // (2 * n_layers * n_head * size_per_head)

        task_list.append((task_name, prefix_prompt_len))
        # GPT NeoX
        weights=weights.view(prefix_prompt_len,n_layers,2,n_head,size_per_head) ## prefix_seq_len, num_layers, 2, num_heads, size_per_head
        # weights=weights.view(prefix_prompt_len,28,2,16,256) ## prefix_seq_len, num_layers, 2, num_heads, size_per_head
        weights=weights.permute(1,2,3,0,4) ## num_layers, 2, num_heads, perfix_seq_len, size_per_head
        local_head_num = n_head // args.infer_gpu_num
        weights_split = torch.split(weights, local_head_num, dim=2)
        for i in range(args.infer_gpu_num):
            output_file_path = saved_dir + "/model.prefix_prompt." + task_name + ".weight." + str(i) + ".bin"
            weights_split[i].detach().cpu().numpy().astype(weight_data_type).tofile(output_file_path)
        
    return task_list


def split_and_convert_process(i, saved_dir,factor,key,args,config,val):

    if key.find("input_layernorm.weight") != -1 or key.find("input_layernorm.bias") != -1 or \
        key.find("attention.dense.bias") != -1 or key.find("post_attention_layernorm.weight") != -1 or \
        key.find("post_attention_layernorm.bias") != -1 or key.find("mlp.dense_4h_to_h.bias") != -1 or \
        key.find("final_layernorm.weight") != -1 or key.find("final_layernorm.bias") != -1:

        # shared weights, only need to convert the weights of rank 0
        if i == 0:
            saved_path = saved_dir + "/model." + key + ".bin"
            val.tofile(saved_path)

    elif key.find("attention.dense.weight") != -1 or key.find("mlp.dense_4h_to_h.weight") != -1:
        split_vals = np.split(val, factor, axis=0)
        for j in range(factor):
            saved_path = saved_dir + "/model." + key + ".%d.bin" % (i * factor + j)
            split_vals[j].tofile(saved_path)

    elif key.find("mlp.dense_h_to_4h.weight") != -1 or key.find("mlp.dense_h_to_4h.bias") != -1:

        split_vals = np.split(val, factor, axis=-1)
        for j in range(factor):
            saved_path = saved_dir + "/model." + key + ".%d.bin" % (i * factor + j)
            split_vals[j].tofile(saved_path)

    elif key.find("attention.query_key_value.bias") != -1:
        local_dim = (int)(val.shape[-1] / 3)
        n_head = config['num_attention_heads']

        val = val.reshape(n_head, 3, local_dim // n_head)
        val = np.transpose(val, [1, 0, 2]).reshape(3, local_dim)
        split_vals = np.split(val, factor, axis=-1)

        for j in range(factor):
            saved_path = saved_dir + "/model." + key + ".%d.bin" % (i * factor + j)
            split_vals[j].tofile(saved_path)

    elif key.find("attention.query_key_value.weight") != -1:
        hidden_dim = val.shape[0]
        local_dim = (int)(val.shape[-1] / 3)
        n_head = config['num_attention_heads']
        # Note that the HF qkv weight are stored as [hidden_size, num_heads, 3, head_hidden]
        # FT needs the shape of [hidden_size, 3, num_heads, head_hidden]
        val = val.reshape(hidden_dim, n_head, 3, local_dim // n_head)
        val = np.transpose(val, [0, 2, 1, 3]).reshape(hidden_dim, 3, local_dim)

        # print(np.mean(np.abs(val[:, 0, :])))
        split_vals = np.split(val, factor, axis=-1)

        for j in range(factor):
            saved_path = saved_dir + "/model." + key + ".%d.bin" % (i * factor + j)
            split_vals[j].tofile(saved_path)

    else:
        print("[ERROR] cannot find key '{}'".format(key))

def split_and_convert(args):
    saved_dir = args.saved_dir + "/%d-gpu/" % args.infer_gpu_num

    if(os.path.exists(saved_dir) == False):
        os.makedirs(saved_dir)

    t_gpu_num = args.trained_gpu_num
    i_gpu_num = args.infer_gpu_num
    assert(i_gpu_num % t_gpu_num == 0)

    factor = (int)(i_gpu_num / t_gpu_num)

    # in_file = a folder to pytorch checkpoint (with pytorch_model.bin/config.json) or folder to hf checkpoint
    model = GPTNeoXForCausalLM.from_pretrained(args.in_file)
    hf_config = vars(model.config)


    hf_config["gpt_j_residual"] = 1 # Copied from eleutherai_gpt_neox_convert.py, not sure how to obtain this from the model
    #if "gpt_j_residual" not in hf_config:
    #    hf_config["gpt_j_residual"] = 0

    np_weight_data_type = get_weight_data_type(args.weight_data_type)

    task_list = []
    if args.prompt_in_file_list is not None:
        task_list = prefix_prompt_convert(args, hf_config, np_weight_data_type)
    
    try:
        model_name = args.model_name
        config = configparser.ConfigParser()
        config['gptneox'] = {}
        config['gptneox']['model_name'] = model_name
        config['gptneox']["head_num"] = str(hf_config['num_attention_heads'])
        n_embd = hf_config["hidden_size"]
        config['gptneox']["size_per_head"] = str(n_embd // hf_config['num_attention_heads'])
        config['gptneox']["vocab_size"] = str(hf_config["vocab_size"])
        config['gptneox']["num_layer"] = str(hf_config["num_hidden_layers"])


        # Hardcode following to avoid garabage output
        rotary_dim = 24 # Copied from eleutherai_gpt_neox_convert.py, not sure how to obtain this number from the model 
        #rotary_dim = n_embd // hf_config['num_attention_heads'] if "rotary_dim" not in hf_config or hf_config["rotary_dim"] is None else hf_config["rotary_dim"]

        config['gptneox']["rotary_embedding"] = str(rotary_dim)
        config['gptneox']["start_id"] = str(hf_config["bos_token_id"]) # '0'
        config['gptneox']["end_id"] = str(hf_config["eos_token_id"]) # '2' according to the eleutherai_gpt_neox_convert.py
        config['gptneox']["inter_size"] = str(n_embd * 4)
        config['gptneox']['use_gptj_residual'] = str(int(hf_config['gpt_j_residual']))
        config['gptneox']["weight_data_type"] = args.weight_data_type

        if len(task_list) > 0:
            config['gptneox']['num_tasks'] = str(len(task_list))
            config['gptneox']['prompt_learning_type'] = str(2)
            for idx, (task_name, prompt_length) in enumerate(task_list):
                config[f'task_{idx}'] = {}
                config[f'task_{idx}']['task_name'] = task_name
                config[f'task_{idx}']['prompt_length'] = str(prompt_length)
        with open((Path(saved_dir) / f"config.ini").as_posix(), 'w') as configfile:
            config.write(configfile)
    except BaseException as e:
        print(f"Fail to save the config in config.ini: {e}")
        traceback.print_exc()

    huggingface_model_name_pattern = [
        "ln_1.bias",
        "ln_1.weight",
        "attn.qkv_proj.bias",
        "attn.qkv_proj.weight",
        "attn.out_proj.bias",
        "attn.out_proj.weight",
        "ln_2.bias",
        "ln_2.weight",
        "mlp.fc_in.bias",
        "mlp.fc_in.weight",
        "mlp.fc_out.bias",
        "mlp.fc_out.weight",
    ]
    
    ft_model_name_pattern = [
        "input_layernorm.bias",
        "input_layernorm.weight",
        "attention.query_key_value.bias",
        "attention.query_key_value.weight",
        "attention.dense.bias",
        "attention.dense.weight",
        "post_attention_layernorm.bias",
        "post_attention_layernorm.weight",
        "mlp.dense_h_to_4h.bias",
        "mlp.dense_h_to_4h.weight",
        "mlp.dense_4h_to_h.bias",
        "mlp.dense_4h_to_h.weight",
    ]
    
    pool = torch.multiprocessing.Pool(args.processes)
    for name, param in model.named_parameters():
        if name.find("weight") == -1 and name.find("bias") == -1:
            continue
        #print(name)
        converted = False
        name = name.replace("gpt_neox.", "")
        if name == 'embed_in.weight':
            param.detach().cpu().numpy().astype(np_weight_data_type).tofile(saved_dir + "model.wte.bin")
            converted = True
        elif name == 'final_layer_norm.bias':
            param.detach().cpu().numpy().astype(np_weight_data_type).tofile(saved_dir + "model.final_layernorm.bias.bin")
            converted = True
        elif name == 'final_layer_norm.weight':
            param.detach().cpu().numpy().astype(np_weight_data_type).tofile(saved_dir + "model.final_layernorm.weight.bin")
            converted = True
        elif name == 'embed_out.weight':
            param.detach().cpu().numpy().astype(np_weight_data_type).tofile(saved_dir + "model.lm_head.weight.bin")
            converted = True
        else:
            for i in range(len(huggingface_model_name_pattern)):
                if name.find(huggingface_model_name_pattern[i]) != -1:
                    new_name = name.replace("transformer.h.", "layers.").replace(huggingface_model_name_pattern[i], ft_model_name_pattern[i])
                    pool.starmap(split_and_convert_process,
                                [(0, saved_dir, factor, new_name, args, vars(model.config),
                                    param.detach().cpu().numpy().astype(np_weight_data_type).T)], )
                    converted = True
                elif name.find(ft_model_name_pattern[i]) != -1:
                    new_name = name
                    pool.starmap(split_and_convert_process,
                                [(0, saved_dir, factor, new_name, args, vars(model.config),
                                    param.detach().cpu().numpy().astype(np_weight_data_type).T)], )
                    converted = True

        if not converted:
            print(f"Not converted: {name}")

    pool.close()
    pool.join()

    # Post-process biases if use_gptj_residual is True
    if hf_config['gpt_j_residual']:
        for layer_idx in range(hf_config["num_hidden_layers"]):
            attn_bias = np.fromfile(saved_dir + f"/model.layers.{layer_idx}.attention.dense.bias.bin", dtype=np.float32)
            mlp_bias =  np.fromfile(saved_dir + f"/model.layers.{layer_idx}.mlp.dense_4h_to_h.bias.bin", dtype=np.float32)

            (attn_bias + mlp_bias).tofile(saved_dir + f"/model.layers.{layer_idx}.mlp.attention.bias.sum.bin")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument('-saved_dir', '-o', type=str, help='file name of output file', required=True)
    parser.add_argument('-in_file', '-i', type=str, help='file name of input checkpoint file', required=True)
    parser.add_argument('-prompt_in_file_list','-p_i_list', type=str, help='list of the prompt weight file path,'
                        'separate by (,). e.g. -prompt_in_file_list prefix_prompt.task0.weight,prefix_prompt.task1.weight')
    parser.add_argument('-trained_gpu_num', '-t_g', type=int, help='How many gpus for inference', default=1)
    parser.add_argument('-infer_gpu_num', '-i_g', type=int, help='How many gpus for inference', required=True)
    parser.add_argument("-processes", "-p", type=int, help="How many processes to spawn for conversion (default: 4)", default=4)
    # Make sure to use fp32, otherwise, we see weird behaviour. The model can still be loaded as fp16 in Triton
    parser.add_argument("-weight_data_type", type=str, default="fp32", choices=["fp32", "fp16"])
    parser.add_argument('-model_name', '-m_n', type=str, help='model name', default="gptneox_20B", required=True)

    args = parser.parse_args()
    print("\n=============== Argument ===============")
    for key in vars(args):
        print("{}: {}".format(key, vars(args)[key]))
    print("========================================")

    split_and_convert(args)
