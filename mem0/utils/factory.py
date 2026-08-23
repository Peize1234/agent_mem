import importlib
import inspect
import logging
from typing import Any, Dict, Optional, Union

from mem0.configs.embeddings.base import BaseEmbedderConfig
from mem0.configs.llms.anthropic import AnthropicConfig
from mem0.configs.llms.aws_bedrock import AWSBedrockConfig
from mem0.configs.llms.azure import AzureOpenAIConfig
from mem0.configs.llms.base import BaseLlmConfig
from mem0.configs.llms.deepseek import DeepSeekConfig
from mem0.configs.llms.gemini import GeminiConfig
from mem0.configs.llms.lmstudio import LMStudioConfig
from mem0.configs.llms.minimax import MinimaxConfig
from mem0.configs.llms.ollama import OllamaConfig
from mem0.configs.llms.openai import OpenAIConfig
from mem0.configs.llms.vllm import VllmConfig
from mem0.configs.llms.xai import XAIConfig
from mem0.configs.rerankers.base import BaseRerankerConfig
from mem0.configs.rerankers.cohere import CohereRerankerConfig
from mem0.configs.rerankers.huggingface import HuggingFaceRerankerConfig
from mem0.configs.rerankers.llm import LLMRerankerConfig
from mem0.configs.rerankers.sentence_transformer import SentenceTransformerRerankerConfig
from mem0.configs.rerankers.zero_entropy import ZeroEntropyRerankerConfig
from mem0.embeddings.encoding_contract import EncodingContractEmbedding
from mem0.embeddings.mock import MockEmbeddings

logger = logging.getLogger(__name__)


def _configure_native_timeout(instance: Any, timeout_seconds: Optional[float]) -> Any:
    """Forward a request timeout to provider-native clients when available."""
    if timeout_seconds is None:
        return instance
    timeout_seconds = float(timeout_seconds)
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than 0")

    instance.request_timeout_seconds = timeout_seconds
    config = getattr(instance, "config", None)
    if config is not None:
        try:
            config.request_timeout_seconds = timeout_seconds
        except Exception:
            logger.debug("Provider config does not expose request_timeout_seconds", exc_info=True)
        http_client = getattr(config, "http_client", None)
        if http_client is not None:
            try:
                http_client.timeout = timeout_seconds
            except Exception:
                logger.debug("Provider HTTP client timeout could not be configured", exc_info=True)

    client = getattr(instance, "client", None)
    if client is None:
        return instance
    with_options = getattr(client, "with_options", None)
    if callable(with_options):
        try:
            configured_client = with_options(timeout=timeout_seconds)
            if configured_client is not None:
                instance.client = configured_client
                client = configured_client
        except Exception:
            logger.debug("Provider client with_options(timeout=...) is unavailable", exc_info=True)
    for attribute in ("timeout", "request_timeout"):
        if hasattr(client, attribute):
            try:
                setattr(client, attribute, timeout_seconds)
                break
            except Exception:
                logger.debug("Provider client %s could not be configured", attribute, exc_info=True)
    return instance


def load_class(class_type):
    module_path, class_name = class_type.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


