import math

from exp.benchmark.run_midterm_field_aware_v2_unified_extraction import (
    ADD_FIELD_SECTION,
    FIELD_WEIGHTS,
    SEARCH_FIELD_SECTION,
    build_add_prompt,
    build_search_prompt,
    cache_identity,
    char_ngrams,
    counter_cosine,
    field_score,
    validate_add_output,
    validate_search_output,
)


def test_add_prompt_preserves_base_and_adds_unified_schema():
    base = (
        '输出结构：\n\n{\n  "summary": "对本轮对话的自包含摘要",\n  "keywords": ["关键词1", "关键词2"]\n}'
        "\n\n## summary 要求\n原规则\n\n"
        '如果本轮对话没有任何值得保留的分析信息，返回：\n\n{\n  "summary": "",\n  "keywords": []\n}'
    )
    prompt = build_add_prompt(base)
    assert "原规则" in prompt
    assert ADD_FIELD_SECTION in prompt
    assert '"task": "当前轮的任务身份"' in prompt


def test_search_prompt_preserves_p2_and_locks_resolved_query():
    base = 'P2 全部规则\n\n严格只返回：\n\n{"resolved_query":"补全后的问题"}'
    prompt = build_search_prompt(base)
    assert prompt.startswith("P2 全部规则")
    assert SEARCH_FIELD_SECTION in prompt
    assert "Task / Fact / Relation 绝对不能反向影响" in prompt


def test_strict_output_schema_and_empty_fields():
    add = validate_add_output(
        {"summary": "摘要", "keywords": ["关键词"], "task": "任务", "fact": "", "relation": "关系"}
    )
    assert add["fact"] == ""
    search = validate_search_output({"resolved_query": "问题", "task": "任务", "fact": "", "relation": "关系"})
    assert search["resolved_query"] == "问题"
    try:
        validate_search_output({"resolved_query": "问题", "task": "任务", "fact": ""})
    except ValueError as exc:
        assert "exactly" in str(exc)
    else:
        raise AssertionError("Schema with missing relation must fail")


def test_cache_key_contract_is_deterministic_and_config_sensitive():
    kwargs = {
        "side": "Search",
        "item_id": "S001-Q001",
        "prompt_hash": "prompt",
        "input_hash": "input",
        "model": "deepseek-v4-flash",
        "temperature": 0.0,
        "top_p": None,
        "top_k": None,
        "extra": {"previous_context_count": 3},
    }
    first = cache_identity(**kwargs)
    second = cache_identity(**kwargs)
    assert first == second
    assert first["prompt_sha256"] == "prompt"
    assert first["input_sha256"] == "input"
    changed = cache_identity(**{**kwargs, "temperature": 0.1})
    assert changed != first


def test_field_score_renormalizes_nonempty_pairs():
    query_vectors = {
        "task": {"Q": [1.0, 0.0]},
        "fact": {},
        "relation": {"Q": [0.0, 1.0]},
    }
    page_vectors = {
        "task": {"P": [1.0, 0.0]},
        "fact": {"P": [1.0, 0.0]},
        "relation": {"P": [0.0, 1.0]},
    }
    final, scores, valid = field_score("Q", "P", query_vectors, page_vectors)
    assert valid == ["task", "relation"]
    assert math.isclose(final or 0.0, 1.0, abs_tol=1e-12)
    assert scores["fact"] is None
    assert math.isclose(FIELD_WEIGHTS["task"] + FIELD_WEIGHTS["fact"] + FIELD_WEIGHTS["relation"], 1.0)


def test_char_ngram_similarity_identical_and_disjoint():
    assert math.isclose(counter_cosine(char_ngrams("证据分级"), char_ngrams("证据分级")), 1.0)
    assert math.isclose(counter_cosine(char_ngrams("证据分级"), char_ngrams("现金流量")), 0.0)
