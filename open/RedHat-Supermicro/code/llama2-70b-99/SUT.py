import os
import sys
import time
import re
import numpy as np
import array
import torch
from torch.nn.functional import pad
from torch.utils.data import DataLoader
from transformers import AutoModelForCausalLM, AutoTokenizer, LlamaForCausalLM
from transformers.generation.streamers import BaseStreamer

import pickle
import time
import threading
import tqdm
import queue

from concurrent.futures.thread import ThreadPoolExecutor
import requests
from urllib3.exceptions import InsecureRequestWarning
import json

import more_itertools as mit
from itertools import repeat

requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

import logging
from typing import TYPE_CHECKING, Optional, List
from pathlib import Path

import mlperf_loadgen as lg
from dataset import Dataset

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("Llama-70B-SUT")

gen_kwargs = {
    "early_stopping": True,
    "max_new_tokens": 1024,
    "min_new_tokens": 1,
    "num_beams": 1,
    "do_sample": False
}



class FirstTokenStreamer(BaseStreamer):
    """ Streams first tokens to a 'holder' """

    def __init__(self, first_token, tokens_cache=[], is_first_token=True, response_ids=[] ):
        """ Response ids added to 'sign' the first token"""

        self.first_token = first_token # Queue for first token
        self.is_first_token = is_first_token

        # Cache for subsequent generated tokens
        self.tokens_cache = tokens_cache

        self.response_ids = response_ids

        self.is_prompt = True # The first tokens sent to the streamer are actually the input prompts

    def put(self, value):
        """ Caches the tokens as they're generated. Assumes bs=1 """

        # Prompts are streamed first so we need to skip the first time value that arrives
        if self.is_prompt:
            self.is_prompt = False
            return

        value = value.item()
        if self.is_first_token:

            # Add generated first token together with its query response_id to first tokens queue
            self.first_token.put((value, self.response_ids[0]))

            self.is_first_token = False
            return

        self.tokens_cache.append(value)


    def end(self):
        pass

    def get_out_tokens(self):
        return self.tokens_cache


