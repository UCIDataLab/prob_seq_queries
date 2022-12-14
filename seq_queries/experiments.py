#################################################################################
#
#             Project Title:  Sequential Queries
#             Date:           2022.04.11
#
#################################################################################


#################################################################################
#   Module Imports
#################################################################################

import os
import sys
import numpy as np
from collections import defaultdict
from itertools import product
import torch
import torch.nn as nn
import torch.nn.functional as F
from datetime import datetime

from tqdm import tqdm
# from .data import load_text, process_data
from .model import CausalLM, MaskedLM
from .arguments import get_args
from .tree import BeamSearchSampleTree
from .sample import *
from .data import *
from .train import load_checkpoint, get_model
from .utils import read_pkl, write_pkl, compute_num_beams_from_budget, set_random_seed

ROOT =os.path.abspath(os.path.join(__file__,"../../"))


#################################################################################
#   Function-Class Declaration
#################################################################################


def prep_experiment(
    config_path,
    name="shakespeare",
    need_train=False,
    need_val=True,
    need_test=False,
    device=0,
    extra_args = {}
 ):
    args = get_args(manual_config=config_path)
    set_random_seed(args)
    name = name.lower()
    config_roster = {
        "flashy_gpt2": {
            "checkpoint_path": None,
            "data_path": "data/wikitext/wikitext_val-dl.csv",
            "seq_len": 20,
            "vocab_size": 50257,
            "batch_size":512,
            "use_gpt2":True,
            "checkpoint_path":None,
            "needs_dl":False,
            "fixed_seq_len": 30,
            "excluded_terms":[30,13,0,26],
            "flashy":True,
        },
        "wikitext": {
            "checkpoint_path": None,
            "data_path": "data/wikitext/wikitext_val-dl.csv",
            "seq_len": 20,
            "vocab_size": 50257,
            "batch_size":512,
            "use_gpt2":True,
            "checkpoint_path":None,
            "needs_dl":True,
        },
        "amazon": {
            "checkpoint_path": f"{ROOT}/models/amazon/",
            "data_path": "data/amazon/amazon_text_dict.pkl",
            "hidden_size": 512,
            "seq_len": 15,
            "vocab_size": 30,
            "val_data_pct": 0.0001,
            "needs_dl":True,
        },

        "flashy_apps": {
            "checkpoint_path": f"{ROOT}/models/apps/",
            "data_path": "data/apps/lsapp.tsv",
            "hidden_size": 512,
            "fixed_seq_len": 100,
            "vocab_size": 88,
            "val_data_pct": 0.001,
            "needs_dl":False,
        },
        "apps": {
            "checkpoint_path": f"{ROOT}/models/apps/",
            "data_path": "data/apps/lsapp.tsv",
            "hidden_size": 512,
            "seq_len": 15,
            "vocab_size": 88,
            "val_data_pct": 0.001,
            "needs_dl":True,
        },
        "moocs": {
            "checkpoint_path": f"{ROOT}/models/moocs/",
            "data_path": "data/moocs/mooc.csv",
            "hidden_size": 128,
            "seq_len": 15,
            "vocab_size": 98,
            "val_data_pct": 0.01,
            "needs_dl":True,
        },
        "shakespeare": {
            "checkpoint_path": f"{ROOT}/models/shakespeare/",
            "data_path": "data/shakespeare/shakespeare_input.txt",
            "hidden_size": 128,
            "seq_len": 100,
            "vocab_size": 68,
            "val_data_pct": 0.05,
            "needs_dl":True,
        },
    }

    load_roster = {
        "amazon": load_amazon_data,
        "apps": load_app_data,
        "moocs": load_mooc_data,
        "shakespeare": load_text_data,
        "wikitext": load_wikitext_data,
        "flashy_gpt2": load_flashy_gpt_data,
        "flashy_apps": load_flashy_apps,
    }
    assert name in load_roster,\
        "Dataset {} not found in roster"
    process_roster = {
        "amazon": process_amazon_data,
        "wikitext":process_wikitext_data,
        "apps": process_app_mooc_data,
        "moocs": process_app_mooc_data,
        "shakespeare": process_text_data,
    }
    for argument,details in config_roster[name].items():
        args.__dict__[argument] = details
    for argument,details in extra_args.items():
        args.__dict__[argument] = details
    args.device=device
    args.dataset = name
    text_dict= load_roster[name](args.data_path,args)
    args.text_dict = text_dict
    if args.needs_dl:
        train_dl, val_dl, test_dl = process_roster[name](
            text_dict, args,
            need_train=need_train,
            need_val=need_val,
            need_test=need_test,
        )

    model = get_model(args)
    if args.checkpoint_path:
        load_checkpoint(args, model)
    model.eval()
    print("====="*10)

    return {
        "train_dl": train_dl if need_train else None,
        "test_dl": test_dl if need_test else None,
        "val_dl": val_dl if args.needs_dl else None,
        "args": args,
        "model":model,
    }


