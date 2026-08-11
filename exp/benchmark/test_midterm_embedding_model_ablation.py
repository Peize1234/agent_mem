from exp.benchmark.run_midterm_embedding_model_ablation import (
    MODEL_SPECS,
    QWEN_QUERY_PROMPT,
    YOUTU_QUERY_PROMPT,
    actual_query_text,
    next_reasonable_max_length,
    tokenizer_audit,
)


class CharacterTokenizer:
    model_max_length = 8192
    truncation_side = "right"
    padding_side = "right"

    @staticmethod
    def encode(text, add_special_tokens=True, truncation=False):
        assert truncation is False
        return list(range(len(str(text)) + (2 if add_special_tokens else 0)))


def test_official_query_instructions_are_applied_only_to_query():
    assert actual_query_text(MODEL_SPECS["youtu"], "问题") == YOUTU_QUERY_PROMPT + "问题"
    assert actual_query_text(MODEL_SPECS["qwen3_4b"], "问题") == QWEN_QUERY_PROMPT + "问题"
    assert actual_query_text(MODEL_SPECS["acge"], "问题") == "问题"


def test_next_reasonable_max_length_preserves_all_tokens():
    assert next_reasonable_max_length(500, 1024) == 512
    assert next_reasonable_max_length(513, 1024) == 1024


def test_tokenizer_audit_selects_untruncated_contract():
    pages = [
        {
            "page_id": "page-1",
            "session_code": "S001",
            "source_turn_id": "S001-Q001",
            "no_user_text": "页" * 600,
        }
    ]
    detail, summary, max_length = tokenizer_audit(
        MODEL_SPECS["acge"], CharacterTokenizer(), pages, ["S001-Q010"], ["问题"]
    )
    assert max_length == 1024
    assert summary["Page"]["truncated_count"] == 0
    assert summary["Query"]["truncated_count"] == 0
    assert not any(row["truncated"] for row in detail)
