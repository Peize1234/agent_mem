from exp.benchmark.run_midterm_rerank_tuning import (
    expanded_union,
    fixed_budget_union,
    movement_category,
)


def _ranking(ids: list[str]) -> list[dict[str, object]]:
    return [
        {"page_id": page_id, "source_turn_id": page_id, "score": float(len(ids) - index), "rank": index + 1}
        for index, page_id in enumerate(ids)
    ]


def test_fixed_budget_union_honors_final_deduplicated_budget() -> None:
    dense = _ranking(["A", "B", "C", "D", "E", "F"])
    bm25 = _ranking(["A", "D", "E", "G", "H", "I"])

    ranking, actual = fixed_budget_union(dense, bm25, 6)

    assert actual == 6
    assert len({row["page_id"] for row in ranking[:actual]}) == 6
    assert [row["page_id"] for row in ranking[:3]] == ["A", "B", "D"]


def test_expanded_union_reports_actual_pool_larger_than_per_path_k() -> None:
    dense = _ranking(["A", "B", "C", "D"])
    bm25 = _ranking(["D", "E", "F", "G"])

    ranking, actual = expanded_union(dense, bm25, 3)

    assert actual == 6
    assert {row["page_id"] for row in ranking[:actual]} == {"A", "B", "C", "D", "E", "F"}


def test_movement_categories_track_top5_boundary() -> None:
    assert movement_category(8, 4) == "PROMOTED_INTO_TOP5"
    assert movement_category(8, 6) == "PROMOTED_BUT_STILL_MISS"
    assert movement_category(4, 8) == "DEMOTED_OUT_OF_TOP5"
    assert movement_category(2, 4) == "DEMOTED_BUT_STILL_HIT"
    assert movement_category(8, 10) == "UNCHANGED"
