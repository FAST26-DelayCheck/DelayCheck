import os
import sys
import logging
import argparse
from tensorboardX import SummaryWriter

SUMMARY_WRITER_DIR_NAME = 'runs'


def get_argument_parser():
    parser = argparse.ArgumentParser()

    # Required parameters
    parser.add_argument(
        "--bert_model",
        default=None,
        type=str,
        # required=True,
        help="Bert pre-trained model selected in the list: bert-base-uncased, "
        "bert-large-uncased, bert-base-cased, bert-large-cased, bert-base-multilingual-uncased, "
        "bert-base-multilingual-cased, bert-base-chinese.")
    # parser.add_argument(
    #     "--output_dir",
    #     default=None,
    #     type=str,
    #     required=True,
    #     help=
    #     "The output directory where the model checkpoints and predictions will be written."
    # )

    # Other parameters
    # parser.add_argument("--train_file",
    #                     default=None,
    #                     type=str,
    #                     help="SQuAD json for training. E.g., train-v1.1.json")
    
    parser.add_argument(
        "--predict_file",
        default=None,
        type=str,
        help="SQuAD json for predictions. E.g., dev-v1.1.json or test-v1.1.json"
    )
    parser.add_argument(
        "--max_seq_length",
        default=384,
        type=int,
        help=
        "The maximum total input sequence length after WordPiece tokenization. Sequences "
        "longer than this will be truncated, and sequences shorter than this will be padded."
    )
    parser.add_argument(
        "--doc_stride",
        default=128,
        type=int,
        help=
        "When splitting up a long document into chunks, how much stride to take between chunks."
    )
    parser.add_argument(
        "--max_query_length",
        default=64,
        type=int,
        help=
        "The maximum number of tokens for the question. Questions longer than this will "
        "be truncated to this length.")
    parser.add_argument("--do_train",
                        action='store_true',
                        help="Whether to run training.")
    parser.add_argument("--do_predict",
                        action='store_true',
                        help="Whether to run eval on the dev set.")
    parser.add_argument("--train_batch_size",
                        default=32,
                        type=int,
                        help="Total batch size for training.")
    parser.add_argument("--predict_batch_size",
                        default=8,
                        type=int,
                        help="Total batch size for predictions.")
    
    # parser.add_argument("--learning_rate",
    #                     default=5e-5,
    #                     type=float,
    #                     help="The initial learning rate for Adam.")
    
    # parser.add_argument("--num_train_epochs",
    #                     default=3.0,
    #                     type=float,
    #                     help="Total number of training epochs to perform.")
    
    parser.add_argument(
        "--warmup_proportion",
        default=0.1,
        type=float,
        help=
        "Proportion of training to perform linear learning rate warmup for. E.g., 0.1 = 10% "
        "of training.")
    parser.add_argument(
        "--n_best_size",
        default=20,
        type=int,
        help=
        "The total number of n-best predictions to generate in the nbest_predictions.json "
        "output file.")
    parser.add_argument(
        "--max_answer_length",
        default=30,
        type=int,
        help=
        "The maximum length of an answer that can be generated. This is needed because the start "
        "and end predictions are not conditioned on one another.")
    parser.add_argument(
        "--verbose_logging",
        action='store_true',
        help=
        "If true, all of the warnings related to data processing will be printed. "
        "A number of warnings are expected for a normal SQuAD evaluation.")
    parser.add_argument("--no_cuda",
                        action='store_true',
                        help="Whether not to use CUDA when available")
    
    # parser.add_argument('--seed',
    #                     type=int,
    #                     default=42,
    #                     help="random seed for initialization")
    # parser.add_argument(
    #     '--gradient_accumulation_steps',
    #     type=int,
    #     default=1,
    #     help=
    #     "Number of updates steps to accumulate before performing a backward/update pass."
    # )
    parser.add_argument(
        "--do_lower_case",
        action='store_true',
        help=
        "Whether to lower case the input text. True for uncased models, False for cased models."
    )
    parser.add_argument("--local_rank",
                        type=int,
                        default=-1,
                        help="local_rank for distributed training on gpus")
    # parser.add_argument(
    #     '--fp16',
    #     action='store_true',
    #     help="Whether to use 16-bit float precision instead of 32-bit")
    
    parser.add_argument(
        '--wall_clock_breakdown',
        action='store_true',
        default=False,
        help=
        "Whether to display the breakdown of the wall-clock time for foraward, backward and step"
    )
    parser.add_argument(
        '--loss_scale',
        type=float,
        default=0,
        help=
        "Loss scaling to improve fp16 numeric stability. Only used when fp16 set to True.\n"
        "0 (default value): dynamic loss scaling.\n"
        "Positive power of 2: static loss scaling value.\n")
    parser.add_argument("--model_file",
                        type=str,
                        default="0",
                        help="Path to the Pretrained BERT Encoder File.")

    parser.add_argument("--max_grad_norm",
                        default=1.,
                        type=float,
                        help="Gradient clipping for FusedAdam.")
    parser.add_argument('--job_name',
                        type=str,
                        default=None,
                        help='Output path for Tensorboard event files.')

    parser.add_argument(
        '--preln',
        action='store_true',
        default=False,
        help=
        "Whether to display the breakdown of the wall-clock time for foraward, backward and step"
    )

    parser.add_argument(
        '--loss_plot_alpha',
        type=float,
        default=0.2,
        help='Alpha factor for plotting moving average of loss.')

    parser.add_argument(
        '--max_steps',
        type=int,
        default=sys.maxsize,
        help=
        'Maximum number of training steps of effective batch size to complete.'
    )

    parser.add_argument(
        '--max_steps_per_epoch',
        type=int,
        default=sys.maxsize,
        help=
        'Maximum number of training steps of effective batch size within an epoch to complete.'
    )

    parser.add_argument('--print_steps',
                        type=int,
                        default=100,
                        help='Interval to print training details.')

    parser.add_argument('--deepspeed_transformer_kernel',
                        default=False,
                        action='store_true',
                        help='Use DeepSpeed transformer kernel to accelerate.')

    parser.add_argument('--dropout', type=float, default=0.1, help='dropout')

    parser.add_argument(
        '--ckpt_type',
        type=str,
        default="DS",
        help="Checkpoint's type, DS - DeepSpeed, TF - Tensorflow, HF - Huggingface.")

    parser.add_argument(
        "--origin_bert_config_file",
        type=str,
        default=None,
        help="The config json file corresponding to the non-DeepSpeed pre-trained BERT model."
    )
    
    
    
    
    from transformers import (
    CONFIG_MAPPING,
    MODEL_MAPPING,
    AutoConfig,
    AutoModelForCausalLM,
    AutoTokenizer,
    SchedulerType,
    default_data_collator,
    get_scheduler,
    
    )
    MODEL_CONFIG_CLASSES = list(MODEL_MAPPING.keys())
    MODEL_TYPES = tuple(conf.model_type for conf in MODEL_CONFIG_CLASSES)


    
    parser.add_argument(
        "--dataset_name",
        type=str,
        default=None,
        help="The name of the dataset to use (via the datasets library).",
    )
    parser.add_argument(
        "--dataset_config_name",
        type=str,
        default=None,
        help="The configuration name of the dataset to use (via the datasets library).",
    )
    parser.add_argument(
        "--train_file", type=str, default=None, help="A csv, txt or a json file containing the training data."
    )
    
    parser.add_argument(
        "--validation_file", type=str, default=None, help="A csv, txt or a json file containing the validation data."
    )
    parser.add_argument(
        "--validation_split_percentage",
        default=5,
        help="The percentage of the train set used as validation set in case there's no validation split",
    )
    parser.add_argument(
        "--model_name_or_path",
        type=str,
        help="Path to pretrained model or model identifier from huggingface.co/models.",
        required=False,
    )
    parser.add_argument(
        "--config_name",
        type=str,
        default=None,
        help="Pretrained config name or path if not the same as model_name",
    )
    parser.add_argument(
        "--tokenizer_name",
        type=str,
        default=None,
        help="Pretrained tokenizer name or path if not the same as model_name",
    )
    parser.add_argument(
        "--use_slow_tokenizer",
        action="store_true",
        help="If passed, will use a slow tokenizer (not backed by the 🤗 Tokenizers library).",
    )
    parser.add_argument(
        "--per_device_train_batch_size",
        type=int,
        default=8,
        help="Batch size (per device) for the training dataloader.",
    )
    parser.add_argument(
        "--per_device_eval_batch_size",
        type=int,
        default=8,
        help="Batch size (per device) for the evaluation dataloader.",
    )
    parser.add_argument(
        "--learning_rate",
        type=float,
        default=5e-5,
        help="Initial learning rate (after the potential warmup period) to use.",
    )
    parser.add_argument("--weight_decay", type=float, default=0.0, help="Weight decay to use.")
    
    # Max Epochs
    parser.add_argument("--num_train_epochs", type=int, default=3, help="Total number of training epochs to perform.")
    parser.add_argument(
        "--max_train_steps",
        type=int,
        default=None,
        help="Total number of training steps to perform. If provided, overrides num_train_epochs.",
    )
    parser.add_argument(
        "--gradient_accumulation_steps",
        type=int,
        default=1,
        help="Number of updates steps to accumulate before performing a backward/update pass.",
    )
    parser.add_argument(
        "--lr_scheduler_type",
        type=SchedulerType,
        default="linear",
        help="The scheduler type to use.",
        choices=["linear", "cosine", "cosine_with_restarts", "polynomial", "constant", "constant_with_warmup"],
    )
    parser.add_argument(
        "--num_warmup_steps", type=int, default=0, help="Number of steps for the warmup in the lr scheduler."
    )
    parser.add_argument("--output_dir", type=str, default=None, help="Where to store the final model.")
    parser.add_argument("--seed", type=int, default=42, help="A seed for reproducible training.")
    parser.add_argument(
        "--model_type",
        type=str,
        default=None,
        help="Model type to use if training from scratch.",
        choices=MODEL_TYPES,
    )
    parser.add_argument(
        "--block_size",
        type=int,
        default=None,
        help=(
            "Optional input sequence length after tokenization. The training dataset will be truncated in block of"
            " this size for training. Default to the model max input length for single sentence inputs (take into"
            " account special tokens)."
        ),
    )
    parser.add_argument(
        "--preprocessing_num_workers",
        type=int,
        default=None,
        help="The number of processes to use for the preprocessing.",
    )
    parser.add_argument(
        "--overwrite_cache", action="store_true", help="Overwrite the cached training and evaluation sets"
    )
    parser.add_argument(
        "--no_keep_linebreaks", action="store_true", help="Do not keep line breaks when using TXT files."
    )
    parser.add_argument("--push_to_hub", action="store_true", help="Whether or not to push the model to the Hub.")
    parser.add_argument(
        "--hub_model_id", type=str, help="The name of the repository to keep in sync with the local `output_dir`."
    )
    parser.add_argument("--hub_token", type=str, help="The token to use to push to the Model Hub.")
    parser.add_argument(
        "--trust_remote_code",
        type=bool,
        default=False,
        help=(
            "Whether or not to allow for custom models defined on the Hub in their own modeling files. This option "
            "should only be set to `True` for repositories you trust and in which you have read the code, as it will "
            "execute code present on the Hub on your local machine."
        ),
    )
    parser.add_argument(
        "--checkpointing_steps",
        type=str,
        default=None,
        help="Whether the various states should be saved at the end of every n steps, or 'epoch' for each epoch.",
    )
    parser.add_argument(
        "--resume_from_checkpoint",
        type=str,
        default=None,
        help="If the training should continue from a checkpoint folder.",
    )
    parser.add_argument(
        "--with_tracking",
        action="store_true",
        help="Whether to enable experiment trackers for logging.",
    )
    parser.add_argument(
        "--report_to",
        type=str,
        default="all",
        help=(
            'The integration to report the results and logs to. Supported platforms are `"tensorboard"`,'
            ' `"wandb"`, `"comet_ml"` and `"clearml"`. Use `"all"` (default) to report to all integrations. '
            "Only applicable when `--with_tracking` is passed."
        ),
    )
    parser.add_argument(
        "--low_cpu_mem_usage",
        action="store_true",
        help=(
            "It is an option to create the model as an empty shell, then only materialize its parameters when the pretrained weights are loaded. "
            "If passed, LLM loading time and RAM consumption will be benefited."
        ),
    )
    
    parser.add_argument('--gpu', action='store_true', default=True, help='use gpu or not')
    parser.add_argument('--no-cuda', action='store_true', default=False, help='disables CUDA training')
    # parser.add_argument('--seed', type=int, default=42, help='random seed')
    parser.add_argument('--fp16',
                        default=False,
                        action='store_true',
                        help="Mixed precision training")
    # 
    parser.add_argument('--use-adasum', action='store_true', default=False,
                    help='use adasum algorithm to do reduction')
    
    # Elastic Horovod settings
    parser.add_argument('--batches-per-commit', type=int, default=50,
                    help='number of batches processed before calling `state.commit()`; '
                         'commits prevent losing progress if an error occurs, but slow '
                         'down training.')
    parser.add_argument('--batches-per-host-check', type=int, default=5,
                    help='number of batches processed before calling `state.check_host_updates()`; '
                         'this check is very fast compared to state.commit() (which calls this '
                         'as part of the commit process), but because still incurs some cost due '
                         'to broadcast, so we may not want to perform it every batch.')

    
    # Gradient Merging    
    parser.add_argument('--model-net', default='gpt_2', type=str, help='net type')
    
    parser.add_argument('--model', type=str, default='gpt_2',
                    help='model to benchmark')
    
    parser.add_argument('--num-warmup-batches', type=int, default=20,
                        help='number of warm-up batches that don\'t count towards benchmark')
    parser.add_argument('--num-batches-per-iter', type=int, default=10,
                        help='number of batches per benchmark iteration')
    parser.add_argument('--num-iters', type=int, default=50,
                        help='number of benchmark iterations')
    
    
    parser.add_argument('--mgwfbp', action='store_true', default=False, help='Use MG-WFBP')
    parser.add_argument('--asc', action='store_true', default=False, help='Use MG-WFBP')
    parser.add_argument('--nstreams', type=int, default=1, help='Number of communication streams')

    # 设置合并的阈值大小, default=23705252为ResNet-50所有层梯度元素数量的总和
    parser.add_argument('--threshold', type=int, default=34015396, help='Set threshold if mgwfbp is False')
    parser.add_argument('--rdma', action='store_true', default=False, help='Use RDMA')

    # Top-k + EF
    # parser.add_argument('--compressor', type=str, default='topkef', choices=compressors.keys(), help='Specify the compressors if density < 1.0')
    parser.add_argument('--compressor', type=str, default='topkef', help='Specify the compressors if density < 1.0')
    
    parser.add_argument('--memory', type=str, default = 'residual', help='Error-feedback')
    parser.add_argument('--density', type=float, default=0.01, help='Density for sparsification')
    
    parser.add_argument('--percent', type=float, default=0, help='percent of residual 0')
    
    
    

    return parser


def get_summary_writer(name, base=".."):
    """Returns a tensorboard summary writer
    """
    return SummaryWriter(
        log_dir=os.path.join(base, SUMMARY_WRITER_DIR_NAME, name))


def write_summary_events(summary_writer, summary_events):
    for event in summary_events:
        summary_writer.add_scalar(event[0], event[1], event[2])


def is_time_to_exit(args, epoch_steps=0, global_steps=0):
    return (epoch_steps >= args.max_steps_per_epoch) or \
            (global_steps >= args.max_steps)


def check_early_exit_warning(args):
    # Issue warning if early exit from epoch is configured
    if args.max_steps < sys.maxsize:
        logging.warning(
            'Early training exit is set after {} global steps'.format(
                args.max_steps))

    if args.max_steps_per_epoch < sys.maxsize:
        logging.warning('Early epoch exit is set after {} global steps'.format(
            args.max_steps_per_epoch))
