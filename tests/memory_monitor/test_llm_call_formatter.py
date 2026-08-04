import json

import pytest

from memory_monitor.components.llm_call_formatter import (
    extract_response_text,
    format_json_document,
    format_model_answer,
    format_prompt_messages,
    is_json_document,
    split_mixed_text_and_json,
)


def test_prompt_messages_keep_role_order_markdown_and_original_newlines():
    messages = [
        {"role": "system", "content": "遵循规则：\n\n- 保留列表\n- 保留 **Markdown**"},
        {"role": "user", "content": "中文问题\n```python\nprint('<safe>')\n```"},
        {"role": "assistant", "content": "先前回答"},
        {"role": "tool", "content": "检索结果\n1. 第一项\n2. 第二项"},
    ]

    formatted = format_prompt_messages(messages)

    assert [message.role for message in formatted] == ["System", "User", "Assistant", "Tool"]
    assert formatted[0].content == "遵循规则：\n\n- 保留列表\n- 保留 **Markdown**"
    assert formatted[1].content == "中文问题\n```python\nprint('<safe>')\n```"
    assert formatted[2].content == "先前回答"
    assert formatted[3].content == "检索结果\n1. 第一项\n2. 第二项"
    assert format_prompt_messages({"role": "user", "content": "single"})[0].role == "User"


def test_complete_prompt_strings_are_not_reserialized_or_entity_encoded():
    prompt = '<current_time>\n{\n  "content": "中文"\n}\n</current_time>'

    formatted = format_prompt_messages([{"role": "system", "content": prompt}])

    assert formatted[0].content == prompt
    assert "&lt;" not in formatted[0].content
    assert "&quot;" not in formatted[0].content
    assert "\\u4e2d" not in formatted[0].content


def test_structured_prompt_content_uses_readable_multiline_json():
    formatted = format_prompt_messages(
        [{"role": "user", "content": {"items": [{"content": "中文"}], "empty": [], "missing": None}}]
    )

    assert formatted[0].content == (
        '{\n  "empty": [],\n  "items": [\n    {\n      "content": "中文"\n    }\n  ],\n  "missing": null\n}'
    )


def test_prompt_messages_include_actual_assistant_tool_call_and_tool_result():
    arguments = '{"queries":["中文检索词"]}'
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "search_memory", "arguments": arguments},
                }
            ],
        },
        {
            "role": "tool",
            "name": "search_memory",
            "tool_call_id": "call-1",
            "content": '{"ok":true,"items":[]}',
        },
        {"role": "system", "content": "不得再次请求任何工具；请直接回答。"},
    ]

    formatted = format_prompt_messages(messages)

    assert [message.role for message in formatted] == ["Assistant", "Tool · search_memory", "System"]
    assert "Tool Call · search_memory" in formatted[0].content
    assert "Call ID: call-1" in formatted[0].content
    assert arguments in formatted[0].content
    assert '\\"queries\\"' not in formatted[0].content
    assert formatted[1].content == '{"ok":true,"items":[]}'


def test_structured_responses_extract_only_the_answer_text():
    cases = [
        ("plain answer", "plain answer"),
        ({"content": "content answer", "usage": {"tokens": 10}}, "content answer"),
        ({"message": {"content": "nested answer"}}, "nested answer"),
        ({"choices": [{"message": {"content": "choice answer"}}]}, "choice answer"),
        (
            {"content": [{"type": "text", "text": "第一段"}, {"type": "text", "text": "第二段"}]},
            "第一段\n\n第二段",
        ),
        (
            {"output": [{"content": [{"type": "output_text", "text": "responses answer"}]}]},
            "responses answer",
        ),
    ]

    for response, expected in cases:
        assert extract_response_text(response) == expected

    assert extract_response_text({"content": None, "tool_calls": [{"id": "call-1"}]}) == "（模型未返回文本回答）"
    assert (
        extract_response_text({"content": [{"type": "tool_use", "name": "search", "input": {"query": "x"}}]})
        == "（模型未返回文本回答）"
    )
    assert format_model_answer({"response": None, "status": "failed"}) == "（调用失败，未返回文本回答）"


def test_unknown_structured_response_uses_safe_unicode_fallback():
    rendered = extract_response_text({"payload": {"value": "中文", "count": 2}, "usage": {"tokens": 9}})

    assert '"payload"' in rendered
    assert "中文" in rendered
    assert '"usage"' not in rendered
    assert "\\u" not in rendered


