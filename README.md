# DelayCheck: Delaying Checkpointing for Efficient Modern Distributed Training

**Delaying** is a new checkpointing mechanism for efficient modern distributed training,  which delays and partitions the checkpointing operation until the forward pass ends and the backward pass begins, so as to reduce the training stalls for efficient modern distributed training. Further, we also propose an optimized on-disk checkpointing scheme and a fast failure recovery scheme to enhance the performance of DelayCheck.

# Implementation

## The system architecture of DelayCheck
**DelayCheck** employs a decoupled and hierarchical storage design for checkpointing and consists of three modules:

1. an in-memory checkpoint creation module
2. an on-disk checkpoint creation module 
3. a failure recovery module

The system architecture of **DelayCheck** is as follows: 

<center class ='img'>
<img src="checkpoint_workflow_.jpg" width="600px" />
</center>





# Installation

## **Prerequisites**
- CUDA-12.6
- DeepSpeed-0.14.5 
- NCCL-2.20.5 
- Hadoop-3.3.6
- Huggingface-0.24.6


## **Get the code**
```shell
git clone https://github.com/FAST26-DelayCheck/DelayCheck
cd DelayCheck
pip install -r requirements.txt
python setup.py
```

## **Quick start**

Codes of seven types of checkpointing methods are provided, as well as no-checkpoint Baseline. They are DataStates-LLM, FastPersist, Gemini, DeepFreeze, CheckFreq, TorchSnapshot and DelayCheck. For each methods, there are codes for four models, which are GPT2, BERT, RoBERT, BLOOM, ResNet and VIT.

For example, to run gpt2-10B with DelayCheck:


```shell
cd DelayCheck/delaycheck/example/llm/gpt2
bash run_clm_no_trainer_deepspeed_layer_10B_delay.sh
```


## **Referred Datasets**


- Wikitex-103: [https://huggingface.co/datasets/wikitext](https://huggingface.co/datasets/wikitext)
- SQuAD: [https://rajpurkar.github.io/SQuAD-explorer/](https://rajpurkar.github.io/SQuAD-explorer/)
- ImageNet: [https://www.image-net.org/](https://www.image-net.org/)



