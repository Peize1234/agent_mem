from memory_monitor.components.llm_call_formatter import (
    extract_response_text,
    format_model_answer,
    format_prompt_messages,
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
