import torch
from concurrent.futures import ThreadPoolExecutor
import time
from collections import OrderedDict, deque
import sys
import os
from typing import Union
import pickle
import json
import ctypes
import numpy as np
from datastates.ckpt import CkptEngine
from .helper import parse_config, get_checkpoint_version, HOST_CACHE_SIZE, CKPT_PARSER_THREADS
from datastates.utils import get_logger


SIZE_UINT64 = ctypes.sizeof(ctypes.c_uint64)
KEY_SEPARATOR = "|"


class Checkpointing:
    def __init__(self, runtime_config={}, rank=0) -> None:
        try:
            if not torch.cuda.is_available():
                raise RuntimeError("[DataStates.llm] CUDA is not available. Make sure CUDA drivers are installed and GPU is accessible.")
            
            self.rank           = int(rank)
            datastates_config   = parse_config(runtime_config)
            host_cache_size     = int(datastates_config[HOST_CACHE_SIZE]*(1<<30))       # From GB to Bytes
            cuda_device         = int(torch.cuda.current_device())
            concurrent_parser_threads = int(datastates_config[CKPT_PARSER_THREADS])
            self.ckpt_engine = CkptEngine(host_cache_size, cuda_device, self.rank)   
            self.executor = ThreadPoolExecutor(max_workers=concurrent_parser_threads)
            self.logger = get_logger(__name__)
            self.last_ckpt_version = -1

        except Exception as exc:
            print(f"[DataStates.llm][ERROR] Got exception during DataStates init {exc}")
            sys.exit(-1)

    def save_background(self, state_dict: Union[dict, OrderedDict], path: str):
        
        # print('save_background',)
        
        
        try:
            version = get_checkpoint_version(path, self.last_ckpt_version)
            header = {}
            async_copies = {}
            _start_tensor_offset = 0
            _end_tensor_offset = 0
            def _parse_state(key, data):
                nonlocal _start_tensor_offset, _end_tensor_offset
                try:
                    if torch.is_tensor(data): # and data.device.type == 'cuda':
                        tensor_size = data.numel()*data.element_size()
                        _end_tensor_offset += tensor_size
                        header[key] = {
                            "dtype": str(data.dtype),                       # JSON cannot stringify torch.Size() type
                            "shape": tuple(data.shape),
                            "data_offsets": [_start_tensor_offset, _end_tensor_offset],
                        }
                        data = data.contiguous()
                        async_copies[key] = {
                            "tensor": data,
                            "file_offset": _start_tensor_offset
                        }
                        _start_tensor_offset = _end_tensor_offset
                        snapshot = f"TENSOR{KEY_SEPARATOR}{key}"
                    elif isinstance(data, list):
                        snapshot = [None]*len(data)
                        for (idx, ele) in enumerate(data):
                            new_key = f"{key}{KEY_SEPARATOR}{idx}" if len(key) else f"{idx}"
                            snapshot[idx] = _parse_state(new_key, ele)
                    elif isinstance(data, (dict, OrderedDict)):
                        snapshot = {}
                        for (k, v) in data.items():
                            new_key = f"{key}{KEY_SEPARATOR}{k}" if len(key) else f"{k}"
                            snapshot[k] = _parse_state(new_key, v)
                    else:
                        snapshot = data
                    return snapshot
                except Exception as exc:
                    raise Exception(f"[DataStates.llm][ERROR] Cannot parse {key}, exception: {exc}, data is {data}")

            lean_state_dict = _parse_state("", state_dict)
            lean_state_dict = pickle.dumps(lean_state_dict, protocol=pickle.HIGHEST_PROTOCOL)
            _end_tensor_offset += len(lean_state_dict)
            header.update({"datastates_metadata": {"data_offsets": [_start_tensor_offset, _end_tensor_offset]}})
            header = json.dumps(header).encode("utf-8")
            header_size = len(header).to_bytes(SIZE_UINT64, 'little')   # Force the header size to take 8 bytes
            metadata_size = len(header_size) + len(header)

            # 
            # Launch Async copies
            # print('async_copies.items() = ', async_copies.items())

            async_ckpt_list = []
            for _, v in async_copies.items():
                v["file_offset"] += metadata_size
                async_ckpt_list.append((version, v["tensor"], v["file_offset"], path))
            
            self.ckpt_engine.async_save(async_ckpt_list)
            
            # print('self.ckpt_engine.async_save')
            

            # 
            
            # 

            return None
        except Exception as exc:
            self.logger.error(f"[DataStates.llm][ERROR] From DataStates save_background, generated exception: {exc}")
            sys.exit(-1)

    def save(self, state_dict, path: str):
        try:
            # print('save, ')

            if not isinstance(state_dict, (dict, OrderedDict)):
                raise Exception(f"[DataStates.llm] state_dict given to checkpoint must be dictionary. Passed {type(state_dict)} instead for {path}.")

            # print('save, ')
            self.executor.submit(self.save_background, state_dict, path)
            # 
            # self.save_background(state_dict, path)
            # 
            return True
        except Exception as exc:
            self.logger.error(f"[DataStates.llm][ERROR] Could not save {path}, exception: {exc}, data: {state_dict}")
            sys.exit(-1)
            
    def load(self, path: str, map_location=None):
        try:
            version = get_checkpoint_version(path, self.last_ckpt_version)
            f = open(path, 'rb')
            f.seek(0)
            header_size_bytes = f.read(SIZE_UINT64)
            header_size = int.from_bytes(header_size_bytes, 'little')
            metadata_size = header_size + SIZE_UINT64
            header = json.loads(f.read(header_size))
            [start_offset, end_offset] = np.add(header["datastates_metadata"]["data_offsets"], metadata_size)
            del(header["datastates_metadata"])
            f.seek(start_offset)
            data = pickle.loads(f.read(end_offset-start_offset))

            try:
                restore_list = []
                for k, v in header.items():
                    split_k = deque(k.split(KEY_SEPARATOR))
                    dtype = v["dtype"]
                    if dtype.startswith("torch"):
                        dtype = dtype.replace('torch.', '')
                    shape = v["shape"]
                    [start_offset, end_offset] = np.add(v["data_offsets"], metadata_size)

                    pre_dest = data
                    dest = data
                    while len(split_k):
                        sub_k = split_k.popleft()
                        if sub_k.isdigit():
                            sub_k = int(sub_k) 
                        pre_dest = dest
                        dest = dest[sub_k]
                    if dest != f"TENSOR{KEY_SEPARATOR}{k}":
                        raise Exception(f"[DataStates.llm] The key in header {k} does not match key at location {dest}")

                    # tensor_restored = torch.zeros(size=tuple(shape), dtype=getattr(torch, dtype))
                    # restore_list.append((version, tensor_restored, start_offset, path))
                    f.seek(start_offset)
                    buffer = f.read(end_offset-start_offset)
                    tensor_restored = torch.frombuffer(buffer, dtype=getattr(torch, dtype)).reshape(tuple(shape))
                    pre_dest[sub_k] = tensor_restored
                # self.ckpt_engine.load(restore_list)
            except Exception as exc:
                raise Exception(f"[DataStates.llm] Got error with tensor loading {dtype}, {shape}, {exc}")
            self.logger.info(f"[DataStates.llm] Loaded checkpoint from {path}.")
            return data
        except Exception as exc:
            self.logger.error(f"[DataStates.llm][ERROR] Could not load {path}, exception: {exc}")
            sys.exit(-1)

    def commit(self, tag):
        self.wait()
        self.logger.info(f"[DataStates.llm] Checkpoint {tag} is ready now!")
        self.last_ckpt_version += 1
        return True

    def wait(self):
        try:
            t = time.time()
            self.ckpt_engine.wait()
            # self.logger.info(f"[DataStates.llm] Wait time in checkpointing engine {time.time()-t}")
        except Exception as exc:
            self.logger.error(f"[DataStates.llm][ERROR] From wait, generated exception: {exc}")
            sys.exit(-1)
        return 
    
    def __del__(self):
        self.executor.shutdown(True)













