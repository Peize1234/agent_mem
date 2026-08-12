import math

from exp.benchmark.run_midterm_field_aware_multi_vector_retrieval import (
    FIELD_WEIGHTS,
    cosine,
    field_aware_rank,
    parse_json_object,
    validate_fields,
)


def test_field_weights_are_frozen():
    assert FIELD_WEIGHTS == {"task": 0.4, "fact": 0.4, "relation": 0.2}
    assert math.isclose(sum(FIELD_WEIGHTS.values()), 1.0, abs_tol=1e-12)


def test_parse_and_validate_fields():
    value = parse_json_object('{"task":"核验", "fact":"总资产", "relation":"总资产与权益比较"}')
    assert validate_fields(value) == {
        "task": "核验",
        "fact": "总资产",
        "relation": "总资产与权益比较",
    }


def test_validate_fields_rejects_empty_field():
    try:
        validate_fields({"task": "核验", "fact": "", "relation": "比较"})
    except ValueError as exc:
        assert "fact" in str(exc)
    else:
        raise AssertionError("Expected empty fact field to fail")


def test_cosine_and_weighted_ranking():
    query_vectors = {
        "task": {"Q": [1.0, 0.0]},
        "fact": {"Q": [1.0, 0.0]},
        "relation": {"Q": [0.0, 1.0]},
    }
    page_vectors = {
        "task": {"A": [1.0, 0.0], "B": [0.0, 1.0]},
        "fact": {"A": [1.0, 0.0], "B": [0.0, 1.0]},
        "relation": {"A": [0.0, 1.0], "B": [0.0, 1.0]},
    }
    pages = [
        {"page_id": "A", "source_turn_id": "S-Q1"},
        {"page_id": "B", "source_turn_id": "S-Q2"},
    ]
    ranking = field_aware_rank("Q", pages, query_vectors, page_vectors)
    assert [row["page_id"] for row in ranking] == ["A", "B"]
    assert math.isclose(ranking[0]["task_cosine"], 1.0, abs_tol=1e-12)
    assert math.isclose(ranking[0]["fact_cosine"], 1.0, abs_tol=1e-12)
    assert math.isclose(ranking[0]["relation_cosine"], 1.0, abs_tol=1e-12)
    assert math.isclose(ranking[0]["final_score"], 1.0, abs_tol=1e-12)
    assert math.isclose(cosine([1.0, 0.0], [0.0, 1.0]), 0.0, abs_tol=1e-12)
