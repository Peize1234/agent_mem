from __future__ import annotations

from exp.benchmark.run_midterm_reranker_input_diagnosis import (
    candidate_ranking,
    locate_keyword_tokens,
    pack_document_ids,
    selection_key,
)


class CharacterTokenizer:
    """Small tokenizer double for deterministic packing unit tests."""

    def encode(self, text: str, *, add_special_tokens: bool = False) -> list[int]:
        assert not add_special_tokens
        return [ord(character) for character in text]

    def num_special_tokens_to_add(self, *, pair: bool) -> int:
        return 4 if pair else 2

    def build_inputs_with_special_tokens(self, first: list[int], second: list[int]) -> list[int]:
        return [0, *first, 1, 1, *second, 1]

    def decode(self, token_ids: list[int], *, skip_special_tokens: bool) -> str:
        assert skip_special_tokens
        return "".join(chr(token) for token in token_ids)

    def __call__(
        self,
        first: str,
        second: str,
        *,
        add_special_tokens: bool = True,
        truncation: bool = False,
    ) -> dict[str, list[int]]:
        assert add_special_tokens
        assert not truncation
        return {
            "input_ids": self.build_inputs_with_special_tokens(
                self.encode(first),
                self.encode(second),
            )
        }


def test_budgeted_pack_never_exceeds_pair_limit_and_respects_priority() -> None:
    tokenizer = CharacterTokenizer()
    page = {"user_input": "U" * 10, "keywords": ["K" * 10], "summary": "S" * 100}
    config = {"order": ("user", "keywords", "summary")}

    document_ids, metadata = pack_document_ids(tokenizer, "Q" * 8, page, config, max_length=64)

    assert len(tokenizer.build_inputs_with_special_tokens([ord("Q")] * 8, document_ids)) <= 64
    assert metadata["field_allocations"]["user"]["allocated_tokens"] == metadata["field_allocations"]["user"]["full_tokens"]
    assert metadata["field_allocations"]["keywords"]["allocated_tokens"] == metadata["field_allocations"]["keywords"]["full_tokens"]
    assert metadata["field_allocations"]["summary"]["allocated_tokens"] < metadata["field_allocations"]["summary"]["full_tokens"]


def test_balanced_pack_applies_field_caps() -> None:
    tokenizer = CharacterTokenizer()
    page = {"user_input": "U" * 30, "keywords": ["K" * 30], "summary": "S" * 100}
    config = {"order": ("user", "keywords", "summary"), "caps": {"user": 12, "keywords": 9}}

    _, metadata = pack_document_ids(tokenizer, "query", page, config, max_length=80)

    assert metadata["field_allocations"]["user"]["allocated_tokens"] == 12
    assert metadata["field_allocations"]["keywords"]["allocated_tokens"] == 9
    assert metadata["field_allocations"]["summary"]["allocated_tokens"] > 0


def test_keyword_locator_returns_document_token_interval() -> None:
    tokenizer = CharacterTokenizer()
    document = "summary\nKeywords: alpha, beta"
    keyword_line = "Keywords: alpha, beta"

    start, end = locate_keyword_tokens(tokenizer, document, keyword_line)

    assert start == len("summary\n")
    assert end == len(document)


def test_candidate_ranking_reports_real_expanded_union_size() -> None:
    dense = [{"page_id": page_id, "score": 1.0 / rank} for rank, page_id in enumerate(("a", "b", "c"), 1)]
    bm25 = [{"page_id": page_id, "score": 1.0 / rank} for rank, page_id in enumerate(("c", "d", "e"), 1)]
    components = {"dense": dense, "bm25": bm25, "rrf": dense}

    ranking, count = candidate_ranking(
        components,
        {"strategy": "expanded_union", "candidate_k": 3},
    )

    assert count == 5
    assert {row["page_id"] for row in ranking[:count]} == {"a", "b", "c", "d", "e"}


def test_selection_key_prefers_recall_then_macro_and_ranking_quality() -> None:
    stronger = {
        "recall_at_5": 0.5,
        "macro_session_recall_at_5": 0.4,
        "ndcg_at_5": 0.3,
        "mrr": 0.2,
        "demoted": 3,
        "mean_candidate_count": 20,
    }
    weaker = {**stronger, "recall_at_5": 0.49, "macro_session_recall_at_5": 0.9}

    assert selection_key(stronger) > selection_key(weaker)
