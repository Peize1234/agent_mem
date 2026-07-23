from mem0.configs import prompts


def test_get_update_memory_messages():
    retrieved_old_memory_dict = [{"id": "1", "text": "old memory 1"}]
    response_content = ["new fact"]
    custom_update_memory_prompt = "custom prompt determining memory update"

    ## When custom update memory prompt is provided
    ##
    result = prompts.get_update_memory_messages(
        retrieved_old_memory_dict, response_content, custom_update_memory_prompt
    )
    assert result.startswith(custom_update_memory_prompt)

    ## When custom update memory prompt is not provided
    ##
    result = prompts.get_update_memory_messages(retrieved_old_memory_dict, response_content, None)
    assert result.startswith(prompts.DEFAULT_UPDATE_MEMORY_PROMPT)


def test_get_update_memory_messages_empty_memory():
    # Test with None for retrieved_old_memory_dict
    result = prompts.get_update_memory_messages(None, ["new fact"], None)
    assert "当前记忆为空" in result

    # Test with empty list for retrieved_old_memory_dict
    result = prompts.get_update_memory_messages([], ["new fact"], None)
    assert "当前记忆为空" in result


def test_get_update_memory_messages_non_empty_memory():
    # Non-empty memory scenario
    memory_data = [{"id": "1", "text": "existing memory"}]
    result = prompts.get_update_memory_messages(memory_data, ["new fact"], None)
    # Check that the memory data is displayed
    assert str(memory_data) in result
    # And that the non-empty memory message is present
    assert "截至目前收集的记忆内容" in result


def test_additive_extraction_prompt_uses_chinese_sections_and_preserves_protocol():
    result = prompts.generate_additive_extraction_prompt(
        new_messages=[
            {
                "role": "user",
                "content": "我周末去了西湖",
                "created_at": "2026-07-20T08:30:00+00:00",
                "id": "internal-message-id",
            }
        ],
        session_summary="当前会话正在讨论周末安排",
        existing_long_term_memories=[{"id": "memory-1", "text": "用户住在杭州"}],
        existing_related_memories=[{"summary": "用户之前询问过西湖"}],
        short_term_context=[{"role": "assistant", "content": "西湖周末人流较多"}],
        custom_instructions="保留地点名称",
        use_input_language=True,
    )

    expected_sections = [
        "## 新消息",
        "## 会话摘要",
        "## 现有长期记忆",
        "## 现有相关记忆",
        "## 当前短期窗口上下文",
        "## 观察日期",
    ]
    assert all(section in result for section in expected_sections)
    assert [result.index(section) for section in expected_sections] == sorted(
        result.index(section) for section in expected_sections
    )
    assert "## 观察日期\n2026-07-20" in result
    assert "## 自定义指令\n保留地点名称" in result
    assert "## 语言要求" in result
    assert '"role": "user"' in result
    assert '"id": "memory-1"' in result
    assert "internal-message-id" not in result
    assert "## 当前日期" not in result


def test_additive_extraction_prompt_uses_declared_input_names_and_message_schema():
    result = prompts.generate_additive_extraction_prompt(
        new_messages=[{"role": "user", "content": "被淘汰消息", "created_at": "2026-07-21"}],
        short_term_context=[{"role": "user", "content": "后续消息", "created_at": "2026-07-22"}],
    )

    assert '## 新消息\n[{"role": "user", "content": "被淘汰消息"}]' in result
    assert '## 当前短期窗口上下文\n[{"role": "user", "content": "后续消息"}]' in result
    assert "## 观察日期\n2026-07-21" in result
    assert "## 摘要" not in result
    assert "## 最近 k 条消息" not in result
    assert "## 最近提取的记忆" not in result
    assert "## 现有记忆" not in result
    assert "## 后续消息" not in result


def test_additive_extraction_system_prompt_declares_current_short_term_context():
    assert "“新消息”被移出后" in prompts.ADDITIVE_EXTRACTION_PROMPT
    assert "当前短期窗口上下文不能作为独立的新记忆来源" in prompts.ADDITIVE_EXTRACTION_PROMPT


def test_chinese_prompts_preserve_json_fields_and_event_values():
    assert "只返回能被 json.loads() 解析的有效 JSON" in prompts.ADDITIVE_EXTRACTION_PROMPT
    assert '"linked_memory_ids"' in prompts.ADDITIVE_EXTRACTION_PROMPT
    assert '"attributed_to"' in prompts.ADDITIVE_EXTRACTION_PROMPT
    assert '"ADD"、"UPDATE"、"DELETE" 或 "NONE"' in prompts.get_update_memory_messages([], [], None)