class LlmFactory:
    """
    Factory for creating LLM instances with appropriate configurations.
    Supports both old-style BaseLlmConfig and new provider-specific configs.
    """

    # Provider mappings with their config classes
    provider_to_class = {
        "ollama": ("mem0.llms.ollama.OllamaLLM", OllamaConfig),
        "openai": ("mem0.llms.openai.OpenAILLM", OpenAIConfig),
        "groq": ("mem0.llms.groq.GroqLLM", BaseLlmConfig),
        "together": ("mem0.llms.together.TogetherLLM", BaseLlmConfig),
        "aws_bedrock": ("mem0.llms.aws_bedrock.AWSBedrockLLM", AWSBedrockConfig),
        "litellm": ("mem0.llms.litellm.LiteLLM", BaseLlmConfig),
        "azure_openai": ("mem0.llms.azure_openai.AzureOpenAILLM", AzureOpenAIConfig),
        "openai_structured": ("mem0.llms.openai_structured.OpenAIStructuredLLM", OpenAIConfig),
        "anthropic": ("mem0.llms.anthropic.AnthropicLLM", AnthropicConfig),
        "azure_openai_structured": ("mem0.llms.azure_openai_structured.AzureOpenAIStructuredLLM", AzureOpenAIConfig),
        "gemini": ("mem0.llms.gemini.GeminiLLM", GeminiConfig),
        "deepseek": ("mem0.llms.deepseek.DeepSeekLLM", DeepSeekConfig),
        "minimax": ("mem0.llms.minimax.MiniMaxLLM", MinimaxConfig),
        "xai": ("mem0.llms.xai.XAILLM", XAIConfig),
        "sarvam": ("mem0.llms.sarvam.SarvamLLM", BaseLlmConfig),
        "lmstudio": ("mem0.llms.lmstudio.LMStudioLLM", LMStudioConfig),
        "vllm": ("mem0.llms.vllm.VllmLLM", VllmConfig),
        "langchain": ("mem0.llms.langchain.LangchainLLM", BaseLlmConfig),
    }

    @classmethod
    def create(
        cls,
        provider_name: str,
        config: Optional[Union[BaseLlmConfig, Dict]] = None,
        *,
        timeout_seconds: Optional[float] = None,
        **kwargs,
    ):
        """
        Create an LLM instance with the appropriate configuration.

        Args:
            provider_name (str): The provider name (e.g., 'openai', 'anthropic')
            config: Configuration object or dict. If None, will create default config
            **kwargs: Additional configuration parameters

        Returns:
            Configured LLM instance

        Raises:
            ValueError: If provider is not supported
        """
        if provider_name not in cls.provider_to_class:
            raise ValueError(f"Unsupported Llm provider: {provider_name}")

        class_type, config_class = cls.provider_to_class[provider_name]
        llm_class = load_class(class_type)

        # Handle configuration
        if config is None:
            # Create default config with kwargs
            config = config_class(**kwargs)
        elif isinstance(config, dict):
            # Merge dict config with kwargs
            config.update(kwargs)
            config = config_class(**config)
        elif isinstance(config, BaseLlmConfig):
            # Convert base config to provider-specific config if needed
            if config_class != BaseLlmConfig:
                # Convert to provider-specific config
                config_dict = {
                    "model": config.model,
                    "temperature": config.temperature,
                    "api_key": config.api_key,
                    "max_tokens": config.max_tokens,
                    "top_p": config.top_p,
                    "top_k": config.top_k,
                    "enable_vision": config.enable_vision,
                    "vision_details": config.vision_details,
                    "http_client_proxies": config.http_client_proxies,
                }
                # Only forward reasoning fields to provider configs that accept them
                # (explicitly or via **kwargs); others would raise on unexpected kwargs.
                params = inspect.signature(config_class).parameters
                accepts_kwargs = any(p.kind == p.VAR_KEYWORD for p in params.values())
                if accepts_kwargs or "reasoning_effort" in params:
                    config_dict["reasoning_effort"] = config.reasoning_effort
                if accepts_kwargs or "is_reasoning_model" in params:
                    config_dict["is_reasoning_model"] = config.is_reasoning_model
                config_dict.update(kwargs)
                config = config_class(**config_dict)
            else:
                # Use base config as-is
                pass
        else:
            # Assume it's already the correct config type
            pass

        return _configure_native_timeout(llm_class(config), timeout_seconds)

    @classmethod
    def register_provider(cls, name: str, class_path: str, config_class=None):
        """
        Register a new provider.

        Args:
            name (str): Provider name
            class_path (str): Full path to LLM class
            config_class: Configuration class for the provider (defaults to BaseLlmConfig)
        """
        if config_class is None:
            config_class = BaseLlmConfig
        cls.provider_to_class[name] = (class_path, config_class)

    @classmethod
    def get_supported_providers(cls) -> list:
        """
        Get list of supported providers.

        Returns:
            list: List of supported provider names
        """
        return list(cls.provider_to_class.keys())


class EmbedderFactory:
    provider_to_class = {
        "openai": "mem0.embeddings.openai.OpenAIEmbedding",
        "ollama": "mem0.embeddings.ollama.OllamaEmbedding",
        "huggingface": "mem0.embeddings.huggingface.HuggingFaceEmbedding",
        "azure_openai": "mem0.embeddings.azure_openai.AzureOpenAIEmbedding",
        "gemini": "mem0.embeddings.gemini.GoogleGenAIEmbedding",
        "vertexai": "mem0.embeddings.vertexai.VertexAIEmbedding",
        "together": "mem0.embeddings.together.TogetherEmbedding",
        "lmstudio": "mem0.embeddings.lmstudio.LMStudioEmbedding",
        "langchain": "mem0.embeddings.langchain.LangchainEmbedding",
        "aws_bedrock": "mem0.embeddings.aws_bedrock.AWSBedrockEmbedding",
        "fastembed": "mem0.embeddings.fastembed.FastEmbedEmbedding",
    }

    @classmethod
    def create(
        cls,
        provider_name,
        config,
        vector_config: Optional[dict],
        *,
        timeout_seconds: Optional[float] = None,
    ):
        if provider_name == "upstash_vector" and vector_config and vector_config.enable_embeddings:
            return _configure_native_timeout(MockEmbeddings(), timeout_seconds)
        class_type = cls.provider_to_class.get(provider_name)
        if class_type:
            embedder_instance = load_class(class_type)
            base_config = BaseEmbedderConfig(**config)
            instance = _configure_native_timeout(embedder_instance(base_config), timeout_seconds)
            if base_config.encoding_contract:
                instance = EncodingContractEmbedding(instance, base_config.encoding_contract)
            return instance
        else:
            raise ValueError(f"Unsupported Embedder provider: {provider_name}")


