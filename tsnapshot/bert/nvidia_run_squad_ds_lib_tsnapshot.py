# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HugginFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
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
"""Run BERT on SQuAD."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import collections
import logging
import json
import math
import os
import random
import pickle
from tqdm import tqdm, trange
from utils_bert import get_argument_parser, \
    get_summary_writer, write_summary_events, \
    is_time_to_exit, check_early_exit_warning
import deepspeed

import torch.distributed as dist
from tqdm import tqdm
from multiprocessing import shared_memory

import time
import numpy as np
import torch
from torch.utils.data import TensorDataset, DataLoader, RandomSampler, SequentialSampler
from torch.utils.data.distributed import DistributedSampler

from pytorch_pretrained_bert.tokenization import whitespace_tokenize, BasicTokenizer, BertTokenizer
from pytorch_pretrained_bert.optimization import BertAdam
from pytorch_pretrained_bert.file_utils import PYTORCH_PRETRAINED_BERT_CACHE

from turing.nvidia_modeling import BertForQuestionAnswering, BertConfig
from turing.nvidia_modelingpreln import BertConfig as BertConfigPreLN
from turing.nvidia_modelingpreln import BertForQuestionAnswering as BertForQuestionAnsweringPreLN

import torchsnapshot
from torchsnapshot import Snapshot, Stateful
import uuid
from typing import Dict, Optional

import sys
sys.path.append("../../") 
import oteacheck_lib as oteacheck_lib
import oteacheck_lib.utils


# logging.basicConfig(
#     format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
#     datefmt='%m/%d/%Y %H:%M:%S',
#     level=logging.INFO)


logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
    datefmt='%m/%d/%Y %H:%M:%S',
    level=logging.WARNING)

logger = logging.getLogger(__name__)

all_step_time = 0.0

local_env = os.environ.copy() 
local_env['PATH']= 'bin:' + local_env['PATH']
os.environ.update(local_env)

class SquadExample(object):
    """A single training/test example for the Squad dataset."""
    def __init__(self,
                 qas_id,
                 question_text,
                 doc_tokens,
                 orig_answer_text=None,
                 start_position=None,
                 end_position=None):
        self.qas_id = qas_id
        self.question_text = question_text
        self.doc_tokens = doc_tokens
        self.orig_answer_text = orig_answer_text
        self.start_position = start_position
        self.end_position = end_position

    def __str__(self):
        return self.__repr__()

    def __repr__(self):
        s = ""
        s += "qas_id: %s" % (self.qas_id)
        s += ", question_text: %s" % (self.question_text)
        s += ", doc_tokens: [%s]" % (" ".join(self.doc_tokens))
        if self.start_position:
            s += ", start_position: %d" % (self.start_position)
        if self.start_position:
            s += ", end_position: %d" % (self.end_position)
        return s


class InputFeatures(object):
    """A single set of features of data."""
    def __init__(self,
                 unique_id,
                 example_index,
                 doc_span_index,
                 tokens,
                 token_to_orig_map,
                 token_is_max_context,
                 input_ids,
                 input_mask,
                 segment_ids,
                 start_position=None,
                 end_position=None):
        self.unique_id = unique_id
        self.example_index = example_index
        self.doc_span_index = doc_span_index
        self.tokens = tokens
        self.token_to_orig_map = token_to_orig_map
        self.token_is_max_context = token_is_max_context
        self.input_ids = input_ids
        self.input_mask = input_mask
        self.segment_ids = segment_ids
        self.start_position = start_position
        self.end_position = end_position


def read_squad_examples(input_file, is_training):
    """Read a SQuAD json file into a list of SquadExample."""
    with open(input_file, "r", encoding='utf-8') as reader:
        input_data = json.load(reader)["data"]

    def is_whitespace(c):
        if c == " " or c == "\t" or c == "\r" or c == "\n" or ord(c) == 0x202F:
            return True
        return False

    examples = []
    for entry in input_data:
        for paragraph in entry["paragraphs"]:
            paragraph_text = paragraph["context"]
            doc_tokens = []
            char_to_word_offset = []
            prev_is_whitespace = True
            for c in paragraph_text:
                if is_whitespace(c):
                    prev_is_whitespace = True
                else:
                    if prev_is_whitespace:
                        doc_tokens.append(c)
                    else:
                        doc_tokens[-1] += c
                    prev_is_whitespace = False
                char_to_word_offset.append(len(doc_tokens) - 1)

            for qa in paragraph["qas"]:
                qas_id = qa["id"]
                question_text = qa["question"]
                start_position = None
                end_position = None
                orig_answer_text = None
                if is_training:
                    if len(qa["answers"]) != 1:
                        raise ValueError(
                            "For training, each question should have exactly 1 answer."
                        )
                    answer = qa["answers"][0]
                    orig_answer_text = answer["text"]
                    answer_offset = answer["answer_start"]
                    answer_length = len(orig_answer_text)
                    start_position = char_to_word_offset[answer_offset]
                    end_position = char_to_word_offset[answer_offset +
                                                       answer_length - 1]
                    # Only add answers where the text can be exactly recovered from the
                    # document. If this CAN'T happen it's likely due to weird Unicode
                    # stuff so we will just skip the example.
                    #
                    # Note that this means for training mode, every example is NOT
                    # guaranteed to be preserved.
                    actual_text = " ".join(
                        doc_tokens[start_position:(end_position + 1)])
                    cleaned_answer_text = " ".join(
                        whitespace_tokenize(orig_answer_text))
                    if actual_text.find(cleaned_answer_text) == -1 and  torch.distributed.get_rank() == 0:
                        logger.warning("Could not find answer: '%s' vs. '%s'",
                                       actual_text, cleaned_answer_text)
                        continue

                example = SquadExample(qas_id=qas_id,
                                       question_text=question_text,
                                       doc_tokens=doc_tokens,
                                       orig_answer_text=orig_answer_text,
                                       start_position=start_position,
                                       end_position=end_position)
                examples.append(example)
    return examples


def convert_examples_to_features(examples, tokenizer, max_seq_length,
                                 doc_stride, max_query_length, is_training):
    """Loads a data file into a list of `InputBatch`s."""

    unique_id = 1000000000

    features = []
    for (example_index, example) in enumerate(examples):
        query_tokens = tokenizer.tokenize(example.question_text)

        if len(query_tokens) > max_query_length:
            query_tokens = query_tokens[0:max_query_length]

        tok_to_orig_index = []
        orig_to_tok_index = []
        all_doc_tokens = []
        for (i, token) in enumerate(example.doc_tokens):
            orig_to_tok_index.append(len(all_doc_tokens))
            sub_tokens = tokenizer.tokenize(token)
            for sub_token in sub_tokens:
                tok_to_orig_index.append(i)
                all_doc_tokens.append(sub_token)

        tok_start_position = None
        tok_end_position = None
        if is_training:
            tok_start_position = orig_to_tok_index[example.start_position]
            if example.end_position < len(example.doc_tokens) - 1:
                tok_end_position = orig_to_tok_index[example.end_position +
                                                     1] - 1
            else:
                tok_end_position = len(all_doc_tokens) - 1
            (tok_start_position, tok_end_position) = _improve_answer_span(
                all_doc_tokens, tok_start_position, tok_end_position,
                tokenizer, example.orig_answer_text)

        # The -3 accounts for [CLS], [SEP] and [SEP]
        max_tokens_for_doc = max_seq_length - len(query_tokens) - 3

        # We can have documents that are longer than the maximum sequence length.
        # To deal with this we do a sliding window approach, where we take chunks
        # of the up to our max length with a stride of `doc_stride`.
        _DocSpan = collections.namedtuple(  # pylint: disable=invalid-name
            "DocSpan", ["start", "length"])
        doc_spans = []
        start_offset = 0
        while start_offset < len(all_doc_tokens):
            length = len(all_doc_tokens) - start_offset
            if length > max_tokens_for_doc:
                length = max_tokens_for_doc
            doc_spans.append(_DocSpan(start=start_offset, length=length))
            if start_offset + length == len(all_doc_tokens):
                break
            start_offset += min(length, doc_stride)

        for (doc_span_index, doc_span) in enumerate(doc_spans):
            tokens = []
            token_to_orig_map = {}
            token_is_max_context = {}
            segment_ids = []
            tokens.append("[CLS]")
            segment_ids.append(0)
            for token in query_tokens:
                tokens.append(token)
                segment_ids.append(0)
            tokens.append("[SEP]")
            segment_ids.append(0)

            for i in range(doc_span.length):
                split_token_index = doc_span.start + i
                token_to_orig_map[len(
                    tokens)] = tok_to_orig_index[split_token_index]

                is_max_context = _check_is_max_context(doc_spans,
                                                       doc_span_index,
                                                       split_token_index)
                token_is_max_context[len(tokens)] = is_max_context
                tokens.append(all_doc_tokens[split_token_index])
                segment_ids.append(1)
            tokens.append("[SEP]")
            segment_ids.append(1)

            input_ids = tokenizer.convert_tokens_to_ids(tokens)

            # The mask has 1 for real tokens and 0 for padding tokens. Only real
            # tokens are attended to.
            input_mask = [1] * len(input_ids)

            # Zero-pad up to the sequence length.
            while len(input_ids) < max_seq_length:
                input_ids.append(0)
                input_mask.append(0)
                segment_ids.append(0)

            assert len(input_ids) == max_seq_length
            assert len(input_mask) == max_seq_length
            assert len(segment_ids) == max_seq_length

            start_position = None
            end_position = None
            if is_training:
                # For training, if our document chunk does not contain an annotation
                # we throw it out, since there is nothing to predict.
                doc_start = doc_span.start
                doc_end = doc_span.start + doc_span.length - 1
                if (example.start_position < doc_start
                        or example.end_position < doc_start
                        or example.start_position > doc_end
                        or example.end_position > doc_end):
                    continue

                doc_offset = len(query_tokens) + 2
                start_position = tok_start_position - doc_start + doc_offset
                end_position = tok_end_position - doc_start + doc_offset

            if example_index < 20 and torch.distributed.get_rank() == 0:
                logger.info("*** Example ***")
                logger.info("unique_id: %s" % (unique_id))
                logger.info("example_index: %s" % (example_index))
                logger.info("doc_span_index: %s" % (doc_span_index))
                logger.info("tokens: %s" % " ".join(tokens))
                logger.info("token_to_orig_map: %s" % " ".join(
                    ["%d:%d" % (x, y)
                     for (x, y) in token_to_orig_map.items()]))
                logger.info("token_is_max_context: %s" % " ".join([
                    "%d:%s" % (x, y)
                    for (x, y) in token_is_max_context.items()
                ]))
                logger.info("input_ids: %s" %
                            " ".join([str(x) for x in input_ids]))
                logger.info("input_mask: %s" %
                            " ".join([str(x) for x in input_mask]))
                logger.info("segment_ids: %s" %
                            " ".join([str(x) for x in segment_ids]))
                if is_training:
                    answer_text = " ".join(
                        tokens[start_position:(end_position + 1)])
                    logger.info("start_position: %d" % (start_position))
                    logger.info("end_position: %d" % (end_position))
                    logger.info("answer: %s" % (answer_text))


            features.append(
                InputFeatures(unique_id=unique_id,
                              example_index=example_index,
                              doc_span_index=doc_span_index,
                              tokens=tokens,
                              token_to_orig_map=token_to_orig_map,
                              token_is_max_context=token_is_max_context,
                              input_ids=input_ids,
                              input_mask=input_mask,
                              segment_ids=segment_ids,
                              start_position=start_position,
                              end_position=end_position))
            unique_id += 1

    return features


def _improve_answer_span(doc_tokens, input_start, input_end, tokenizer,
                         orig_answer_text):
    """Returns tokenized answer spans that better match the annotated answer."""

    # The SQuAD annotations are character based. We first project them to
    # whitespace-tokenized words. But then after WordPiece tokenization, we can
    # often find a "better match". For example:
    #
    #   Question: What year was John Smith born?
    #   Context: The leader was John Smith (1895-1943).
    #   Answer: 1895
    #
    # The original whitespace-tokenized answer will be "(1895-1943).". However
    # after tokenization, our tokens will be "( 1895 - 1943 ) .". So we can match
    # the exact answer, 1895.
    #
    # However, this is not always possible. Consider the following:
    #
    #   Question: What country is the top exporter of electornics?
    #   Context: The Japanese electronics industry is the lagest in the world.
    #   Answer: Japan
    #
    # In this case, the annotator chose "Japan" as a character sub-span of
    # the word "Japanese". Since our WordPiece tokenizer does not split
    # "Japanese", we just use "Japanese" as the annotation. This is fairly rare
    # in SQuAD, but does happen.
    tok_answer_text = " ".join(tokenizer.tokenize(orig_answer_text))

    for new_start in range(input_start, input_end + 1):
        for new_end in range(input_end, new_start - 1, -1):
            text_span = " ".join(doc_tokens[new_start:(new_end + 1)])
            if text_span == tok_answer_text:
                return (new_start, new_end)

    return (input_start, input_end)


def _check_is_max_context(doc_spans, cur_span_index, position):
    """Check if this is the 'max context' doc span for the token."""

    # Because of the sliding window approach taken to scoring documents, a single
    # token can appear in multiple documents. E.g.
    #  Doc: the man went to the store and bought a gallon of milk
    #  Span A: the man went to the
    #  Span B: to the store and bought
    #  Span C: and bought a gallon of
    #  ...
    #
    # Now the word 'bought' will have two scores from spans B and C. We only
    # want to consider the score with "maximum context", which we define as
    # the *minimum* of its left and right context (the *sum* of left and
    # right context will always be the same, of course).
    #
    # In the example the maximum context for 'bought' would be span C since
    # it has 1 left context and 3 right context, while span B has 4 left context
    # and 0 right context.
    best_score = None
    best_span_index = None
    for (span_index, doc_span) in enumerate(doc_spans):
        end = doc_span.start + doc_span.length - 1
        if position < doc_span.start:
            continue
        if position > end:
            continue
        num_left_context = position - doc_span.start
        num_right_context = end - position
        score = min(num_left_context,
                    num_right_context) + 0.01 * doc_span.length
        if best_score is None or score > best_score:
            best_score = score
            best_span_index = span_index

    return cur_span_index == best_span_index


RawResult = collections.namedtuple("RawResult",
                                   ["unique_id", "start_logits", "end_logits"])


def write_predictions(all_examples, all_features, all_results, n_best_size,
                      max_answer_length, do_lower_case, output_prediction_file,
                      output_nbest_file, verbose_logging):
    
    if torch.distributed.get_rank() == 0:
        """Write final predictions to the json file."""
        logger.info("Writing predictions to: %s" % (output_prediction_file))
        logger.info("Writing nbest to: %s" % (output_nbest_file))


    example_index_to_features = collections.defaultdict(list)
    for feature in all_features:
        example_index_to_features[feature.example_index].append(feature)

    unique_id_to_result = {}
    for result in all_results:
        unique_id_to_result[result.unique_id] = result

    _PrelimPrediction = collections.namedtuple(  # pylint: disable=invalid-name
        "PrelimPrediction", [
            "feature_index", "start_index", "end_index", "start_logit",
            "end_logit"
        ])

    all_predictions = collections.OrderedDict()
    all_nbest_json = collections.OrderedDict()
    for (example_index, example) in enumerate(all_examples):
        features = example_index_to_features[example_index]

        prelim_predictions = []
        for (feature_index, feature) in enumerate(features):
            result = unique_id_to_result[feature.unique_id]

            start_indexes = _get_best_indexes(result.start_logits, n_best_size)
            end_indexes = _get_best_indexes(result.end_logits, n_best_size)
            for start_index in start_indexes:
                for end_index in end_indexes:
                    # We could hypothetically create invalid predictions, e.g., predict
                    # that the start of the span is in the question. We throw out all
                    # invalid predictions.
                    if start_index >= len(feature.tokens):
                        continue
                    if end_index >= len(feature.tokens):
                        continue
                    if start_index not in feature.token_to_orig_map:
                        continue
                    if end_index not in feature.token_to_orig_map:
                        continue
                    if not feature.token_is_max_context.get(
                            start_index, False):
                        continue
                    if end_index < start_index:
                        continue
                    length = end_index - start_index + 1
                    if length > max_answer_length:
                        continue
                    prelim_predictions.append(
                        _PrelimPrediction(
                            feature_index=feature_index,
                            start_index=start_index,
                            end_index=end_index,
                            start_logit=result.start_logits[start_index],
                            end_logit=result.end_logits[end_index]))

        prelim_predictions = sorted(prelim_predictions,
                                    key=lambda x:
                                    (x.start_logit + x.end_logit),
                                    reverse=True)

        _NbestPrediction = collections.namedtuple(  # pylint: disable=invalid-name
            "NbestPrediction", ["text", "start_logit", "end_logit"])

        seen_predictions = {}
        nbest = []
        for pred in prelim_predictions:
            if len(nbest) >= n_best_size:
                break
            feature = features[pred.feature_index]

            tok_tokens = feature.tokens[pred.start_index:(pred.end_index + 1)]
            orig_doc_start = feature.token_to_orig_map[pred.start_index]
            orig_doc_end = feature.token_to_orig_map[pred.end_index]
            orig_tokens = example.doc_tokens[orig_doc_start:(orig_doc_end + 1)]
            tok_text = " ".join(tok_tokens)

            # De-tokenize WordPieces that have been split off.
            tok_text = tok_text.replace(" ##", "")
            tok_text = tok_text.replace("##", "")

            # Clean whitespace
            tok_text = tok_text.strip()
            tok_text = " ".join(tok_text.split())
            orig_text = " ".join(orig_tokens)

            final_text = get_final_text(tok_text, orig_text, do_lower_case,
                                        verbose_logging)
            if final_text in seen_predictions:
                continue

            seen_predictions[final_text] = True
            nbest.append(
                _NbestPrediction(text=final_text,
                                 start_logit=pred.start_logit,
                                 end_logit=pred.end_logit))

        # In very rare edge cases we could have no valid predictions. So we
        # just create a nonce prediction in this case to avoid failure.
        if not nbest:
            nbest.append(
                _NbestPrediction(text="empty", start_logit=0.0, end_logit=0.0))

        assert len(nbest) >= 1

        total_scores = []
        for entry in nbest:
            total_scores.append(entry.start_logit + entry.end_logit)

        probs = _compute_softmax(total_scores)

        nbest_json = []
        for (i, entry) in enumerate(nbest):
            output = collections.OrderedDict()
            output["text"] = entry.text
            output["probability"] = probs[i]
            output["start_logit"] = entry.start_logit
            output["end_logit"] = entry.end_logit
            nbest_json.append(output)

        assert len(nbest_json) >= 1

        all_predictions[example.qas_id] = nbest_json[0]["text"]
        all_nbest_json[example.qas_id] = nbest_json

    with open(output_prediction_file, "w") as writer:
        writer.write(json.dumps(all_predictions, indent=4) + "\n")

    with open(output_nbest_file, "w") as writer:
        writer.write(json.dumps(all_nbest_json, indent=4) + "\n")


def get_final_text(pred_text, orig_text, do_lower_case, verbose_logging=False):
    """Project the tokenized prediction back to the original text."""

    # When we created the data, we kept track of the alignment between original
    # (whitespace tokenized) tokens and our WordPiece tokenized tokens. So
    # now `orig_text` contains the span of our original text corresponding to the
    # span that we predicted.
    #
    # However, `orig_text` may contain extra characters that we don't want in
    # our prediction.
    #
    # For example, let's say:
    #   pred_text = steve smith
    #   orig_text = Steve Smith's
    #
    # We don't want to return `orig_text` because it contains the extra "'s".
    #
    # We don't want to return `pred_text` because it's already been normalized
    # (the SQuAD eval script also does punctuation stripping/lower casing but
    # our tokenizer does additional normalization like stripping accent
    # characters).
    #
    # What we really want to return is "Steve Smith".
    #
    # Therefore, we have to apply a semi-complicated alignment heruistic between
    # `pred_text` and `orig_text` to get a character-to-charcter alignment. This
    # can fail in certain cases in which case we just return `orig_text`.

    def _strip_spaces(text):
        ns_chars = []
        ns_to_s_map = collections.OrderedDict()
        for (i, c) in enumerate(text):
            if c == " ":
                continue
            ns_to_s_map[len(ns_chars)] = i
            ns_chars.append(c)
        ns_text = "".join(ns_chars)
        return (ns_text, ns_to_s_map)

    # We first tokenize `orig_text`, strip whitespace from the result
    # and `pred_text`, and check if they are the same length. If they are
    # NOT the same length, the heuristic has failed. If they are the same
    # length, we assume the characters are one-to-one aligned.
    tokenizer = BasicTokenizer(do_lower_case=do_lower_case)

    tok_text = " ".join(tokenizer.tokenize(orig_text))

    start_position = tok_text.find(pred_text)
    if start_position == -1:
        if verbose_logging and torch.distributed.get_rank() == 0:
            logger.info("Unable to find text: '%s' in '%s'" %
                        (pred_text, orig_text))
        return orig_text
    end_position = start_position + len(pred_text) - 1

    (orig_ns_text, orig_ns_to_s_map) = _strip_spaces(orig_text)
    (tok_ns_text, tok_ns_to_s_map) = _strip_spaces(tok_text)

    if len(orig_ns_text) != len(tok_ns_text):
        if verbose_logging and torch.distributed.get_rank() == 0:
            logger.info(
                "Length not equal after stripping spaces: '%s' vs '%s'",
                orig_ns_text, tok_ns_text)
        return orig_text

    # We then project the characters in `pred_text` back to `orig_text` using
    # the character-to-character alignment.
    tok_s_to_ns_map = {}
    for (i, tok_index) in tok_ns_to_s_map.items():
        tok_s_to_ns_map[tok_index] = i

    orig_start_position = None
    if start_position in tok_s_to_ns_map:
        ns_start_position = tok_s_to_ns_map[start_position]
        if ns_start_position in orig_ns_to_s_map:
            orig_start_position = orig_ns_to_s_map[ns_start_position]

    if orig_start_position is None:
        if verbose_logging and torch.distributed.get_rank() == 0:
            logger.info("Couldn't map start position")
        return orig_text

    orig_end_position = None
    if end_position in tok_s_to_ns_map:
        ns_end_position = tok_s_to_ns_map[end_position]
        if ns_end_position in orig_ns_to_s_map:
            orig_end_position = orig_ns_to_s_map[ns_end_position]

    if orig_end_position is None:
        if verbose_logging and torch.distributed.get_rank() == 0:
            logger.info("Couldn't map end position")
        return orig_text

    output_text = orig_text[orig_start_position:(orig_end_position + 1)]
    return output_text


def _get_best_indexes(logits, n_best_size):
    """Get the n-best logits from a list."""
    index_and_score = sorted(enumerate(logits),
                             key=lambda x: x[1],
                             reverse=True)

    best_indexes = []
    for i in range(len(index_and_score)):
        if i >= n_best_size:
            break
        best_indexes.append(index_and_score[i][0])
    return best_indexes


def _compute_softmax(scores):
    """Compute softmax probability over raw logits."""
    if not scores:
        return []

    max_score = None
    for score in scores:
        if max_score is None or score > max_score:
            max_score = score

    exp_scores = []
    total_sum = 0.0
    for score in scores:
        x = math.exp(score - max_score)
        exp_scores.append(x)
        total_sum += x

    probs = []
    for score in exp_scores:
        probs.append(score / total_sum)
    return probs


def warmup_linear(x, warmup=0.002):
    if x < warmup:
        return x / warmup
    return 1.0 - x


def copy_optimizer_params_to_model(named_params_model, named_params_optimizer):
    """ Utility function for optimize_on_cpu and 16-bits training.
        Copy the parameters optimized on CPU/RAM back to the model on GPU
    """
    for (name_opti, param_opti), (name_model,
                                  param_model) in zip(named_params_optimizer,
                                                      named_params_model):
        if name_opti != name_model:
            if torch.distributed.get_rank() == 0:
                logger.error("name_opti != name_model: {} {}".format(
                name_opti, name_model))
            raise ValueError
        param_model.data.copy_(param_opti.data)


def set_optimizer_params_grad(named_params_optimizer,
                              named_params_model,
                              test_nan=False):
    """ Utility function for optimize_on_cpu and 16-bits training.
        Copy the gradient of the GPU parameters to the CPU/RAMM copy of the model
    """
    is_nan = False
    for (name_opti, param_opti), (name_model,
                                  param_model) in zip(named_params_optimizer,
                                                      named_params_model):
        if name_opti != name_model:
            if torch.distributed.get_rank() == 0:
                logger.error("name_opti != name_model: {} {}".format(
                name_opti, name_model))
            raise ValueError
        if param_model.grad is not None:
            max = param_model.grad.max
            if test_nan and max != max:
                is_nan = True
            if param_opti.grad is None:
                param_opti.grad = torch.nn.Parameter(
                    param_opti.data.new().resize_(*param_opti.data.size()))
            param_opti.grad.data.copy_(param_model.grad.data)
        else:
            param_opti.grad = None
    return is_nan

# 采用Snapshot写入Checkpoint
def save_checkpoint_in_disk_snapshot(progress_save, app_state, checkpoint_save_work_dir):
    # 首先清空Checkpoint文件夹
    # if torch.distributed.get_rank() == 0:
    if 0 == 0:
        # delete_folder_contents(checkpoint_save_work_dir)
        # 快照先写入CPU内存再写入本地磁盘
        # torchsnapshot: take snapshot
        progress_save["current_epoch"] += 1
        snapshot = torchsnapshot.Snapshot.take(
            # f"{checkpoint_save_work_dir}/run-{uuid.uuid4()}-epoch-{progress['current_epoch']}-model",
            # f"{checkpoint_save_work_dir}/run-{uuid.uuid4()}-epoch-{progress['current_epoch']}-optimizer",
            f"{checkpoint_save_work_dir}/run-{uuid.uuid4()}-epoch-{progress_save['current_epoch']}-model-optimizer",
            app_state,
            replicated=["**"],
            # this pattern treats all states as replicated
        )
    return


def main():
    parser = get_argument_parser()

    deepspeed.init_distributed(dist_backend='nccl')

    # Include DeepSpeed configuration arguments
    parser = deepspeed.add_config_arguments(parser)

    args = parser.parse_args()
    args.local_rank = int(os.environ['LOCAL_RANK'])
    args.train_batch_size = int(args.train_batch_size /
                                args.gradient_accumulation_steps)

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    tokenizer = BertTokenizer.from_pretrained(args.bert_model,
                                              do_lower_case=args.do_lower_case)

    train_examples = None
    num_train_steps = None
    if args.do_train:
        train_examples = read_squad_examples(input_file=args.train_file,
                                             is_training=True)
        num_train_steps = int(
            len(train_examples) / args.train_batch_size /
            args.gradient_accumulation_steps * args.num_train_epochs)

    # Prepare model
    # model = BertForQuestionAnswering.from_pretrained(args.bert_model,
    #            cache_dir=PYTORCH_PRETRAINED_BERT_CACHE / 'distributed_{}'.format(args.local_rank))

    # Support for word embedding padding checkpoints
    # Prepare model

    bert_model_config = {
        "vocab_size_or_config_json_file": 119547,
        "hidden_size": 1024,
        "num_hidden_layers": 24,
        "num_attention_heads": 16,
        "intermediate_size": 4096,
        "hidden_act": "gelu",
        "hidden_dropout_prob": args.dropout,
        "attention_probs_dropout_prob": args.dropout,
        "hidden_dropout_prob": 0.1,
        "attention_probs_dropout_prob": 0.1,
        "max_position_embeddings": 512,
        "type_vocab_size": 2,
        "initializer_range": 0.02
    }

    if args.ckpt_type == "DS":
        if args.preln:
            bert_config = BertConfigPreLN(**bert_model_config)
        else:
            bert_config = BertConfig(**bert_model_config)
    else:
        # Models from Tensorflow and Huggingface are post-LN.
        if args.preln:
            raise ValueError(
                "Should NOT use --preln if the loading checkpoint doesn't use pre-layer-norm."
        )

        # Use the original bert config if want to load from non-DeepSpeed checkpoint.
        if args.origin_bert_config_file is None:
            raise ValueError(
                "--origin_bert_config_file is required for loading non-DeepSpeed checkpoint."
            )

        bert_config = BertConfig.from_json_file(args.origin_bert_config_file)

        if bert_config.vocab_size != len(tokenizer.vocab):
            raise ValueError("vocab size from original checkpoint mismatch.")

    bert_config.vocab_size = len(tokenizer.vocab)
    # Padding for divisibility by 8
    if bert_config.vocab_size % 8 != 0:
        vocab_diff = 8 - (bert_config.vocab_size % 8)
        bert_config.vocab_size += vocab_diff

    if args.preln:
        model = BertForQuestionAnsweringPreLN(bert_config, args)
    else:
        model = BertForQuestionAnswering(bert_config, args)

    print("VOCAB SIZE:", bert_config.vocab_size)
    # 不加载Checkpoint
    if args.model_file is not "0" and False:
        logger.info(f"Loading Pretrained Bert Encoder from: {args.model_file}")

        if args.ckpt_type == "DS":
            checkpoint_state_dict = torch.load(
                args.model_file, map_location=torch.device("cpu"))
            if 'module' in checkpoint_state_dict:
                logger.info('Loading DeepSpeed v2.0 style checkpoint')
                model.load_state_dict(checkpoint_state_dict['module'],
                                      strict=False)
            elif 'model_state_dict' in checkpoint_state_dict:
                model.load_state_dict(
                    checkpoint_state_dict['model_state_dict'], strict=False)
            else:
                raise ValueError("Unable to find model state in checkpoint")
        else:
            from convert_bert_ckpt_to_deepspeed import convert_ckpt_to_deepspeed
            convert_ckpt_to_deepspeed(model, args.ckpt_type, args.model_file,
                                      vocab_diff,
                                      args.deepspeed_transformer_kernel)

        logger.info(f"Pretrained Bert Encoder Loaded from: {args.model_file}")

    # Prepare optimizer
    param_optimizer = list(model.named_parameters())

    # hack to remove pooler, which is not used
    # thus it produce None grad that break apex
    param_optimizer = [n for n in param_optimizer if 'pooler' not in n[0]]

    no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
    if args.deepspeed_transformer_kernel:
        no_decay = no_decay + [
            'attn_nw', 'attn_nb', 'norm_w', 'norm_b', 'attn_qkvb', 'attn_ob',
            'inter_b', 'output_b'
        ]
    optimizer_grouped_parameters = [{
        'params':
        [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)],
        'weight_decay':
        0.01
    }, {
        'params':
        [p for n, p in param_optimizer if any(nd in n for nd in no_decay)],
        'weight_decay':
        0.0
    }]
    
    
    # 
    # 创建内存共享缓冲区用于保存CPU内的检查点, 
    # 共享缓冲区的创建可以由节点内的其他进程完成, 这些进程可以异步地执行, 而不需要
    # if dist.get_rank() == 0:
    if (dist.get_rank()) % torch.cuda.device_count() == 0:
        _name = 'parameter_buffer'
        shm = shared_memory.SharedMemory(create=True, size=1024*1024*1024*100, name =_name)
        
        
        # _name_backup = 'parameter_buffer_backup'
        # shm_backup = shared_memory.SharedMemory(create=True, size=1024*1024*1024*100, name =_name_backup)

        # 创建一个 NumPy 数组并将其存储在共享内存中
        # array = np.ndarray((10,), dtype=np.float32, buffer=shm.buf)
        # array[:] = np.arange(10)

        # resource_tracker.unregister(shm.name, 'shared_memory')
        # resource_tracker.register(shm.name, 'shared_memory')
        # print(array)
    
    # return_items = [engine, engine.optimizer, engine.training_dataloader, engine.lr_scheduler]
    # model = engine
    
    # model, optimizer, _, _ = deepspeed.initialize(
    #     args=args,
    #     model=model,
    #     model_parameters=optimizer_grouped_parameters,
    #     dist_init_required=True)
    
    
    model, optimizer, _, _ = oteacheck_lib.initialize(
        args=args,
        model=model,
        model_parameters=optimizer_grouped_parameters,
        dist_init_required=True)
    
    
    


    if args.local_rank == -1 or args.no_cuda:
        device = torch.device("cuda" if torch.cuda.is_available()
                              and not args.no_cuda else "cpu")
        n_gpu = torch.cuda.device_count()
    else:
        torch.cuda.set_device(args.local_rank)
        device = torch.device("cuda", args.local_rank)
        n_gpu = 1
        # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        #torch.distributed.init_process_group(backend='nccl')
    
    if torch.distributed.get_rank() == 0:
        logger.info(
            "device: {} n_gpu: {}, distributed training: {}, 16-bits training: {}".
            format(device, n_gpu, bool(args.local_rank != -1), args.fp16))

    if args.gradient_accumulation_steps < 1:
        raise ValueError(
            "Invalid gradient_accumulation_steps parameter: {}, should be >= 1"
            .format(args.gradient_accumulation_steps))
    if n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)

    if not args.do_train and not args.do_predict:
        raise ValueError(
            "At least one of `do_train` or `do_predict` must be True.")

    if args.do_train:
        if not args.train_file:
            raise ValueError(
                "If `do_train` is True, then `train_file` must be specified.")
    if args.do_predict:
        if not args.predict_file:
            raise ValueError(
                "If `do_predict` is True, then `predict_file` must be specified."
            )

    if os.path.exists(args.output_dir) and os.listdir(
            args.output_dir) and args.do_train:
        os.makedirs(args.output_dir, exist_ok=True)
    
    # if os.path.exists(args.output_dir) and os.listdir(
    #         args.output_dir) and args.do_train:
    #     pass
    #     # delete_folder_contents(args.output_dir)
        
    #     # raise ValueError(
    #     #     "Output directory () already exists and is not empty.")
    # elif torch.distributed.get_rank() == 0:
    #     os.makedirs(args.output_dir, exist_ok=True)
    

    # Prepare Summary writer
    if torch.distributed.get_rank() == 0 and args.job_name is not None:
        args.summary_writer = get_summary_writer(name=args.job_name,
                                                 base=args.output_dir)
    else:
        args.summary_writer = None
    
    if torch.distributed.get_rank() == 0:
        logger.info("propagate deepspeed-config settings to client settings")
    
    
    args.train_batch_size = model.train_micro_batch_size_per_gpu()
    args.gradient_accumulation_steps = model.gradient_accumulation_steps()
    args.fp16 = model.fp16_enabled()
    
    args.print_steps = model.steps_per_print()
    
    
    args.learning_rate = model.get_lr()[0]
    args.wall_clock_breakdown = model.wall_clock_breakdown()

    t_total = num_train_steps
    if args.local_rank != -1:
        t_total = t_total // torch.distributed.get_world_size()

    global_step = 0
    if args.do_train:
        cached_train_features_file = args.train_file + '_{0}_{1}_{2}_{3}'.format(
            list(filter(None, args.bert_model.split('/'))).pop(),
            str(args.max_seq_length), str(args.doc_stride),
            str(args.max_query_length))
        train_features = None
        try:
            with open(cached_train_features_file, "rb") as reader:
                train_features = pickle.load(reader)
        except:
            train_features = convert_examples_to_features(
                examples=train_examples,
                tokenizer=tokenizer,
                max_seq_length=args.max_seq_length,
                doc_stride=args.doc_stride,
                max_query_length=args.max_query_length,
                is_training=True)
            if args.local_rank == -1 or torch.distributed.get_rank() == 0:
                logger.info("  Saving train features into cached file %s",
                            cached_train_features_file)
                with open(cached_train_features_file, "wb") as writer:
                    pickle.dump(train_features, writer)
        
        if torch.distributed.get_rank() == 0:
            logger.info("***** Running training *****")
            logger.info("  Num orig examples = %d", len(train_examples))
            logger.info("  Num split examples = %d", len(train_features))
            logger.info("  Batch size = %d", args.train_batch_size)
            logger.info("  Num steps = %d", num_train_steps)
        
        
        all_input_ids = torch.tensor([f.input_ids for f in train_features],
                                     dtype=torch.long)
        all_input_mask = torch.tensor([f.input_mask for f in train_features],
                                      dtype=torch.long)
        all_segment_ids = torch.tensor([f.segment_ids for f in train_features],
                                       dtype=torch.long)
        all_start_positions = torch.tensor(
            [f.start_position for f in train_features], dtype=torch.long)
        all_end_positions = torch.tensor(
            [f.end_position for f in train_features], dtype=torch.long)
        train_data = TensorDataset(all_input_ids, all_input_mask,
                                   all_segment_ids, all_start_positions,
                                   all_end_positions)
        if args.local_rank == -1:
            train_sampler = RandomSampler(train_data)
        else:
            train_sampler = DistributedSampler(train_data)
        train_dataloader = DataLoader(train_data,
                                      sampler=train_sampler,
                                      batch_size=args.train_batch_size)

        model.train()
        ema_loss = 0.
        sample_count = 0
        num_epoch = 0
        global all_step_time
        

        ave_rounds = 10
        
        progress = torchsnapshot.StateDict(current_epoch=0)
        # torchsnapshot: define app state
        app_state: Dict[str, Stateful] = {
            "rng_state": torchsnapshot.RNGState(),
            "model": model,
            # "optim": optimizer,
            "progress": progress,
        }
        snapshot: Optional[Snapshot] = None
        
        iteration_time = AverageMeter('Time', ':6.3f')
        batch_time_start = time.time()
        for _ in trange(int(args.num_train_epochs), desc="Epoch"):
            num_epoch += 1
            epoch_step = 0
            # for step, batch in enumerate(
            #         tqdm(train_dataloader, desc="Iteration", smoothing=0)):
            end = time.time()
            
            
            iteration_io_time_array = []
            iteration_forward_time_array = []
            iteration_backward_time_array = []
            iteration_step_time_array = []
            
            for step, batch in enumerate(train_dataloader):
                start_time = time.time()
                io_time = time.time()
                bs_size = batch[0].size()[0]
                if n_gpu == 1:
                    batch = tuple(
                        t.to(device)
                        for t in batch)  # multi-gpu does scattering it-self
                input_ids, input_mask, segment_ids, start_positions, end_positions = batch
                
                iteration_io_time_array.append(time.time() - io_time)
                
                
                forward_time = time.time()
                loss = model(input_ids, segment_ids, input_mask,
                             start_positions, end_positions)
                if n_gpu > 1:
                    loss = loss.mean()  # mean() to average on multi-gpu.
                if args.gradient_accumulation_steps > 1:
                    loss = loss / args.gradient_accumulation_steps
                ema_loss = args.loss_plot_alpha * ema_loss + (
                    1 - args.loss_plot_alpha) * loss.item()
                
                iteration_forward_time_array.append(time.time() - forward_time)
                
                backward_time = time.time()
                # print('model.backward(loss)')
                # 在反向传播的时候执行Allreduce操作, 
                model.backward(loss)
                
                iteration_backward_time_array.append(time.time()- backward_time)

                loss_item = loss.item() * args.gradient_accumulation_steps
                loss = None

                sample_count += (args.train_batch_size *
                                 torch.distributed.get_world_size())
                
                step_time = time.time()
                if (step + 1) % args.gradient_accumulation_steps == 0:
                    # modify learning rate with special warm up BERT uses
                    lr_this_step = args.learning_rate * warmup_linear(
                        global_step / t_total, args.warmup_proportion)
                    for param_group in optimizer.param_groups:
                        param_group['lr'] = lr_this_step

                    model.step()
                    global_step += 1
                    epoch_step += 1

                    if torch.distributed.get_rank(
                    ) == 0 and args.summary_writer:
                        summary_events = [
                            (f'Train/Steps/lr', lr_this_step, global_step),
                            (f'Train/Samples/train_loss', loss_item,
                             sample_count),
                            (f'Train/Samples/lr', lr_this_step, sample_count),
                            (f'Train/Samples/train_ema_loss', ema_loss,
                             sample_count)
                        ]

                        if args.fp16 and hasattr(optimizer, 'cur_scale'):
                            summary_events.append(
                                (f'Train/Samples/scale', optimizer.cur_scale,
                                 sample_count))
                        write_summary_events(args.summary_writer,
                                             summary_events)
                        args.summary_writer.flush()

                    # 打印输出日志
                    if torch.distributed.get_rank() == 0 and (
                            step + 1) % args.print_steps == 0:
                        
                        logger.info(f"bert_squad_progress: step={global_step} lr={lr_this_step} loss={ema_loss}")

                else:
                    model.step()
                
                iteration_step_time_array.append(time.time() - step_time)

                if is_time_to_exit(args=args,
                                   epoch_steps=epoch_step,
                                   global_steps=global_step):
                    
                    if torch.distributed.get_rank() == 0:
                        logger.info(f'Warning: Early epoch termination due to max steps limit, epoch step ={epoch_step}, global step = {global_step}, epoch = {num_epoch}')

                    break
                one_step_time = time.time() - start_time
                all_step_time += one_step_time
                
                if step >= 30:
                    iteration_time.update(time.time() - end)
                    
                if step % args.ckpt_freq == 0:
                    progress_save = torchsnapshot.StateDict(current_epoch=step)
                    # torchsnapshot: define app state
                    app_state: Dict[str, Stateful] = {
                            "rng_state": torchsnapshot.RNGState(),
                            "model": model,
                            "optim": optimizer,
                            "progress": progress,
                    }
                
                    checkpoint_save_work_dir = 'checkpoints'
                    oteacheck_lib.utils.save_checkpoint_in_disk_snapshot(progress_save, app_state, checkpoint_save_work_dir)

                if (step + 1) % (
                        ave_rounds) == 0 and torch.distributed.get_rank() == 0:
                    
                    # numel_count = 0
                    # numel_count_model = 0
                    # for key, value in optimizer.state.items():
                    #     numel_count += value['exp_avg'].numel() + value['exp_avg_sq'].numel()
                    # for key, value in model.state_dict().items():
                    #     numel_count_model += value.numel()
                    
                    # print("Size of Optimizer Size:", numel_count)
                    # print("Size of Model State:", numel_count_model)
                    
                    print(
                        ' At step {}, averaged throughput for {} rounds is: {} Samples/s'
                        .format(
                            step, ave_rounds,
                            bs_size * ave_rounds *
                            torch.distributed.get_world_size() /
                            all_step_time),
                        flush=True)
                    all_step_time = 0.0
                    print('------------------------------------------------------------------------')
                    print('one_step_time = ', one_step_time)
                    print('Avg_Iteration_time = ', iteration_time.avg)
                    
                    batch_time_end = time.time() - batch_time_start
                    backward_time = sum(model.backward_time_array)
                    allreduce_time = sum(model.allreduce_time_array)

                    # print('model.engine_timers.backward_inner_timers = ', backward_time)
                    # print('model.engine_timers.backward_reduce_timers = ', allreduce_time)
                    
                    print('per_batch_time = ', batch_time_end/ave_rounds)
                    # print('per_iteration_io = ', sum(iteration_io_time_array)/ave_rounds)
                    print('per_iteration_forward_time = ', sum(iteration_forward_time_array)/ave_rounds)
                    per_iteration_backward_allreduce_time = sum(iteration_backward_time_array)/ave_rounds
                    print('per_iteration_backward_allreduce_time = ', per_iteration_backward_allreduce_time)
                    print('per_iteration_step_time = ', sum(iteration_step_time_array)/ave_rounds)

                    
                    per_backward_time = backward_time/ave_rounds
                    print('per_backward_time = ', per_backward_time)
                    # print('per_allreduce_time = ', allreduce_time/ave_rounds)
                    
                    print('per_ide_time = ', per_iteration_backward_allreduce_time - per_backward_time)
                            
                    model.backward_time_array = []
                    model.allreduce_time_array = []
                    
                    
                    iteration_io_time_array = []
                    iteration_backward_time_array = []
                    iteration_forward_time_array = []
                    iteration_step_time_array = []
                    
                    
                    batch_time_start = time.time()


                    
                end = time.time()
    
        if torch.distributed.get_rank() == 0:
            print("Final_Iteration_Time = ", iteration_time.avg)
            with open("./iteartion_time_bert.txt", "w+") as fp:
                fp.write(iteration_time.avg)


    # Save a trained model
    # model_to_save = model.module if hasattr(model, 'module') else model  # Only save the model it-self
    # output_model_file = os.path.join(args.output_dir, "pytorch_model.bin")
    # if args.do_train:
    #    torch.save(model_to_save.state_dict(), output_model_file)

    # Load a trained model that you have fine-tuned
    # model_state_dict = torch.load(output_model_file)
    # model = BertForQuestionAnswering.from_pretrained(args.bert_model, state_dict=model_state_dict)
    # model.to(device)

    if args.do_predict and (args.local_rank == -1
                            or torch.distributed.get_rank() == 0):
        eval_examples = read_squad_examples(input_file=args.predict_file,
                                            is_training=False)
        eval_features = convert_examples_to_features(
            examples=eval_examples,
            tokenizer=tokenizer,
            max_seq_length=args.max_seq_length,
            doc_stride=args.doc_stride,
            max_query_length=args.max_query_length,
            is_training=False)

        if torch.distributed.get_rank() == 0:
            logger.info("***** Running predictions *****")
            logger.info("  Num orig examples = %d", len(eval_examples))
            logger.info("  Num split examples = %d", len(eval_features))
            logger.info("  Batch size = %d", args.predict_batch_size)
        
        
        all_input_ids = torch.tensor([f.input_ids for f in eval_features],
                                     dtype=torch.long)
        all_input_mask = torch.tensor([f.input_mask for f in eval_features],
                                      dtype=torch.long)
        all_segment_ids = torch.tensor([f.segment_ids for f in eval_features],
                                       dtype=torch.long)
        all_example_index = torch.arange(all_input_ids.size(0),
                                         dtype=torch.long)
        eval_data = TensorDataset(all_input_ids, all_input_mask,
                                  all_segment_ids, all_example_index)
        # Run prediction for full data
        eval_sampler = SequentialSampler(eval_data)
        eval_dataloader = DataLoader(eval_data,
                                     sampler=eval_sampler,
                                     batch_size=args.predict_batch_size)

        model.eval()
        all_results = []
        
        if torch.distributed.get_rank() == 0:
            logger.info("Start evaluating")
        
        # for input_ids, input_mask, segment_ids, example_indices in tqdm(
        #         eval_dataloader, desc="Evaluating"):
        for input_ids, input_mask, segment_ids, example_indices in eval_dataloader:
            
            if len(all_results) % 1000 == 0 and torch.distributed.get_rank() == 0:
                logger.info("Processing example: %d" % (len(all_results)))
                
            input_ids = input_ids.to(device)
            input_mask = input_mask.to(device)
            segment_ids = segment_ids.to(device)
            with torch.no_grad():
                batch_start_logits, batch_end_logits = model(
                    input_ids, segment_ids, input_mask)
            for i, example_index in enumerate(example_indices):
                start_logits = batch_start_logits[i].detach().cpu().tolist()
                end_logits = batch_end_logits[i].detach().cpu().tolist()
                eval_feature = eval_features[example_index.item()]
                unique_id = int(eval_feature.unique_id)
                all_results.append(
                    RawResult(unique_id=unique_id,
                              start_logits=start_logits,
                              end_logits=end_logits))
        output_prediction_file = os.path.join(args.output_dir,
                                              "predictions.json")
        output_nbest_file = os.path.join(args.output_dir,
                                         "nbest_predictions.json")
        write_predictions(eval_examples, eval_features, all_results,
                          args.n_best_size, args.max_answer_length,
                          args.do_lower_case, output_prediction_file,
                          output_nbest_file, args.verbose_logging)

# 从cifar100中添加的类用来记录Iteartion的平均时间
from enum import Enum
class Summary(Enum):
    NONE = 0
    AVERAGE = 1
    SUM = 2
    COUNT = 3
    
class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self, name, fmt=':f', summary_type=Summary.AVERAGE):
        self.name = name
        self.fmt = fmt
        self.summary_type = summary_type
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count

    def all_reduce(self):
        if torch.cuda.is_available():
            device = torch.device("cuda")
        elif torch.backends.mps.is_available():
            device = torch.device("mps")
        else:
            device = torch.device("cpu")
        total = torch.tensor([self.sum, self.count], dtype=torch.float32, device=device)
        dist.all_reduce(total, dist.ReduceOp.SUM, async_op=False)
        self.sum, self.count = total.tolist()
        self.avg = self.sum / self.count

    def __str__(self):
        fmtstr = '{name} {val' + self.fmt + '} ({avg' + self.fmt + '})'
        return fmtstr.format(**self.__dict__)
    
    def summary(self):
        fmtstr = ''
        if self.summary_type is Summary.NONE:
            fmtstr = ''
        elif self.summary_type is Summary.AVERAGE:
            fmtstr = '{name} {avg:.3f}'
        elif self.summary_type is Summary.SUM:
            fmtstr = '{name} {sum:.3f}'
        elif self.summary_type is Summary.COUNT:
            fmtstr = '{name} {count:.3f}'
        else:
            raise ValueError('invalid summary type %r' % self.summary_type)
        
        return fmtstr.format(**self.__dict__)

if __name__ == "__main__":
    main()
