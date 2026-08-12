import math

import numpy as np
import torch

from exp.benchmark.run_midterm_multivector_followup_ablation import (
    deterministic_idf_filter,
    idf_weight,
    raw_cache_identity,
    retained_token_mask,
    weighted_maxsim_score,
)


class FakeTokenizer:
    all_special_ids = [101, 102]

    @staticmethod
    def convert_ids_to_tokens(token_id):
        return {101: "[CLS]", 102: "[SEP]", 1: "。", 2: "指标"}.get(token_id, str(token_id))

    @staticmethod
    def decode(token_ids, skip_special_tokens=True):
        return {101: "", 102: "", 1: "。", 2: "指标"}.get(token_ids[0], str(token_ids[0]))


def test_retained_token_mask_removes_padding_and_special_tokens():
    input_ids = torch.tensor([[101, 2, 102, 0]])
    attention = torch.tensor([[1, 1, 1, 0]])
    special = torch.tensor([[1, 0, 1, 1]])
    assert retained_token_mask(input_ids, attention, special).tolist() == [[False, True, False, False]]


def test_idf_formula_is_exact():
    assert math.isclose(idf_weight(9, 4), math.log(2.0) + 1.0)


def test_weighted_maxsim_uses_query_token_idf_weights():
    query = np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    page = np.asarray([[1.0, 0.0], [1.0, 0.0]], dtype=np.float32)
    score = weighted_maxsim_score(query, page, np.asarray([3.0, 1.0], dtype=np.float32))
    assert math.isclose(score, 0.75, abs_tol=1e-6)


def test_filtered_maxsim_falls_back_when_every_token_is_removed():
    query = np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    page = np.asarray([[1.0, 0.0]], dtype=np.float32)
    score = weighted_maxsim_score(
        query,
        page,
        np.asarray([1.0, 1.0], dtype=np.float32),
        keep_mask=np.asarray([False, False]),
    )
    assert math.isclose(score, 0.5, abs_tol=1e-6)


def test_deterministic_filter_uses_only_syntax_and_visible_df():
    tokenizer = FakeTokenizer()
    assert deterministic_idf_filter(token_id=101, tokenizer=tokenizer, document_frequency=0, document_count=10) == (
        True,
        "SPECIAL_TOKEN",
    )
    assert deterministic_idf_filter(token_id=1, tokenizer=tokenizer, document_frequency=0, document_count=10) == (
        True,
        "PUNCTUATION",
    )
    assert deterministic_idf_filter(token_id=2, tokenizer=tokenizer, document_frequency=8, document_count=10) == (
        True,
        "DF_RATIO_GE_0.8",
    )
    assert deterministic_idf_filter(token_id=2, tokenizer=tokenizer, document_frequency=7, document_count=10) == (
        False,
        "KEPT",
    )


def test_raw_cache_identity_tracks_contract_and_text():
    first = raw_cache_identity(kind="pages", ids=["P"], texts=["text"], revision="rev")
    same = raw_cache_identity(kind="pages", ids=["P"], texts=["text"], revision="rev")
    changed = raw_cache_identity(kind="pages", ids=["P"], texts=["changed"], revision="rev")
    assert first == same
    assert first != changed
    assert first["representation"] == "raw_token"
    assert first["max_length"] == 512
    assert "special_token_policy" in first