class VectorStoreFactory:
    provider_to_class = {
        "qdrant": "mem0.vector_stores.qdrant.Qdrant",
        "chroma": "mem0.vector_stores.chroma.ChromaDB",
        "pgvector": "mem0.vector_stores.pgvector.PGVector",
        "milvus": "mem0.vector_stores.milvus.MilvusDB",
        "upstash_vector": "mem0.vector_stores.upstash_vector.UpstashVector",
        "azure_ai_search": "mem0.vector_stores.azure_ai_search.AzureAISearch",
        "azure_mysql": "mem0.vector_stores.azure_mysql.AzureMySQL",
        "pinecone": "mem0.vector_stores.pinecone.PineconeDB",
        "mongodb": "mem0.vector_stores.mongodb.MongoDB",
        "redis": "mem0.vector_stores.redis.RedisDB",
        "valkey": "mem0.vector_stores.valkey.ValkeyDB",
        "databricks": "mem0.vector_stores.databricks.Databricks",
        "elasticsearch": "mem0.vector_stores.elasticsearch.ElasticsearchDB",
        "vertex_ai_vector_search": "mem0.vector_stores.vertex_ai_vector_search.GoogleMatchingEngine",
        "opensearch": "mem0.vector_stores.opensearch.OpenSearchDB",
        "supabase": "mem0.vector_stores.supabase.Supabase",
        "weaviate": "mem0.vector_stores.weaviate.Weaviate",
        "faiss": "mem0.vector_stores.faiss.FAISS",
        "langchain": "mem0.vector_stores.langchain.Langchain",
        "s3_vectors": "mem0.vector_stores.s3_vectors.S3Vectors",
        "baidu": "mem0.vector_stores.baidu.BaiduDB",
        "cassandra": "mem0.vector_stores.cassandra.CassandraDB",
        "neptune": "mem0.vector_stores.neptune_analytics.NeptuneAnalyticsVector",
        "turbopuffer": "mem0.vector_stores.turbopuffer.TurbopufferDB",
    }

    @classmethod
    def create(cls, provider_name, config, *, timeout_seconds: Optional[float] = None):
        class_type = cls.provider_to_class.get(provider_name)
        if class_type:
            if not isinstance(config, dict):
                config = config.model_dump()
            vector_store_instance = load_class(class_type)
            if timeout_seconds is not None:
                constructor_parameters = inspect.signature(vector_store_instance).parameters
                if "timeout_seconds" in constructor_parameters and "timeout_seconds" not in config:
                    config["timeout_seconds"] = float(timeout_seconds)
            return _configure_native_timeout(vector_store_instance(**config), timeout_seconds)
        else:
            raise ValueError(f"Unsupported VectorStore provider: {provider_name}")

    @classmethod
    def reset(cls, instance):
        instance.reset()
        return instance


class RerankerFactory:
    """
    Factory for creating reranker instances with appropriate configurations.
    Supports provider-specific configs following the same pattern as other factories.
    """

    # Provider mappings with their config classes
    provider_to_class = {
        "cohere": ("mem0.reranker.cohere_reranker.CohereReranker", CohereRerankerConfig),
        "sentence_transformer": (
            "mem0.reranker.sentence_transformer_reranker.SentenceTransformerReranker",
            SentenceTransformerRerankerConfig,
        ),
        "zero_entropy": ("mem0.reranker.zero_entropy_reranker.ZeroEntropyReranker", ZeroEntropyRerankerConfig),
        "llm_reranker": ("mem0.reranker.llm_reranker.LLMReranker", LLMRerankerConfig),
        "huggingface": ("mem0.reranker.huggingface_reranker.HuggingFaceReranker", HuggingFaceRerankerConfig),
    }

    @classmethod
    def create(
        cls,
        provider_name: str,
        config: Optional[Union[BaseRerankerConfig, Dict]] = None,
        *,
        timeout_seconds: Optional[float] = None,
        **kwargs,
    ):
        """
        Create a reranker instance based on the provider and configuration.

        Args:
            provider_name: The reranker provider (e.g., 'cohere', 'sentence_transformer')
            config: Configuration object or dictionary
            **kwargs: Additional configuration parameters

        Returns:
            Reranker instance configured for the specified provider

        Raises:
            ImportError: If the provider class cannot be imported
            ValueError: If the provider is not supported
        """
        if provider_name not in cls.provider_to_class:
            raise ValueError(f"Unsupported reranker provider: {provider_name}")

        class_path, config_class = cls.provider_to_class[provider_name]

        # Handle configuration
        if config is None:
            config = config_class(**kwargs)
        elif isinstance(config, dict):
            config = config_class(**config, **kwargs)
        elif not isinstance(config, BaseRerankerConfig):
            raise ValueError(f"Config must be a {config_class.__name__} instance or dict")

        # Import and create the reranker class
        try:
            reranker_class = load_class(class_path)
        except (ImportError, AttributeError) as e:
            raise ImportError(f"Could not import reranker for provider '{provider_name}': {e}")

        return _configure_native_timeout(reranker_class(config), timeout_seconds)
