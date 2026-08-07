from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from types import SimpleNamespace

from exp.benchmark.benchmark_memory import BenchmarkObservedLLM
from mem0.configs.midterm_prompts import MIDTERM_PAGE_SUMMARY_PROMPT, MIDTERM_SESSION_MERGE_PROMPT
from mem0.configs.prompts import ADDITIVE_EXTRACTION_PROMPT


class RecordingLLM:
    config = SimpleNamespace(model="deepseek-v4-flash")

    def __init__(self):
        self.calls = []

    def generate_response(self, messages, response_format=None, **kwargs):
        self.calls.append(
            {
                "messages": messages,
                "response_format": response_format,
                **kwargs,
            }
        )
        return json.dumps({"summary": "ok", "keywords": []})


def _wrapper(delegate, context, *, observability=True, midterm_non_thinking=True, longterm_non_thinking=True):
    return BenchmarkObservedLLM(
        delegate,
        call_context=context,
        observability_enabled=observability,
        deepseek_midterm_non_thinking=midterm_non_thinking,
        deepseek_longterm_non_thinking=longterm_non_thinking,
    )


def test_midterm_calls_disable_thinking_and_log_sizes(caplog):
    caplog.set_level(logging.INFO, logger="benchmark_memory")
    delegate = RecordingLLM()
    context = ContextVar("test_llm_context", default=None)
    context.set({"source_job_id": "job-1", "stage": "midterm"})
    llm = _wrapper(delegate, context)

    for prompt in (MIDTERM_PAGE_SUMMARY_PROMPT, MIDTERM_SESSION_MERGE_PROMPT):
        llm.generate_response(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": "input"},
            ],
            response_format={"type": "json_object"},
        )

    assert len(delegate.calls) == 2
    assert all(call["extra_body"] == {"thinking": {"type": "disabled"}} for call in delegate.calls)
    assert "operation=midterm_page_summary source_job_id=job-1 stage=midterm" in caplog.text
    assert "operation=midterm_session_merge source_job_id=job-1 stage=midterm" in caplog.text
    assert "model=deepseek-v4-flash thinking=disabled" in caplog.text
    assert "prompt_chars=" in caplog.text
    assert "response_chars=" in caplog.text
    assert "elapsed_ms=" in caplog.text


def test_longterm_call_disables_thinking_and_is_observed(caplog):
    caplog.set_level(logging.INFO, logger="benchmark_memory")
    delegate = RecordingLLM()
    context = ContextVar("test_longterm_context", default=None)
    context.set({"source_job_id": "job-2", "stage": "longterm"})
    llm = _wrapper(delegate, context)

    llm.generate_response(
        [
            {"role": "system", "content": ADDITIVE_EXTRACTION_PROMPT},
            {"role": "user", "content": "input"},
        ],
        response_format={"type": "json_object"},
    )

    assert delegate.calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "operation=longterm_extraction source_job_id=job-2 stage=longterm" in caplog.text
    assert "thinking=disabled" in caplog.text


def test_unrelated_llm_call_is_unchanged_and_not_observed(caplog):
    caplog.set_level(logging.INFO, logger="benchmark_memory")
    delegate = RecordingLLM()
    context = ContextVar("test_unrelated_context", default=None)
    llm = _wrapper(delegate, context)

    llm.generate_response([{"role": "user", "content": "hello"}])

    assert "extra_body" not in delegate.calls[0]
    assert "Benchmark LLM call" not in caplog.text