class SUT():
    def __init__(self,
                 model_path=None,
                 api_server=None,
                 api_model_name=None,
                 additional_servers=[],
                 vllm=False,
                 dtype="bfloat16",
                 device="cpu",
                 batch_size=None,
                 total_sample_count=24576,
                 dataset_path=None,
                 use_cached_outputs=False,  # Set this to True *only for test accuracy runs* in case your prior session was killed partway through
                 workers=1):

        self.model_path = model_path or "meta-llama/Llama-2-70b-chat-hf"
        self.api_servers = []
        if api_server:
            self.api_servers.append(api_server)
        if additional_servers and not api_server:
            sys.exit("Additional servers cannot be used without primary api server")
        for server in additional_servers:
            self.api_servers.append(server)
        self.api_model_name = api_model_name
        self.vllm = vllm
        self.device = device

        if not batch_size:
            if device == "cpu":
                batch_size = 2048
            else:
                batch_size = 32  # Reduce to 8 if using 4 GPUs, 16 for 8.
        self.batch_size = batch_size

        # dtype
        if dtype == 'bfloat16':
            self.amp_enabled = True
            self.amp_dtype = torch.bfloat16
        elif dtype == 'float16':
            self.amp_enabled = True
            self.amp_dtype = torch.float16
        else:
            self.amp_enabled = False
            self.amp_dtype = torch.float32

        if 'cuda' in self.device:
            assert torch.cuda.is_available(), "torch gpu is not available, exiting..."

        self.dataset_path = dataset_path
        self.data_object = Dataset(self.model_path,
                                   dataset_path=self.dataset_path,
                                   total_sample_count=total_sample_count,
                                   device=self.device)
        self.qsl = lg.ConstructQSL(self.data_object.total_sample_count, self.data_object.perf_count,
                                   self.data_object.LoadSamplesToRam, self.data_object.UnloadSamplesFromRam)

        # self.load_model()
        self.tokenizer = AutoTokenizer.from_pretrained(
            self.model_path,
            model_max_length=1024,
            padding_side="left",
            use_fast=True,) #changed from false

        self.tokenizer.pad_token = self.tokenizer.eos_token

        self.num_workers = workers
        self.worker_threads = [None] * self.num_workers
        self.query_queue = queue.Queue()

        self.use_cached_outputs = use_cached_outputs
        self.sample_counter = 0
        self.sample_counter_lock = threading.Lock()


    def start(self):
        # Create worker threads
        for j in range(self.num_workers):
            worker = threading.Thread(target=self.process_queries)
            worker.start()
            self.worker_threads[j] = worker

    def stop(self):
        for _ in range(self.num_workers):
            self.query_queue.put(None)

        for worker in self.worker_threads:
            worker.join()


    def query_api(self, input, idx):
        pass
    
    def query_api_vllm(self, inputs, idx):
        headers = {
            'Content-Type': 'application/json',
        }

        json_data = {
            'model': self.api_model_name,
            'prompt': inputs,
            'max_tokens': 1024,
            'temperature': 0,
        }

        print(json_data)

        response_code = 0
        while response_code != 200:
            try:
                response = requests.post(f'{self.api_servers[idx]}/v1/completions', headers=headers, json=json_data, verify=False)
                response_code = response.status_code
            except:
                print("connection failure")
                exit()
        return [resp["text"] for resp in json.loads(response.text)["choices"]]
    
    def api_action_handler(self, chunk, server_idx):
        # removed the part for grpc
        if self.vllm:
            output = self.query_api_vllm(chunk, server_idx)
        else:
            with ThreadPoolExecutor(max_workers=len(chunk)) as executor:
                output = list(executor.map(self.query_api,chunk, repeat(server_idx)))
        return output

    def process_queries(self):
        """Processor of the queued queries. User may choose to add batching logic """

        while True:
            qitem = self.query_queue.get()
            if qitem is None:
                break

            query_ids = [q.index for q in qitem]

            fname = "q" + "_".join([str(i) for i in query_ids])
            fname = f"run_outputs/{fname}.pkl"
            _p = Path(fname)
            if self.use_cached_outputs and _p.exists():
                # Read cache
                with _p.open(mode="rb") as f:
                    d = pickle.load(f)
                processed_output = d["outputs"]
                tik1 = None
                tik2 = None
                tik3 = None
                tok = None
            else:
                # Construct / collate batch
                max_seq_len = 1024

                tik1 = time.time()

                input_ids_tensor = []
                input_masks_tensor = []
                input_len = []
                for q in qitem:
                    input_ids_tensor.append(pad(self.data_object.input_ids[q.index],
                                                (max_seq_len - self.data_object.input_lens[q.index], 0, 0, 0),
                                                value=self.tokenizer.pad_token_id))
                    input_masks_tensor.append(pad(self.data_object.attention_masks[q.index],
                                                  (max_seq_len - self.data_object.input_lens[q.index], 0, 0, 0),
                                                 value=0))
                    input_len.append(self.data_object.input_lens[q.index])
                input_ids_tensor = torch.cat(input_ids_tensor)
                input_masks_tensor = torch.cat(input_masks_tensor)

                assert input_ids_tensor.shape == input_masks_tensor.shape
                assert input_ids_tensor.shape[0] <= self.batch_size

                if self.api_servers:
                    decoded = self.tokenizer.batch_decode(input_ids_tensor)
                    cleaned = [entry.replace('</s>','').replace('<s>','') for entry in decoded]
                    cleaned_chunks = [list(c) for c in mit.divide(len(self.api_servers), cleaned)]

                tik2 = time.time()

                if self.api_servers:
                    with ThreadPoolExecutor(max_workers=len(self.api_servers)) as executor:
                        #needs to be tested
                        output_chunks = list(executor.map(self.api_action_handler,cleaned_chunks,range(len(self.api_servers))))
                    output = []
                    for row in output_chunks:
                        output += row
                else:
                    print("Error: Specify at least one API to which the request is to be sent!")
                    exit()

                tik3 = time.time()

                if self.api_servers:
                    processed_output = np.array(self.tokenizer(output, padding='longest')['input_ids'])
                    # non api part code removed

            for i in range(len(qitem)):
                unpadded = np.delete(processed_output[i], np.where(processed_output[i] == 2))
                n_tokens = unpadded.shape[0]
                response_array = array.array("B", unpadded.tobytes())
                bi = response_array.buffer_info()
                response = [lg.QuerySampleResponse(qitem[i].id, bi[0], bi[1], n_tokens)]
                lg.QuerySamplesComplete(response)

            tok = time.time()

            with self.sample_counter_lock:
                self.sample_counter += len(qitem)
                print(f"Samples run: {self.sample_counter}")
                if tik1:
                    print(f"\tBatchMaker time: {tik2 - tik1}")
                    print(f"\tInference time: {tik3 - tik2}")
                    print(f"\tPostprocess time: {tok - tik3}")
                    print(f"\t==== Total time: {tok - tik1}")
                else:
                    print(f"\tLoaded from cache: {_p}")

    def get_sut(self):
        self.sut = lg.ConstructSUT(self.issue_queries, self.flush_queries)
        return self.sut

    def get_qsl(self):
        return self.qsl


    def predict(self,**kwargs):
        raise NotImplementedError


    def issue_queries(self, query_samples):
        """ Receives samples from loadgen and adds them to queue. Users may choose to batch here"""

        list_prompts_tokens = []
        list_prompts_attn_masks = []

        print(f"IssueQuery started with {len(query_samples)} samples")
        while len(query_samples) > 0:
            self.query_queue.put(query_samples[:self.batch_size])
            query_samples = query_samples[self.batch_size:]
        print(f"IssueQuery done")


    def flush_queries(self):
        pass

    def __del__(self):
        pass


