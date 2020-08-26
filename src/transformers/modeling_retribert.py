# coding=utf-8
# Copyright 2019-present, the HuggingFace Inc. team, The Google AI Language Team and Facebook, Inc.
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
"""
RetriBERT model
"""


import math

import torch
import torch.nn as nn
import torch.utils.checkpoint as checkpoint

from .configuration_retribert import RetriBertConfig
from .file_utils import add_start_docstrings
from .modeling_bert import BertLayerNorm, BertModel
from .modeling_utils import PreTrainedModel
from .utils import logging


logger = logging.get_logger(__name__)

RETRIBERT_PRETRAINED_MODEL_ARCHIVE_LIST = [
    "yjernite/retribert-base-uncased",
    # See all RetriBert models at https://huggingface.co/models?filter=retribert
]


# INTERFACE FOR ENCODER AND TASK SPECIFIC MODEL #
class RetriBertPreTrainedModel(PreTrainedModel):
    """ An abstract class to handle weights initialization and
        a simple interface for downloading and loading pretrained models.
    """

    config_class = RetriBertConfig
    load_tf_weights = None
    base_model_prefix = "retribert"

    def _init_weights(self, module):
        """ Initialize the weights """
        if isinstance(module, (nn.Linear, nn.Embedding)):
            module.weight.data.normal_(mean=0.0, std=self.config.initializer_range)
        elif isinstance(module, BertLayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)
        if isinstance(module, nn.Linear) and module.bias is not None:
            module.bias.data.zero_()


RETRIBERT_START_DOCSTRING = r"""

    This model is a PyTorch `torch.nn.Module <https://pytorch.org/docs/stable/nn.html#torch.nn.Module>`_ sub-class.
    Use it as a regular PyTorch Module and refer to the PyTorch documentation for all matter related to general
    usage and behavior.

    Parameters:
        config (:class:`~transformers.RetriBertConfig`): Model configuration class with all the parameters of the model.
            Initializing with a config file does not load the weights associated with the model, only the configuration.
            Check out the :meth:`~transformers.PreTrainedModel.from_pretrained` method to load the model weights.
"""


@add_start_docstrings(
    """Bert Based model to embed queries or document for document retreival. """, RETRIBERT_START_DOCSTRING,
)
class RetriBertModel(RetriBertPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        self.projection_dim = config.projection_dim

        self.bert_query = BertModel(config)
        self.bert_doc = None if config.share_encoders else BertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.project_query = nn.Linear(config.hidden_size, config.projection_dim, bias=False)
        self.project_doc = nn.Linear(config.hidden_size, config.projection_dim, bias=False)

        self.ce_loss = nn.CrossEntropyLoss(reduction="mean")

        self.init_weights()

    def embed_sentences_checkpointed(
        self, input_ids, attention_mask, sent_encoder, checkpoint_batch_size=-1,
    ):
        # reproduces BERT forward pass with checkpointing
        if checkpoint_batch_size < 0 or input_ids.shape[0] < checkpoint_batch_size:
            return sent_encoder(input_ids, attention_mask=attention_mask)[1]
        else:
            # prepare implicit variables
            device = input_ids.device
            input_shape = input_ids.size()
            token_type_ids = torch.zeros(input_shape, dtype=torch.long, device=device)
            head_mask = [None] * sent_encoder.config.num_hidden_layers
            extended_attention_mask: torch.Tensor = sent_encoder.get_extended_attention_mask(
                attention_mask, input_shape, device
            )

            # define function for cehckpointing
            def partial_encode(*inputs):
                encoder_outputs = sent_encoder.encoder(inputs[0], attention_mask=inputs[1], head_mask=head_mask,)
                sequence_output = encoder_outputs[0]
                pooled_output = sent_encoder.pooler(sequence_output)
                return pooled_output

            # run embedding layer on everything at once
            embedding_output = sent_encoder.embeddings(
                input_ids=input_ids, position_ids=None, token_type_ids=token_type_ids, inputs_embeds=None
            )
            # run encoding and pooling on one mini-batch at a time
            pooled_output_list = []
            for b in range(math.ceil(input_ids.shape[0] / checkpoint_batch_size)):
                b_embedding_output = embedding_output[b * checkpoint_batch_size : (b + 1) * checkpoint_batch_size]
                b_attention_mask = extended_attention_mask[b * checkpoint_batch_size : (b + 1) * checkpoint_batch_size]
                pooled_output = checkpoint.checkpoint(partial_encode, b_embedding_output, b_attention_mask)
                pooled_output_list.append(pooled_output)
            return torch.cat(pooled_output_list, dim=0)

    def embed_questions(
        self, input_ids, attention_mask=None, checkpoint_batch_size=-1,
    ):
        q_reps = self.embed_sentences_checkpointed(input_ids, attention_mask, self.bert_query, checkpoint_batch_size,)
        return self.project_query(q_reps)

    def embed_answers(
        self, input_ids, attention_mask=None, checkpoint_batch_size=-1,
    ):
        a_reps = self.embed_sentences_checkpointed(
            input_ids,
            attention_mask,
            self.bert_query if self.bert_doc is None else self.bert_doc,
            checkpoint_batch_size,
        )
        return self.project_doc(a_reps)

    def forward(
        self, input_ids_query, attention_mask_query, input_ids_doc, attention_mask_doc, checkpoint_batch_size=-1
    ):
        r"""
    Args:
        input_ids_query (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_length)`):
            Indices of input sequence tokens in the vocabulary for the queries in a batch.

            Indices can be obtained using :class:`transformers.RetriBertTokenizer`.
            See :func:`transformers.PreTrainedTokenizer.encode` and
            :func:`transformers.PreTrainedTokenizer.__call__` for details.

            `What are input IDs? <../glossary.html#input-ids>`__
        attention_mask_query (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_length)`, `optional`, defaults to :obj:`None`):
            Mask to avoid performing attention on queries padding token indices.
            Mask values selected in ``[0, 1]``:
            ``1`` for tokens that are NOT MASKED, ``0`` for MASKED tokens.

            `What are attention masks? <../glossary.html#attention-mask>`__
        input_ids_doc (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_length)`):
            Indices of input sequence tokens in the vocabulary for the documents in a batch.
        attention_mask_doc (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_length)`, `optional`, defaults to :obj:`None`):
            Mask to avoid performing attention on documents padding token indices.

        checkpoint_batch_size (:obj:`int`, `optional`, defaults to `:obj:`-1`):
            If greater than 0, uses gradient checkpointing to only compute sequence representation on checkpoint_batch_size examples at a time
            on the GPU. All query representations are still compared to all document representations in the batch.

    Return:
        :obj:`torch.FloatTensor` the bi-directional cross-entropy loss obtained while trying to match each query to its corresponding document
        and each cocument to its corresponding query in the batch
        """
        device = input_ids_query.device
        q_reps = self.embed_questions(input_ids_query, attention_mask_query, checkpoint_batch_size)
        a_reps = self.embed_answers(input_ids_doc, attention_mask_doc, checkpoint_batch_size)
        compare_scores = torch.mm(q_reps, a_reps.t())
        loss_qa = self.ce_loss(compare_scores, torch.arange(compare_scores.shape[1]).to(device))
        loss_aq = self.ce_loss(compare_scores.t(), torch.arange(compare_scores.shape[0]).to(device))
        loss = (loss_qa + loss_aq) / 2
        return loss
