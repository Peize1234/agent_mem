from exp.benchmark.run_midterm_add_prompt_conservative_candidates_ablation import (
    CONFIG_LABELS,
    INTERNAL_VARIANTS,
    NEW_VARIANTS,
    candidate_prompts,
)
from mem0.configs.midterm_prompts import MIDTERM_PAGE_SUMMARY_PROMPT


def is_line_subsequence(needle: str, haystack: str) -> bool:
    expected = iter(needle.splitlines())
    current = next(expected, None)
    for line in haystack.splitlines():
        if line == current:
            current = next(expected, None)
    return current is None


def test_candidates_only_insert_into_current_production_prompt() -> None:
    prompts = candidate_prompts()
    assert prompts["Old"] == MIDTERM_PAGE_SUMMARY_PROMPT
    assert set(prompts) == set(INTERNAL_VARIANTS)
    for variant in NEW_VARIANTS:
        assert is_line_subsequence(MIDTERM_PAGE_SUMMARY_PROMPT, prompts[variant])
        assert prompts[variant].count('"summary"') == MIDTERM_PAGE_SUMMARY_PROMPT.count('"summary"')
        assert prompts[variant].count('"keywords"') == MIDTERM_PAGE_SUMMARY_PROMPT.count('"keywords"')


def test_human_facing_configuration_labels_do_not_use_internal_ids() -> None:
    forbidden = ("A1", "A2", "A3", "P2", "E0")
    assert len(CONFIG_LABELS) == 8
    for label in CONFIG_LABELS.values():
        assert not any(token in label for token in forbidden)
