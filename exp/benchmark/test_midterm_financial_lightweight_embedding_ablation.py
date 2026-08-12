from exp.benchmark.run_midterm_financial_lightweight_embedding_ablation import (
    SPECS,
    actual_text,
    component_audit,
)


class CharacterTokenizer:
    model_max_length = 8192
    truncation_side = "right"
    padding_side = "right"

    @staticmethod
    def num_special_tokens_to_add(pair=False):
        assert pair is False
        return 2

    @staticmethod
    def encode(text, add_special_tokens=False, truncation=False):
        assert truncation is False
        return list(range(len(str(text)) + (2 if add_special_tokens else 0)))


def test_official_financial_e5_prefixes_are_kind_specific():
    spec = SPECS["balyasny"]
    assert actual_text(spec, "Query", "问题") == "query: 问题"
    assert actual_text(spec, "Page", "页面") == "passage: 页面"
    assert actual_text(SPECS["gte"], "Query", "问题") == "问题"


def test_component_audit_reports_summary_and_keyword_loss_at_native_512():
    summary = "摘" * 502
    page = {
        "page_id": "p1",
        "session_code": "S001",
        "source_turn_id": "S001-Q001",
        "summary": summary,
        "no_user_text": f"{summary}\nKeywords: 关键词",
    }
    detail, aggregate, used = component_audit(
        "model",
        CharacterTokenizer(),
        [page],
        ["S001-Q010"],
        ["问题"],
        document_prefix="passage: ",
        query_prefix="query: ",
        native_max_length=512,
        used_max_length=512,
    )
    page_row = next(row for row in detail if row["kind"] == "Page")
    assert used == 512
    assert page_row["truncated"] is True
    assert page_row["keywords_status"] == "FULLY_TRUNCATED"
    assert aggregate["Page"]["truncated_count"] == 1