def tau_ab_inf_horizon_query(
    args,
    dataloader,
    model=None,
    sample_artifacts=["sample_estimates",'sample_estimate_var','sample_estimate_mean','model_iters','num_mc_samples',
                      'intermediate_query_probs','efficiency'],
    hybrid_artifacts=["bs_lower_bound",'is_estimates','sample_estimates','model_iters','intermediate_query_probs',
                      'sample_estimate_var','sample_estimate_mean','num_beams','num_mc_samples'],
    search_artifacts=['true_coverage','restricted_coverage','num_beams', 'model_iters',
                      'bs_lower_bound','intermediate_lbs'],
 ):

    args.model = model; print();
    output = {}
    artifact_store_roster = {
        "beam_search_lower_bound":search_artifacts,
        "mc_estimate":sample_artifacts,
        "mc_pseudo_gt":sample_artifacts,
    }

    def _add_output(key, data,output=output):
        if key not in output: output[key] = []
        output_data =[db[key] for db in data]
        if not isinstance(output_data[0], (torch.Tensor, torch.LongTensor)):
            output_data =[torch.Tensor(db[key]) for db in data]
        output[key] += output_data

    def _consolidate_output(key,output=output):
        if isinstance(output[key],(torch.Tensor, torch.LongTensor)):
            return
        elif ((len(output[key][0].shape) == 1) or
              (len(output[key][0].shape) == 2 and
               ((args.sub_estimates) or
               (key =='intermediate_lbs')))):
            output[key] = torch.stack(output[key]).squeeze()
        elif len(output[key][0].shape) >= 1:
            output[key] = torch.cat(output[key])

    artifacts = artifact_store_roster[args.estimate_type.__name__]
    artifacts += ['tau_a_estimates','tau_b_estimates']
    args.excluded_terms = args.tau_a_excl_terms + args.tau_b_excl_terms
    for dbatch in tqdm(dataloader, disable=args.disable_tqdm):
        data_list = []
        data_batch =[dbatch[i,:args.hist_len] for i in range(dbatch.shape[0])]

        for i in range(dbatch.shape[0]):
            if (args.use_gpt2 and
                args.disable_tqdm):
                print(f"[{datetime.now()}] - {i}",flush=True)
            elif i%10 == 0 and args.disable_tqdm:
                print(".",end="",flush=True)
            sample = data_batch[i]
            args.seq_len = 30

            kwargs = vars(args)
            sample_output =args.estimate_type(sample,**kwargs)
            # (samples, seq_len, vocab)
            if args.estimate_type.__name__ == 'beam_search_lower_bound':
                intermediate_query_probs = sample_output['intermediate_lbs']
            else:
                intermediate_query_probs = sample_output['intermediate_query_probs'].squeeze(0)
            intermediate_query_probs = torch.cumsum(
                intermediate_query_probs.squeeze(0),dim=0).squeeze()
            sample_output['tau_a_estimates'] = torch.gather(
                intermediate_query_probs, -1,
                torch.tensor([args.tau_a_excl_terms]*(args.max_k+1))).squeeze().sum(dim=-1)
            sample_output['tau_b_estimates'] = torch.gather(
                intermediate_query_probs, -1,
                torch.tensor([args.tau_b_excl_terms]*(args.max_k+1))).squeeze().sum(dim=-1)

            data_list.append(sample_output)

        print("",flush=True)
        assert args.estimate_type.__name__ in artifact_store_roster,\
            f"Estimate type {args.estimate_type.__name__} not found"
        artifacts = artifact_store_roster[args.estimate_type.__name__]
        for art in artifacts:
            _add_output(art,data_list)

    for art in artifacts:
        _consolidate_output(art)

    args.model = None
    output['metadata'] = vars(args)
    return output


