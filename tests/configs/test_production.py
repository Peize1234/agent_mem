from mem0.configs.production import (
    DEEPSEEK_API_KEY_PLACEHOLDER,
    load_production_memory_config,
)


def test_repository_production_memory_config_owns_provider_and_model_choices(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")

    live = load_production_memory_config()
    provenance_safe = load_production_memory_config(resolve_environment=False)

    assert live.llm.provider == "deepseek"
    assert live.llm.config["model"] == "deepseek-v4-flash"
    assert live.llm.config["api_key"] == "test-deepseek-key"
    assert provenance_safe.llm.config["api_key"] == DEEPSEEK_API_KEY_PLACEHOLDER
    assert live.embedder.provider == "huggingface"
    assert live.embedder.config["model"] == "BAAI/bge-small-zh-v1.5"
    assert live.embedder.config["embedding_dims"] == 512
    assert live.vector_store.config.embedding_model_dims == 512
    assert live.agentic_retrieval.max_tool_result_chars == 30000


def test_repository_production_config_deep_merges_partial_overrides():
    resolved = load_production_memory_config(
        {"midterm": {"top_k_pages": 8}},
        resolve_environment=False,
    )

    assert resolved.midterm.top_k_pages == 8
    assert resolved.llm.provider == "deepseek"
    assert resolved.llm.config["model"] == "deepseek-v4-flash"
    assert resolved.embedder.provider == "huggingface"
    assert resolved.embedder.config["model"] == "BAAI/bge-small-zh-v1.5"
    assert resolved.agentic_retrieval.max_tool_result_chars == 30000