class SUTServer(SUT):
    def __init__(self, model_path=None, api_server=None, additional_servers=[], api_model_name=None, vllm=False, dtype="bfloat16", device="cpu", total_sample_count=24576, dataset_path=None, workers=1):

        super().__init__(model_path=model_path, api_server=api_server, additional_servers=additional_servers, api_model_name=api_model_name, grpc=grpc, vllm=vllm, dtype=dtype, device=device, total_sample_count=total_sample_count, dataset_path=dataset_path, workers=workers)

        with open(f"{self.model_path}/tokenizer.json", 'r') as token_file:
            llama_tokenizer = json.load(token_file)
        self.llama_vocab = llama_tokenizer["model"]["vocab"]

        self.first_token_queue = queue.Queue()
        

    def start(self):

        # Create worker threads
        for j in range(self.num_workers):
            worker = threading.Thread(target=self.process_queries)
            worker.start()
            self.worker_threads[j] = worker

        # Create first token response thread
        self.ft_response_thread = threading.Thread(target=self.process_first_tokens)
        self.ft_response_thread.start()


    def process_first_tokens(self):

        while True:
            first_token_item = self.first_token_queue.get()

            if first_token_item is None:
                log.info("Exiting First token response thread")
                break

            first_tokens, response_id = first_token_item

            response_data = array.array("B", np.array(first_tokens, np.int32).tobytes())
            bi = response_data.buffer_info()
            response = [lg.QuerySampleResponse(response_id, bi[0], bi[1])]
            lg.FirstTokenComplete(response)

    def stream_api(self, input, response_ids, idx):
        headers = {
            'Content-Type': 'application/json',
        }

        json_data = {
            'model_id': 'Llama-2-70b-chat-hf-caikit',
            'inputs': input,
            'parameters': {
                'max_new_tokens': 1024,
                'min_new_tokens': 1,
                'decoding_method': "GREEDY"
            },
        }

        token_cache = []
        s = requests.Session()
        first = True
        with s.post(
            self.api_servers[idx],
            headers=headers,
            json=json_data,
            verify=False,
            stream=True
        ) as resp:
            for line in resp.iter_lines():
                if line:
                    decoded = line.decode()
                    if decoded.startswith("data"):
                        token_l = json.loads(decoded[6:])["tokens"]
                        if token_l:
                            token = self.llama_vocab[token_l[0]["text"]]
                            if first:
                                self.first_token_queue.put((token, response_ids[0]))
                                first = False
                            token_cache.append(token)
        return token_cache

    def stream_api_grpc(self, input, response_ids, idx):
        token_cache = []
        first = True
        resps = self.grpc_clients[idx].make_request_stream(input, model_id=self.api_model_name)
        for resp in resps:
            if resp.tokens:
                token = self.llama_vocab[resp.tokens[0].text]
                if first:
                    self.first_token_queue.put((token, response_ids[0]))
                    first = False
                token_cache.append(token)
        return token_cache
    
    def stream_api_vllm(self, input, response_ids, idx):
        headers = {
            'Content-Type': 'application/json',
        }

        json_data = {
            'model': '/opt/app-root/share/models',
            'prompt': input,
            'max_tokens': 1024,
            'temperature': 0,
            'stream': True,
            'logprobs': 1
        }

        while True:
            try:
                token_cache = []
                s = requests.Session()
                first = True
                with s.post(
                    f'{self.api_servers[idx]}/v1/completions',
                    headers=headers,
                    json=json_data,
                    verify=False,
                    stream=True
                ) as resp:
                    for line in resp.iter_lines():
                        if line:
                            decoded = line.decode()
                            if decoded.startswith("data") and "[DONE]" not in decoded:
                                inter = json.loads(decoded[6:])["choices"][0]["logprobs"]
                                if "top_logprobs" in inter:
                                    token_s = list(inter["top_logprobs"][0].keys())[0]
                                    token = self.llama_vocab[token_s]
                                    if first:
                                        self.first_token_queue.put((token, response_ids[0]))
                                        first = False
                                    token_cache.append(token)
                s.close()
                if token_cache:
                    return token_cache
            except Exception as e:
                s.close()
                print("Connection failure")
                print(f"An exception occurred: {type(e).__name__}")
                print(f"Exception details: {e}")

    def async_process_query(self, input_ids_tensor, qitem_id, idx):
        decoded = self.tokenizer.decode(input_ids_tensor[0])
        response_ids = [qitem_id]
        if self.grpc:
            output_tokens = self.stream_api_grpc(decoded, response_ids, idx)
        elif self.vllm:
            output_tokens = self.stream_api_vllm(decoded, response_ids, idx)
        else:
            output_tokens = self.stream_api(decoded, response_ids, idx)

        n_tokens = len(output_tokens)
        if n_tokens <= 1:
            print("WARNING: caught low token count")
            print(input_ids_tensor)
            print(output_tokens)
        response_array = array.array("B", np.array(output_tokens, np.int32).tobytes())
        bi = response_array.buffer_info()
        response = [lg.QuerySampleResponse(
            qitem_id, bi[0], bi[1], n_tokens)]
        lg.QuerySamplesComplete(response)
        sys.exit()

    def process_queries(self):
        """Processor of the queued queries. User may choose to add batching logic """
        server_idx = 0
        while True:

            qitem = self.query_queue.get()
            if qitem is None:
                break

            input_ids_tensor = self.data_object.input_ids[qitem.index]
            input_masks_tensor = self.data_object.attention_masks[qitem.index]

            if self.api_servers:
                threading.Thread(target=self.async_process_query, args=(input_ids_tensor, qitem.id, server_idx)).start()
                server_idx = (server_idx + 1) % len(self.api_servers)
            else:
                #TODO: This PoC is super slow with significant overhead. Best to create a patch to `generate`
                tokens_cache = []
                tokens_streamer = FirstTokenStreamer(self.first_token_queue, tokens_cache=tokens_cache, is_first_token=True, response_ids=[qitem.id])

                _ = self.model.generate(    input_ids=input_ids_tensor,
                                            attention_mask=input_masks_tensor,
                                            pad_token_id=self.tokenizer.pad_token_id,
                                            streamer = tokens_streamer,
                                            **gen_kwargs
                                            )

                output_tokens = tokens_streamer.get_out_tokens()

                n_tokens = len(output_tokens)
                response_array = array.array("B", np.array(output_tokens, np.int32).tobytes())
                bi = response_array.buffer_info()
                response = [lg.QuerySampleResponse(
                    qitem.id, bi[0], bi[1], n_tokens)]
                lg.QuerySamplesComplete(response)


    def issue_queries(self, query_samples):

        self.query_queue.put(query_samples[0])


    def stop(self):
        for _ in range(self.num_workers):
            self.query_queue.put(None)

        for worker in self.worker_threads:
            worker.join()

        self.first_token_queue.put(None)
        self.ft_response_thread.join()
