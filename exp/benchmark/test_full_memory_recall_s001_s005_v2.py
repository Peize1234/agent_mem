from pathlib import Path

import pytest

from exp.benchmark.benchmark_common import load_json
from exp.benchmark.memory_gold_groups import parse_gold_requirements
from exp.benchmark.run_full_memory_recall_s001_s005_v2 import (
    DEFAULT_CONFIG,
    DatasetTurn,
    load_v3_dataset,
    static_dataset_stats,
    validate_dataset_order,
)


def test_independent_gold_requirements() -> None:
    groups = parse_gold_requirements("S001-Q001；S001-Q002")
    assert [group.members for group in groups] == [("S001-Q001",), ("S001-Q002",)]


def test_parenthesized_or_is_one_denominator_unit() -> None:
    groups = parse_gold_requirements("（S001-Q001；S001-Q002）")
    assert len(groups) == 1
    assert groups[0].members == ("S001-Q001", "S001-Q002")
    assert groups[0].is_or
    assert groups[0].hit_by(["S001-Q002"])


def test_parenthesized_or_plus_independent_gold() -> None:
    groups = parse_gold_requirements("（S001-Q001；S001-Q002）；S001-Q003")
    assert [group.members for group in groups] == [
        ("S001-Q001", "S001-Q002"),
        ("S001-Q003",),
    ]


def test_unbalanced_parentheses_are_rejected() -> None:
    with pytest.raises(ValueError, match="括号"):
        parse_gold_requirements("（S001-Q001；S001-Q002")


def test_future_gold_is_rejected() -> None:
    sessions = {
        "S001": [
            DatasetTurn("S001", "S001_test", 0, "S001-Q001", "q1", "a1", "", ()),
            DatasetTurn(
                "S001",
                "S001_test",
                1,
                "S001-Q002",
                "q2",
                "a2",
                "S001-Q003",
                parse_gold_requirements("S001-Q003"),
            ),
            DatasetTurn("S001", "S001_test", 2, "S001-Q003", "q3", "a3", "", ()),
        ]
    }
    with pytest.raises(ValueError, match="当前或未来"):
        validate_dataset_order(sessions)


def test_v3_static_sanity_and_or_shortterm_semantics() -> None:
    config = load_json(DEFAULT_CONFIG)
    dataset = Path(config["dataset"])
    if not dataset.is_absolute():
        dataset = DEFAULT_CONFIG.parents[2] / dataset
    sessions = load_v3_dataset(dataset.resolve(), config["sessions"])
    stats = static_dataset_stats(sessions, 3, 3)
    assert stats["session_count"] == 5
    assert stats["query_count"] == 348
    assert stats["gold_requirement_count"] == 559
    assert stats["or_requirement_count"] == 5
    assert stats["shortterm_requirement_count"] == 410
    assert stats["outside_shortterm_requirement_count"] == 149


def test_default_midterm_configuration_is_c3() -> None:
    config = load_json(DEFAULT_CONFIG)
    assert config["shortterm"]["qa_turns"] == 3
    assert config["shortterm"]["top_k"] == 3
    assert config["midterm"]["embedding_model"] == "BAAI/bge-small-zh-v1.5"
    assert config["midterm"]["top_k"] == [5, 10, 20]
    assert config["midterm"]["configuration"].startswith("C3:")
    assert "Summary + Keywords" in config["midterm"]["configuration"]
