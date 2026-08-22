import math
import os
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from mem0.configs.rerankers.config import RerankerConfig
from mem0.embeddings.configs import EmbedderConfig
from mem0.llms.configs import LlmConfig
from mem0.vector_stores.configs import VectorStoreConfig

# Set up the directory path
home_dir = os.path.expanduser("~")
mem0_dir = os.environ.get("MEM0_DIR") or os.path.join(home_dir, ".mem0")


class MemoryItem(BaseModel):
    id: str = Field(..., description="The unique identifier for the text data")
    memory: str = Field(
        ..., description="The memory deduced from the text data"
    )  # TODO After prompt changes from platform, update this
    hash: Optional[str] = Field(None, description="The hash of the memory")
    # The metadata value can be anything and not just string. Fix it
    metadata: Optional[Dict[str, Any]] = Field(None, description="Additional metadata for the text data")
    score: Optional[float] = Field(None, description="The score associated with the text data")
    created_at: Optional[str] = Field(None, description="The timestamp when the memory was created")
    updated_at: Optional[str] = Field(None, description="The timestamp when the memory was updated")


class MidTermMemoryConfig(BaseModel):
    enabled: bool = Field(True, description="Enable the mid-term memory layer")
    short_term_capacity: int = Field(10, description="Number of recent SQLite messages to keep per session")
    session_similarity_threshold: float = Field(0.8, description="Minimum score for assigning a page to a session")
    embedding_similarity_weight: float = Field(0.7, description="Weight for embedding similarity during topic routing")
    keyword_overlap_weight: float = Field(0.3, description="Weight for keyword overlap during topic routing")
    top_k_sessions: int = Field(5, ge=0, description="Number of mid-term sessions to retrieve")
    top_k_pages: int = Field(5, ge=0, description="Number of candidate mid-term pages to retrieve per session")
    max_total_pages: int = Field(4, ge=0, description="Maximum total mid-term pages to return")
    # The production retriever uses this only for global Page supplementation.
    # Keep the historical multiplier as the default while making experiments explicit.
    midterm_candidate_pool_multiplier: int = Field(4, ge=1, le=8)
    midterm_rag_threshold: float = Field(
        0.1,
        ge=0,
        le=1,
        description="Minimum raw RAG score for a mid-term page to enter context",
    )

    retention_half_life_turns: float = Field(
        168.0,
        gt=0,
        description="Base conversation-turn half-life used by deterministic mid-term retrieval decay",
    )
    retention_floor: float = Field(
        0.2,
        ge=0,
        le=1,
        description="Minimum mid-term forgetting factor",
    )
    heat_recency_tau_turns: float = Field(
        24.0,
        gt=0,
        description="Conversation-turn decay constant for session visit recency",
    )
    heat_modulation_min: float = Field(
        0.9,
        gt=0,
        lt=1,
        description="Minimum session-heat factor applied to the mid-term half-life",
    )
    heat_modulation_max: float = Field(
        1.1,
        gt=1,
        description="Maximum session-heat factor applied to the mid-term half-life",
    )

    heat_alpha: float = Field(1.0, description="Session heat weight for visit count")
    heat_beta: float = Field(0.5, description="Session heat weight for interaction count")
    heat_gamma: float = Field(1.0, description="Session heat weight for recall recency")
    promotion_min_recall_count: int = Field(
        3,
        ge=1,
        description="Minimum session valid recalls required for cross-session promotion",
    )
    promotion_heat_threshold: float = Field(
        5.0,
        ge=0,
        description="Minimum absolute session heat required for cross-session promotion",
    )

    @model_validator(mode="after")
    def validate_heat_modulation_range(self):
        if self.heat_modulation_min >= self.heat_modulation_max:
            raise ValueError("heat_modulation_min must be less than heat_modulation_max")
        return self