@torch.no_grad()
def flashy_query(
    args,
    dataloader,
    model=None,
    sample_artifacts=["sample_estimates",'sample_estimate_var','sample_estimate_mean','entropy_probs',
                      'model_iters','num_mc_samples','intermediate_query_probs','sample_counts','samples'],
    hybrid_artifacts=["bs_lower_bound",'is_estimates','sample_estimates','model_iters',
                      'sample_estimate_var','sample_estimate_mean','num_beams','num_mc_samples'],
    search_artifacts=['true_coverage','restricted_coverage','num_beams', 'model_iters',
                      'bs_lower_bound','intermediate_lbs'],
    **kwargs,):
    """Sample from any of these methods given an
    input dataloader, arguments, and potentially a model

    :dataloader: TODO
    :args: TODO
    :model: TODO
    :: TODO
    :returns: TODO

    """
    args.model = model; print();
    output = {}
    artifact_store_roster = {
        "beam_search_is_hybrid": hybrid_artifacts,
        "beam_search_lower_bound":search_artifacts,
        "mc_estimate":sample_artifacts,
        "mc_pseudo_gt":sample_artifacts,
    }

    def _tensor_output(key, data,output=output):
        if key not in output: output[key] = []
        output[key].append(torch.Tensor([db[key] for db in data]))

    def _add_output(key, data,output=output):
        if key not in output: output[key] = []
        output_data =[db[key] for db in data]
        if not isinstance(output_data[0], (torch.Tensor, torch.LongTensor)):
            output_data =[torch.Tensor(db[key]) for db in data]
        output[key] += output_data

    def _consolidate_output(key,output=output):
        if isinstance(output[key],(torch.Tensor, torch.LongTensor)):
            return
        elif ((len(output[key][0].shape) == 1) or
              (len(output[key][0].shape) == 2 and
               ((args.sub_estimates) or
               (key =='intermediate_lbs')))):
            output[key] = torch.stack(output[key]).squeeze()
        elif len(output[key][0].shape) >= 1:
            output[key] = torch.cat(output[key])

    all_excluded_terms = [];
    artifacts = artifact_store_roster[args.estimate_type.__name__]
    data_list = []
    for i,sample in tqdm(enumerate(args.text_dict['text']),
                         disable=args.disable_tqdm):
        sample = torch.LongTensor(sample)
        if args.dataset == "flashy_apps":
            args.excluded_terms = list(set(range(args.vocab_size)) - set([sample[0].item()]))
            all_excluded_terms.append(torch.LongTensor(args.excluded_terms))

        if (args.use_gpt2 and
            args.disable_tqdm):
            print(f"[{datetime.now()}] - {i}",flush=True)
        elif i%10 == 0 and args.disable_tqdm:
            print(".",end="",flush=True)
        args.seq_len = args.fixed_seq_len

        kwargs = vars(args)
        sample_output =args.estimate_type(sample,**kwargs)
        data_list.append(sample_output)

        print("",flush=True)
        assert args.estimate_type.__name__ in artifact_store_roster,\
            f"Estimate type {args.estimate_type.__name__} not found"
        artifacts = artifact_store_roster[args.estimate_type.__name__]

    for art in artifacts:
        _add_output(art,data_list)

    for art in artifacts:
        _consolidate_output(art)

    args.model = None
    output['metadata'] = vars(args)
    output['excluded_terms'] = torch.LongTensor(args.excluded_terms)
    return output

#######################################################################
# Static token
#######################################################################




