import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from mem0.configs.llms.base import BaseLlmConfig
from mem0.configs.llms.deepseek import DeepSeekConfig
from mem0.llms.base import LLMResponse
from mem0.llms.deepseek import DeepSeekLLM


@pytest.fixture
def mock_deepseek_client():
    with patch("mem0.llms.deepseek.OpenAI") as mock_openai:
        mock_client = Mock()
        mock_openai.return_value = mock_client
        yield mock_client


def test_deepseek_llm_base_url():
    # case1: default config with deepseek official base url
    config = BaseLlmConfig(model="deepseek-chat", temperature=0.7, max_tokens=100, top_p=1.0, api_key="api_key")
    llm = DeepSeekLLM(config)
    assert str(llm.client.base_url) == "https://api.deepseek.com"

    # case2: with env variable DEEPSEEK_API_BASE
    provider_base_url = "https://api.provider.com/v1/"
    os.environ["DEEPSEEK_API_BASE"] = provider_base_url
    config = DeepSeekConfig(model="deepseek-chat", temperature=0.7, max_tokens=100, top_p=1.0, api_key="api_key")
    llm = DeepSeekLLM(config)
    assert str(llm.client.base_url) == provider_base_url

    # case3: with config.deepseek_base_url
    config_base_url = "https://api.config.com/v1/"
    config = DeepSeekConfig(
        model="deepseek-chat",
        temperature=0.7,
        max_tokens=100,
        top_p=1.0,
        api_key="api_key",
        deepseek_base_url=config_base_url,
    )
    llm = DeepSeekLLM(config)
    assert str(llm.client.base_url) == config_base_url


def test_generate_response_without_tools(mock_deepseek_client):
    config = BaseLlmConfig(model="deepseek-chat", temperature=0.7, max_tokens=100, top_p=1.0)
    llm = DeepSeekLLM(config)
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello, how are you?"},
    ]

    mock_response = Mock()
    mock_response.choices = [Mock(message=Mock(content="I'm doing well, thank you for asking!"))]
    mock_deepseek_client.chat.completions.create.return_value = mock_response

    response = llm.generate_response(messages)

    mock_deepseek_client.chat.completions.create.assert_called_once_with(
        model="deepseek-chat", messages=messages, temperature=0.7, max_tokens=100, top_p=1.0
    )
    assert response == "I'm doing well, thank you for asking!"


def test_generate_response_with_tools(mock_deepseek_client):
    config = BaseLlmConfig(model="deepseek-chat", temperature=0.7, max_tokens=100, top_p=1.0)
    llm = DeepSeekLLM(config)
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Add a new memory: Today is a sunny day."},
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "add_memory",
                "description": "Add a memory",
                "parameters": {
                    "type": "object",
                    "properties": {"data": {"type": "string", "description": "Data to add to memory"}},
                    "required": ["data"],
                },
            },
        }
    ]

    mock_response = Mock()
    mock_message = Mock()
    mock_message.content = "I've added the memory for you."

    mock_tool_call = Mock()
    mock_tool_call.id = "call-deepseek-1"
    mock_tool_call.function.name = "add_memory"
    mock_tool_call.function.arguments = '{"data": "Today is a sunny day."}'

    mock_message.tool_calls = [mock_tool_call]
    mock_response.choices = [Mock(message=mock_message)]
    mock_deepseek_client.chat.completions.create.return_value = mock_response

    response = llm.generate_response(messages, tools=tools)

    mock_deepseek_client.chat.completions.create.assert_called_once_with(
        model="deepseek-chat",
        messages=messages,
        temperature=0.7,
        max_tokens=100,
        top_p=1.0,
        tools=tools,
        tool_choice="auto",
    )

    assert response["content"] == "I've added the memory for you."
    assert len(response["tool_calls"]) == 1
    assert response["tool_calls"][0]["id"] == "call-deepseek-1"
    assert response["tool_calls"][0]["name"] == "add_memory"
    assert response["tool_calls"][0]["arguments"] == {"data": "Today is a sunny day."}


