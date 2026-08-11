from exp.benchmark.run_midterm_add_context_prompt_candidates import (
    CANDIDATE_1,
    CANDIDATE_2,
    CANDIDATE_3,
    lexical_coverage,
    normalized_bigrams,
)
from exp.benchmark.run_midterm_add_local_context_ablation import add_context_instructions
from mem0.configs.midterm_prompts import MIDTERM_PAGE_SUMMARY_PROMPT


def test_candidate_prompts_only_replace_the_context_usage_instructions():
    for instructions in (CANDIDATE_1, CANDIDATE_2, CANDIDATE_3):
        prompt = add_context_instructions(MIDTERM_PAGE_SUMMARY_PROMPT, instructions)

        assert prompt.replace(f"{instructions}\n\n", "") == MIDTERM_PAGE_SUMMARY_PROMPT


def test_normalized_bigrams_ignore_spacing_and_punctuation():
    assert normalized_bigrams("判断 修订，现金质量。") == normalized_bigrams("判断修订现金质量")
    assert lexical_coverage("判断修订", "本轮进行判断修订。") == 1.0


def test_lexical_coverage_does_not_treat_an_unrelated_task_as_current_turn_focus():
    current_user = "检查哪些措辞超过事实能够支持的强度"
    following_user = "把这些内容整理成风险议题卡"
    summary_opening = "风险议题卡：整理管理层需要说明的事项"

    assert lexical_coverage(following_user, summary_opening) > lexical_coverage(current_user, summary_opening)
