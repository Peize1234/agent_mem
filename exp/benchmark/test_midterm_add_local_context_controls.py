import math

from exp.benchmark.run_midterm_add_local_context_controls import (
    ADD_VARIANTS,
    cosine,
    load_existing_add_pages,
    summary_keywords_text,
)
from exp.benchmark.run_midterm_add_search_cross_ablation import load_pages_queries


def test_summary_keywords_formatter_is_the_production_formatter_without_user():
    assert summary_keywords_text("摘要", ["指标一", "指标二"]) == "摘要\nKeywords: 指标一, 指标二"
    assert "User:" not in summary_keywords_text("摘要", ["指标一"])


def test_cosine_control_is_scale_invariant():
    assert math.isclose(cosine([1.0, 2.0], [2.0, 4.0]), 1.0, abs_tol=1e-12)


def test_frozen_no_user_pages_change_only_the_final_user_line():
    _, old_pages, _ = load_pages_queries()
    pages = load_existing_add_pages(old_pages)

    assert all(len(pages[variant]) == 333 for variant in ADD_VARIANTS)
    for variant in ADD_VARIANTS:
        for page in pages[variant]:
            assert page["full_text"] == f"{page['no_user_text']}\nUser: {str(page['user_input']).strip()}"