def test_malformed_tool_arguments_preserve_call_id_and_do_not_raise(mock_deepseek_client):
    llm = DeepSeekLLM(BaseLlmConfig(model="deepseek-chat", max_tokens=100))
    mock_tool_call = Mock()
    mock_tool_call.id = "call-bad-json"
    mock_tool_call.function.name = "search_memory"
    mock_tool_call.function.arguments = '{"query":'
    mock_message = Mock(content=None, tool_calls=[mock_tool_call])
    mock_deepseek_client.chat.completions.create.return_value = Mock(choices=[Mock(message=mock_message)])

    response = llm.generate_response(
        [{"role": "user", "content": "search"}],
        tools=[{"type": "function", "function": {"name": "search_memory"}}],
    )

    assert response["tool_calls"][0]["id"] == "call-bad-json"
    assert response["tool_calls"][0]["arguments"] == '{"query":'
    assert "invalid tool arguments JSON" in response["tool_calls"][0]["arguments_error"]


def test_generate_response_with_response_format(mock_deepseek_client):
    config = BaseLlmConfig(model="deepseek-chat", temperature=0.7, max_tokens=100, top_p=1.0)
    llm = DeepSeekLLM(config)
    messages = [
        {"role": "system", "content": "You are a memory extraction assistant."},
        {"role": "user", "content": "I like hiking on weekends."},
    ]

    mock_response = Mock()
    mock_response.choices = [Mock(message=Mock(content='{"facts": ["User likes hiking on weekends"]}'))]
    mock_deepseek_client.chat.completions.create.return_value = mock_response

    response = llm.generate_response(messages, response_format={"type": "json_object"})

    mock_deepseek_client.chat.completions.create.assert_called_once_with(
        model="deepseek-chat",
        messages=messages,
        temperature=0.7,
        max_tokens=100,
        top_p=1.0,
        response_format={"type": "json_object"},
    )
    assert response == '{"facts": ["User likes hiking on weekends"]}'


def test_generate_response_without_response_format(mock_deepseek_client):
    config = BaseLlmConfig(model="deepseek-chat", temperature=0.7, max_tokens=100, top_p=1.0)
    llm = DeepSeekLLM(config)
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Tell me a joke."},
    ]

    mock_response = Mock()
    mock_response.choices = [Mock(message=Mock(content="Why did the chicken cross the road?"))]
    mock_deepseek_client.chat.completions.create.return_value = mock_response

    response = llm.generate_response(messages)

    call_kwargs = mock_deepseek_client.chat.completions.create.call_args[1]
    assert "response_format" not in call_kwargs
    assert response == "Why did the chicken cross the road?"


def test_profile_response_forwards_enabled_thinking_and_returns_safe_metadata(mock_deepseek_client):
    config = BaseLlmConfig(model="deepseek-v4-flash", temperature=0.7, max_tokens=100, top_p=1.0)
    llm = DeepSeekLLM(config)
    messages = [{"role": "user", "content": "Extract a profile update."}]
    message = SimpleNamespace(
        content='{"operations":[],"unmapped_facts":[]}',
        reasoning_content="private chain of thought " * 1000,
        tool_calls=None,
    )
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=SimpleNamespace(
            prompt_tokens=321,
            completion_tokens=456,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=400),
        ),
        model="deepseek-v4-flash",
    )
    mock_deepseek_client.chat.completions.create.return_value = response

    result = llm.generate_response(
        messages,
        response_format={"type": "json_object"},
        max_tokens=4096,
        extra_body={"thinking": {"type": "enabled"}},
        _return_metadata=True,
    )

    assert result == LLMResponse(
        content='{"operations":[],"unmapped_facts":[]}',
        finish_reason="stop",
        prompt_tokens=321,
        completion_tokens=456,
        reasoning_tokens=400,
        model="deepseek-v4-flash",
    )
    call_kwargs = mock_deepseek_client.chat.completions.create.call_args.kwargs
    assert call_kwargs["model"] == "deepseek-v4-flash"
    assert call_kwargs["max_tokens"] == 4096
    assert call_kwargs["response_format"] == {"type": "json_object"}
    assert call_kwargs["extra_body"] == {"thinking": {"type": "enabled"}}
    assert "_return_metadata" not in call_kwargs
    assert "private chain of thought" not in repr(result)