class UserProfileConfig(BaseModel):
    enabled: bool = True
    update_on_add: bool = True
    extraction_mode: Literal["explicit_only", "explicit_and_inferred"] = "explicit_and_inferred"
    allow_dynamic_attributes: bool = False
    max_dynamic_attributes: int = Field(100, ge=0)
    max_input_user_messages: int = Field(4, ge=1)
    max_operations_per_update: int = Field(8, ge=1)
    max_value_json_bytes: int = Field(16384, ge=1)
    include_metadata_by_default: bool = False
    llm_max_tokens: int = Field(4096, ge=1, le=65536)
    llm_request_options: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("llm_request_options")
    @classmethod
    def validate_llm_request_options(cls, options: Dict[str, Any]) -> Dict[str, Any]:
        reserved = {"messages", "response_format", "tools", "tool_choice", "max_tokens", "_return_metadata"}
        conflicts = sorted(reserved & set(options))
        if conflicts:
            raise ValueError(
                f"llm_request_options cannot override reserved profile request fields: {', '.join(conflicts)}"
            )
        return options


class BackgroundTaskConfig(BaseModel):
    enabled: bool = True
    midterm_worker_count: int = Field(1, ge=1, le=16)
    longterm_worker_count: int = Field(1, ge=1, le=16)
    profile_worker_count: int = Field(1, ge=1, le=16)
    promotion_worker_count: int = Field(1, ge=1, le=16)
    midterm_worker_concurrency: int = Field(1, ge=1, le=1024)
    longterm_worker_concurrency: int = Field(1, ge=1, le=1024)
    profile_worker_concurrency: int = Field(1, ge=1, le=1024)
    promotion_worker_concurrency: int = Field(2, ge=1, le=1024)
    entity_extraction_worker_count: int = Field(2, ge=1, le=16)
    entity_extraction_pending_capacity: int = Field(8, ge=1, le=128)
    max_retries: int = Field(3, ge=0)
    retry_delays_seconds: tuple[float, ...] = (1.0, 2.0, 3.0)
    poll_interval_seconds: float = Field(1.0, gt=0)
    lease_timeout_seconds: float = Field(120.0, gt=0)
    heartbeat_interval_seconds: float = Field(20.0, gt=0)
    watchdog_interval_seconds: float = Field(10.0, gt=0)
    max_stale_recoveries: int = Field(3, ge=0)
    shutdown_timeout_seconds: float = Field(30.0, ge=0)
    include_pending_in_context: bool = Field(
        True,
        description="Legacy context preference; unfinished migration sources are always retained for correctness",
    )
    max_pending_context_messages: int = Field(
        20,
        ge=0,
        description="Legacy bridge limit; it never truncates unfinished migration source messages",
    )
    include_failed_in_context: bool = Field(
        False,
        description="Legacy context preference; unfinished migration sources remain visible until finalized",
    )

    @field_validator("retry_delays_seconds")
    @classmethod
    def validate_retry_delays_seconds(cls, delays: tuple[float, ...]) -> tuple[float, ...]:
        if any(not math.isfinite(delay) or delay < 0 for delay in delays):
            raise ValueError("retry_delays_seconds values must be finite and greater than or equal to 0")
        return delays

    @model_validator(mode="after")
    def validate_lease_intervals(self):
        if self.heartbeat_interval_seconds >= self.lease_timeout_seconds:
            raise ValueError("heartbeat_interval_seconds must be less than lease_timeout_seconds")
        return self


class AgenticRetrievalConfig(BaseModel):
    """Limits for the optional, low-latency mid-term retrieval flow."""

    enabled: bool = True
    max_iterations: int = Field(2, ge=1, le=2)
    max_tool_calls: int = Field(1, ge=1, le=1)
    max_queries: int = Field(3, ge=1, le=3)
    candidate_pool_size: int = Field(20, ge=5, le=100)
    max_total_results: int = Field(
        6,
        ge=1,
        le=20,
        description="最终返回给模型的完整中期记忆 Page 数量",
    )
    max_tool_result_chars: int = Field(30000, ge=1000)
    force_final_answer: bool = True