@torch.no_grad()
def sample_dynamic_target_token(
    args,
    dataloader,
    model=None,
    sample_artifacts=["sample_estimates",'sample_estimate_var','sample_estimate_mean','entropy_probs',
                      'model_iters','num_mc_samples','intermediate_query_probs'],
    hybrid_artifacts=["bs_lower_bound",'is_estimates','sample_estimates','model_iters',
                      'sample_estimate_var','sample_estimate_mean','num_beams','num_mc_samples'],
    search_artifacts=['true_coverage','restricted_coverage','num_beams', 'model_iters',
                      'bs_lower_bound','intermediate_lbs'],
    **kwargs,):
    """Sample from any of these methods given an
    input dataloader, arguments, and potentially a model

    :dataloader: TODO
    :args: TODO
    :model: TODO
    :: TODO
    :returns: TODO

    """
    args.model = model; print();
    output = {}
    artifact_store_roster = {
        "beam_search_is_hybrid": hybrid_artifacts,
        "beam_search_lower_bound":search_artifacts,
        "mc_estimate":sample_artifacts,
        "mc_pseudo_gt":sample_artifacts,
    }

    def _tensor_output(key, data,output=output):
        if key not in output: output[key] = []
        output[key].append(torch.Tensor([db[key] for db in data]))

    def _add_output(key, data,output=output):
        if key not in output: output[key] = []
        output_data =[db[key] for db in data]
        if not isinstance(output_data[0], (torch.Tensor, torch.LongTensor)):
            output_data =[torch.Tensor(db[key]) for db in data]
        output[key] += output_data

    def _consolidate_output(key,output=output):
        if isinstance(output[key],(torch.Tensor, torch.LongTensor)):
            return
        elif ((len(output[key][0].shape) == 1) or
              (len(output[key][0].shape) == 2 and
               ((args.sub_estimates) or
               (key =='intermediate_lbs')))):
            output[key] = torch.stack(output[key]).squeeze()
        elif len(output[key][0].shape) >= 1:
            output[key] = torch.cat(output[key])

    model_budget = None; model_budget_name = ""; model_budget_i =0
    if args.frequentist_test:
        artifact_store_roster['mc_estimate'].append('frequentist_estimates')
    if args.model_budget_filepath:
        model_budget_file = read_pkl(args.model_budget_filepath)
        model_budget = model_budget_file['model_iters']
    elif 'num_mc_samples' in hybrid_artifacts:
        hybrid_artifacts.remove('num_mc_samples')

    all_excluded_terms = [];
    artifacts = artifact_store_roster[args.estimate_type.__name__]
    for dbatch in tqdm(dataloader, disable=args.disable_tqdm):
        data_list = []
        if not args.query_2:
            all_excluded_terms.append(dbatch[:,args.total_seq_len].cpu())
        data_batch =[dbatch[i,:args.hist_len] for i in range(dbatch.shape[0])]

        for i in range(dbatch.shape[0]):
            if (args.use_gpt2 and
                args.disable_tqdm):
                print(f"[{datetime.now()}] - {i}",flush=True)
            elif i%10 == 0 and args.disable_tqdm:
                print(".",end="",flush=True)
            sample = data_batch[i]
            args.seq_len = args.total_seq_len - args.hist_len
            if not args.query_2:
                args.excluded_terms = [dbatch[i,args.total_seq_len].cpu().item()]
            else: args.excluded_terms = []

            if args.model_budget_filepath:
                if args.estimate_type.__name__ == "mc_estimate":
                    args.sub_estimates = (torch.div(model_budget[model_budget_i],args.seq_len,
                                                    rounding_mode="trunc").long() +
                                        ((model_budget[model_budget_i]%args.seq_len > 0).long())).tolist()
                    args.num_mc_samples = args.sub_estimates[-1]
                elif args.estimate_type.__name__ == "beam_search_lower_bound":
                    init_sub_estimates = (torch.div(model_budget[model_budget_i],args.seq_len,
                                                    rounding_mode="trunc").long() +
                                        ((model_budget[model_budget_i]%args.seq_len > 0).long())).tolist()
                    args.sub_estimates = [
                        compute_num_beams_from_budget(args.vocab_size,init_beam,args.seq_len,args.excluded_terms)
                        for init_beam in init_sub_estimates]
                    args.num_beams = args.sub_estimates[-1]
                    assert isinstance(args.num_beams,int),"Num beams for model budget has to be an int"

            kwargs = vars(args)
            sample_output =args.estimate_type(sample,**kwargs)

            if args.long_seq_ablation:
                # Ablation for importance sampling long sequences
                intermediate_query_probs = sample_output['intermediate_query_probs']
                intermediate_sub_estimates = intermediate_query_probs[...,args.intermediate_seqs,:]
                sample_output['intermediate_query_probs'] = torch.stack([
                    intermediate_sub_estimates[:,:samp].mean(dim=1)
                    for samp in args.sub_estimates
                ] + [intermediate_sub_estimates.mean(dim=1)],dim=1)
                assert (sample_output['intermediate_query_probs'].shape[1] == len(args.sub_estimates)+1 and
                        sample_output['intermediate_query_probs'].shape[2] == len(args.intermediate_seqs)),\
                    "Error: Expected {} sub estimates and {} intermediate sequences but got: sub estimates {} and seq {}".format(
                        len(args.sub_estimates)+1, len(args.intermediate_seqs),
                        sample_output['intermediate_query_probs'].shape[1],
                        sample_output['intermediate_query_probs'].shape[2],
                    )

            data_list.append(sample_output)
            model_budget_i += 1

        print("",flush=True)
        assert args.estimate_type.__name__ in artifact_store_roster,\
            f"Estimate type {args.estimate_type.__name__} not found"
        artifacts = artifact_store_roster[args.estimate_type.__name__]
        for art in artifacts:
            _add_output(art,data_list)

    for art in artifacts:
        _consolidate_output(art)

    args.model = None
    output['metadata'] = vars(args)
    if not args.query_2:
        output['excluded_terms'] = torch.cat(all_excluded_terms,dim=0).cpu()
    return output

