from typing import Dict
import torch.nn as nn

from allennlp.data import DatasetReader, DataLoader, TextFieldTensors, Vocabulary
from allennlp.common import Params

from luke.pretraining.model import LukePretrainingModel
from luke.pretraining.validation_evaluator import ValidationEvaluator

from multilingual.tasks.bucc.reader import parse_bucc_file
from multilingual.tasks.bucc.evaluate import extract_sentence_embeddings
from multilingual.retrieval.scoring_functions import ScoringFunction
from multilingual.retrieval.retrievers import Retriever
from multilingual.retrieval.metrics.f1_score import compute_f1_score


class LukeSentenceEmbedding(nn.Module):
    def __init__(self, luke_model: LukePretrainingModel):
        super().__init__()
        self.luke_model = luke_model

    def forward(self, tokens: TextFieldTensors):
        input_dict = tokens["tokens"]
        word_sequence_output = super(type(self.luke_model), self.luke_model).forward(
            word_ids=input_dict["token_ids"],
            word_segment_ids=input_dict["type_ids"],
            word_attention_mask=input_dict["mask"],
        )[0]
        return word_sequence_output[:, 0]  # return the [CLS] token


@ValidationEvaluator.register("bucc")
class LukeEvaluatorBUCC(ValidationEvaluator):
    def __init__(
        self,
        dataset_reader: DatasetReader,
        source_data_path: str,
        target_data_path: str,
        gold_data_path: str,
        data_loader_params: Dict,
        scoring_function: str,
        retriever: str,
    ):
        source_dataset = dataset_reader.read(source_data_path)
        target_dataset = dataset_reader.read(target_data_path)

        vocab = Vocabulary.from_instances(source_dataset)
        vocab.extend_from_instances(target_dataset)
        source_dataset.index_with(vocab)
        target_dataset.index_with(vocab)

        data_loader_params = Params(data_loader_params)
        self.source_data_loader = DataLoader.from_params(dataset=source_dataset, params=data_loader_params.duplicate())
        self.target_data_loader = DataLoader.from_params(dataset=target_dataset, params=data_loader_params.duplicate())
        self.gold_indices = [idx_pair for idx_pair in parse_bucc_file(gold_data_path)]

        self.scoring_function = ScoringFunction.by_name(scoring_function)()
        self.retriever = Retriever.by_name(retriever)()

    def __call__(self, model) -> float:
        device = next(model.parameters()).device

        model = LukeSentenceEmbedding(model)

        source_embeddings, source_indices = extract_sentence_embeddings(self.source_data_loader, model, device=device)
        target_embeddings, target_indices = extract_sentence_embeddings(self.target_data_loader, model, device=device)

        scores = self.scoring_function(source_embeddings, target_embeddings)
        max_scores, retrieved_indices = self.retriever(scores)
        max_scores = max_scores.tolist()
        retrieved_target_indices = [target_indices[i] for i in retrieved_indices]
        prediction = [(src, tgt) for src, tgt in zip(source_indices, retrieved_target_indices)]

        sorted_predictions = reversed(sorted(zip(max_scores, prediction)))
        filtered_prediction = [src_tgt for _, src_tgt in sorted_predictions][: len(self.gold_indices)]
        metrics = compute_f1_score(prediction=filtered_prediction, gold=self.gold_indices)

        return metrics["f1"]