class MemoryConfig(BaseModel):
    vector_store: VectorStoreConfig = Field(
        description="Configuration for the vector store",
        default_factory=VectorStoreConfig,
    )
    llm: LlmConfig = Field(
        description="Configuration for the language model",
        default_factory=LlmConfig,
    )
    embedder: EmbedderConfig = Field(
        description="Configuration for the embedding model",
        default_factory=EmbedderConfig,
    )
    history_db_path: str = Field(
        description="Path to the history database",
        default=os.path.join(mem0_dir, "history.db"),
    )
    enforce_single_process: bool = Field(
        True,
        description="Prevent multiple service processes from using the same history database",
    )
    llm_timeout_seconds: float = Field(60.0, gt=0)
    embedding_timeout_seconds: float = Field(30.0, gt=0)
    vector_store_timeout_seconds: float = Field(15.0, gt=0)
    reranker_timeout_seconds: float = Field(30.0, gt=0)
    entity_extraction_timeout_seconds: float = Field(60.0, gt=0)
    reranker: Optional[RerankerConfig] = Field(
        description="Configuration for the reranker",
        default=None,
    )
    version: str = Field(
        description="The version of the API",
        default="v1.1",
    )
    custom_instructions: Optional[str] = Field(
        description="Custom instructions for fact extraction",
        default=None,
    )
    longterm_rag_threshold: float = Field(
        0.1,
        ge=0,
        le=1,
        description="Minimum raw RAG score for user-scoped fine-grained long-term memory",
    )
    longterm_candidate_pool_multiplier: int = Field(
        4,
        ge=1,
        le=6,
        description="Candidate over-fetch multiplier; the production floor remains 60",
    )
    longterm_other_session_weight: float = Field(
        0.7,
        ge=0,
        le=1,
        description="Ranking weight applied to fine-grained LongTerm memories from other runs",
    )
    entity_similarity_threshold: float = Field(
        0.5,
        ge=0,
        le=1,
        description="Minimum entity similarity used for entity-aware long-term scoring",
    )
    cross_session_longterm_rag_threshold: float = Field(
        0.1,
        ge=0,
        le=1,
        description="Minimum raw RAG score for user-scoped cross-session long-term memory",
    )
    cross_session_retention_half_life_hours: float = Field(
        720.0,
        gt=0,
        description="Base half-life for slow cross-session long-term retrieval decay",
    )
    cross_session_retention_floor: float = Field(
        0.2,
        ge=0,
        le=1,
        description="Minimum cross-session long-term forgetting factor",
    )
    cross_session_reinforcement_gain: float = Field(
        0.25,
        ge=0,
        description="Independent logarithmic strength gain for cross-session valid recalls",
    )
    midterm: MidTermMemoryConfig = Field(
        description="Configuration for the optional mid-term memory layer",
        default_factory=MidTermMemoryConfig,
    )
    profile: UserProfileConfig = Field(
        description="Configuration for the cross-session user profile layer",
        default_factory=UserProfileConfig,
    )
    background: BackgroundTaskConfig = Field(
        description="Configuration for persistent background memory and profile jobs",
        default_factory=BackgroundTaskConfig,
    )
    agentic_retrieval: AgenticRetrievalConfig = Field(
        description="Configuration for optional model-directed mid-term retrieval",
        default_factory=AgenticRetrievalConfig,
    )


class AzureConfig(BaseModel):
    """
    Configuration settings for Azure.

    Args:
        api_key (str): The API key used for authenticating with the Azure service.
        azure_deployment (str): The name of the Azure deployment.
        azure_endpoint (str): The endpoint URL for the Azure service.
        api_version (str): The version of the Azure API being used.
        default_headers (Dict[str, str]): Headers to include in requests to the Azure API.
    """

    api_key: str = Field(
        description="The API key used for authenticating with the Azure service.",
        default=None,
    )
    azure_deployment: str = Field(description="The name of the Azure deployment.", default=None)
    azure_endpoint: str = Field(description="The endpoint URL for the Azure service.", default=None)
    api_version: str = Field(description="The version of the Azure API being used.", default=None)
    default_headers: Optional[Dict[str, str]] = Field(
        description="Headers to include in requests to the Azure API.", default=None
    )