@pytest.mark.parametrize(
    "response",
    [
        '{"summary":"测试","keywords":["A","B"]}',
        '{\n  "summary": "测试",\n  "keywords": [\n    "A",\n    "B"\n  ]\n}',
        '```json\n{"summary":"测试","keywords":["A","B"]}\n```',
        '```JSON\r\n[{"nested":{"中文":[1,2]}}]\r\n```',
    ],
)
def test_complete_json_model_answers_are_pretty_printed(response):
    parsed_text = response.strip()
    if parsed_text.lower().startswith("```json"):
        parsed_text = parsed_text.splitlines()[1:-1]
        parsed_text = "\n".join(parsed_text)
    expected = json.dumps(json.loads(parsed_text), ensure_ascii=False, indent=2)

    rendered = extract_response_text(response)

    assert rendered == expected
    assert "\n" in rendered
    assert "\\u" not in rendered
    assert is_json_document(rendered)


@pytest.mark.parametrize(
    "response",
    [
        "建议保持当前方案，不需要调整。",
        "普通文本中包含 {少量花括号}，不应解析。",
        '{"summary":"缺少右括号"',
        "```json\nnot-json\n```",
    ],
)
def test_non_json_model_answers_remain_text(response):
    assert extract_response_text(response) == response.strip()
    assert not is_json_document(response)


@pytest.mark.parametrize(
    "response",
    [
        {"summary": "中文摘要", "keywords": ["甲", "乙"]},
        [{"summary": "数组回答", "nested": {"values": [1, 2]}}],
        ["甲", "乙"],
    ],
)
def test_direct_provider_objects_and_arrays_use_the_same_pretty_json_rule(response):
    assert extract_response_text(response) == json.dumps(response, ensure_ascii=False, indent=2)


def test_provider_text_part_arrays_still_extract_plain_text():
    response = [
        {"type": "text", "text": "第一段"},
        {"type": "output_text", "text": "第二段"},
    ]

    assert extract_response_text(response) == "第一段\n\n第二段"


@pytest.mark.parametrize(
    ("document", "expected_fragment"),
    [
        ('{"summary":"中文","items":[1,2]}', '  "summary": "中文"'),
        ('[{"nested":{"items":[1,2]}}]', '    "nested": {'),
    ],
)
def test_complete_json_documents_are_formatted_without_mutating_input(document, expected_fragment):
    original = document[:]

    formatted = format_json_document(document)

    assert formatted is not None
    assert expected_fragment in formatted
    assert formatted == json.dumps(json.loads(document), ensure_ascii=False, indent=2)
    assert document == original


def test_mixed_text_and_xml_wrapped_json_are_split_in_original_order():
    content = (
        "短期记忆是当前 Session 最近若干轮完整对话。\n\n"
        "<short_term_memory>\n"
        '[{"content":"用户问题","created_at":"2026-08-03T09:01:56+08:00","role":"user"}]\n'
        "</short_term_memory>"
    )

    parts = split_mixed_text_and_json(content)

    assert [part.is_json for part in parts] == [False, True, False]
    assert parts[0].content.endswith("<short_term_memory>\n")
    assert json.loads(parts[1].content)[0]["content"] == "用户问题"
    assert parts[2].content == "\n</short_term_memory>"
    assert "".join(part.content for part in parts) == content


def test_multiple_nested_json_blocks_keep_text_and_brackets_inside_strings():
    first = {
        "profile": {
            "description": '中文换行\n带有转义引号 "以及 {对象}、[数组] 和 <tag> 文本',
            "items": [{"values": [1, 2]}],
        }
    }
    second = [{"memory": "长期事实"}, {"memory": "中期摘要"}]
    first_json = json.dumps(first, ensure_ascii=False, separators=(",", ":"))
    second_json = json.dumps(second, ensure_ascii=False, separators=(",", ":"))
    content = f"用户画像：\n{first_json}\n参考信息：\n{second_json}\n结束。"

    parts = split_mixed_text_and_json(content)
    json_parts = [part.content for part in parts if part.is_json]

    assert json_parts == [first_json, second_json]
    assert [json.loads(part) for part in json_parts] == [first, second]
    assert "参考信息：" in parts[2].content
    assert "".join(part.content for part in parts) == content


@pytest.mark.parametrize(
    "content",
    [
        '说明\n{"outer":{"valid":true}\n后续文字',
        "普通文本中包含 {少量花括号}，以及版本号[1,2]，都不是独立 JSON 文档。",
        '相邻普通文本不能被截断：prefix{"valid":true}suffix',
    ],
)
def test_invalid_or_non_document_braces_remain_plain_text(content):
    parts = split_mixed_text_and_json(content)

    assert len(parts) == 1
    assert parts[0].content == content
    assert not parts[0].is_json


@pytest.mark.parametrize("fence", ["```json", "```", "~~~json"])
def test_fenced_code_is_not_split_or_wrapped_again(fence):
    content = f'说明\n{fence}\n{{"inside":[1,{{"nested":true}}]}}\n{fence[:3]}\n结束'

    parts = split_mixed_text_and_json(content)

    assert len(parts) == 1
    assert parts[0].content == content
    assert not parts[0].is_json
