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
        summary="用户喜欢徒步",
        existing_memories=[{"id": "memory-1", "text": "用户住在杭州"}],
        new_messages=[{"role": "user", "content": "我周末去了西湖"}],
        current_date="2026-07-21",
        timestamp="2026-07-20",
        custom_instructions="保留地点名称",
        use_input_language=True,
    )

    assert "## 摘要" in result
    assert "## 新消息" in result
    assert "## 观察日期\n2026-07-20" in result
    assert "## 当前日期\n2026-07-21" in result
    assert "## 自定义指令\n保留地点名称" in result
    assert "## 语言要求" in result
    assert '"role": "user"' in result
    assert '"id": "memory-1"' in result


def test_chinese_prompts_preserve_json_fields_and_event_values():
    assert "只返回能被 json.loads() 解析的有效 JSON" in prompts.ADDITIVE_EXTRACTION_PROMPT
    assert '"linked_memory_ids"' in prompts.ADDITIVE_EXTRACTION_PROMPT
    assert '"attributed_to"' in prompts.ADDITIVE_EXTRACTION_PROMPT
    assert '"ADD"、"UPDATE"、"DELETE" 或 "NONE"' in prompts.get_update_memory_messages([], [], None)
