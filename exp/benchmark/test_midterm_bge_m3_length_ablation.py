from exp.benchmark.run_midterm_bge_m3_length_ablation import (
    LABELS,
    MAX_LENGTHS,
    component_token_audit,
    truncated_gold_analysis,
)


class CharacterTokenizer:
    model_max_length = 8192
    truncation_side = "right"

    @staticmethod
    def num_special_tokens_to_add(pair=False):
        assert pair is False
        return 2

    @staticmethod
    def encode(text, add_special_tokens=False):
        assert add_special_tokens is False
        return [ord(value) for value in str(text) if value != "\n"]


def test_configured_lengths_are_first_class_ablation_values():
    assert MAX_LENGTHS == {"Small512": 512, "M3_512": 512, "M3_1024": 1024, "M3_2048": 2048}


def test_component_token_audit_distinguishes_summary_and_keyword_truncation():
    summary = "摘" * 500
    keywords = ["关" * 20]
    page = {
        "session_code": "S001",
        "source_turn_id": "S001-Q001",
        "page_id": "page-1",
        "summary": summary,
        "keywords": keywords,
        "no_user_text": f"{summary}\nKeywords: {keywords[0]}",
    }

    rows, aggregate = component_token_audit([page], CharacterTokenizer(), config="M3_512")

    assert rows[0]["summary_truncated"] is False
    assert rows[0]["keywords_truncation_status"] == "PARTIALLY_TRUNCATED"
    assert aggregate["page_truncated_count"] == 1
    assert aggregate["keywords_partially_truncated_count"] == 1


def test_truncated_gold_analysis_separates_originally_truncated_gold():
    rows = [
        {
            "comparison": "model",
            "gold_page_id": "page-1",
            "before_rank": 8,
            "after_rank": 3,
            "rank_improvement": 5,
        },
        {
            "comparison": "model",
            "gold_page_id": "page-2",
            "before_rank": 2,
            "after_rank": 9,
            "rank_improvement": -7,
        },
    ]

    detail, summary = truncated_gold_analysis(
        rows,
        small_tokens={"page-1": 600, "page-2": 400},
        m3_tokens={"page-1": 450, "page-2": 350},
    )

    assert detail[0]["small_over_512"] is True
    truncated = next(row for row in summary if row["cohort"] == "BGE-small tokenizer >512")
    assert truncated["promoted_gold"] == 1
    assert truncated["demoted_gold"] == 0
    assert LABELS["M3_512"].startswith("BAAI/bge-m3")