#######################################################################
# Static token
#######################################################################


def variance_ablation(
    hist, seq_len, model, interp_func,excluded_terms,
    batch_size, device, vocab_size, num_intervals=100, **kwargs
 ):
     # (beams)
     q_log_probs, p_log_cond = _get_joint_log_prob_of_all_seqs(
        hist, seq_len, model, interp_func,excluded_terms,
        batch_size, device, vocab_size, **kwargs)

     q_log_probs, inds = torch.sort(q_log_probs,
                                descending=True)
     p_log_cond = p_log_cond[inds]
     p_log_joint = (q_log_probs + p_log_cond)

     beam_search_step_size = int(q_log_probs.shape[0]/num_intervals)
     variances = [];

     global_var = None
     for i in range(num_intervals):
         part_p_joint = p_log_joint[i:].exp()
         part_q_probs = q_log_probs[i:].exp()
         part_q_probs /= part_q_probs.sum()

         w_x = (part_p_joint/part_q_probs)
         variance = (part_q_probs*torch.pow(w_x - w_x.mean(),2)).sum()
         if i == 0:
             global_var = variance
         variances.append(variance/global_var)

     return torch.Tensor(variances)


def _get_joint_log_prob_of_all_seqs(
    hist, seq_len, model, interp_func,excluded_terms,
    batch_size, device, vocab_size, **kwargs):
    """
    Examines the variance of the importance sampling estimate
    of the number of paths remaining before and after beam search.

    """

    assert len(excluded_terms) == 1,\
        "Ambiguous choice of excluded term to use"
    excluded_term = excluded_terms[0]
    all_seqs = torch.LongTensor(
        list(product(range(vocab_size),
            repeat=seq_len))
    )
    all_seqs = torch.cat(
        (all_seqs,
         torch.ones((all_seqs.shape[0],1))*excluded_term
        ), dim = 1
    ).long()

    hists=hist.unsqueeze(0).expand(all_seqs.shape[0], -1)
    logits, hidden_state = model.get_next_probs(
        torch.cat((hists,all_seqs), dim=-1),
        return_forward_only=True,
        rnn_args=None,
        device=device,
        max_batch_size=batch_size,
        return_logits=True,
    )

    # All but the last probability q(x_1:k)
    model_log_probs = torch.gather(torch.log_softmax(logits[...,-(seq_len+1):,:],dim=-1),
                                   dim=-1, index=all_seqs.unsqueeze(-1)).squeeze(-1)
    q_log_prob = model_log_probs[...,:-1].sum(dim=-1)
    p_log_cond = model_log_probs[...,-1]

    return q_log_prob, p_log_cond




@torch.no_grad()
def beam_search_ablation(
    args,
    dataloader,
    model=None,
    search_artifacts=['num_beams_over_time'],
    **kwargs,):
    """Sample from any of these methods given an
    input dataloader, arguments, and potentially a model

    :dataloader: TODO
    :args: TODO
    :model: TODO
    :: TODO
    :returns: TODO

    """
    args.model = model; print();
    output = {}
    artifact_store_roster = {
        "beam_search_lower_bound":search_artifacts,
    }


    def _add_output(key, data,output=output):
        if key not in output: output[key] = []
        output_data =[db[key] for db in data]
        output[key] += output_data

    all_excluded_terms = [];
    artifacts = artifact_store_roster[args.estimate_type.__name__]
    for dbatch in tqdm(dataloader, disable=args.disable_tqdm):
        data_list = []
        data_batch =[dbatch[i,:args.hist_len] for i in range(dbatch.shape[0])]

        for i in range(dbatch.shape[0]):
            if (args.use_gpt2 and
                args.disable_tqdm):
                print(f"[{datetime.now()}] - {i}",flush=True)
            elif i%10 == 0 and args.disable_tqdm:
                print(".",end="",flush=True)
            sample = data_batch[i]
            args.seq_len = args.total_seq_len - args.hist_len
            args.excluded_terms = []

            kwargs = vars(args)
            sample_output =args.estimate_type(sample,**kwargs)
            data_list.append(sample_output)

        print("",flush=True)
        assert args.estimate_type.__name__ in artifact_store_roster,\
            f"Estimate type {args.estimate_type.__name__} not found"
        artifacts = artifact_store_roster[args.estimate_type.__name__]
        for art in artifacts:
            _add_output(art,data_list)

    args.model = None
    output['metadata'] = vars(args)
    return output

#######################################################################
# Static token
#######################################################################






