import math
import os
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from mem0.configs.midterm_prompts import (
    MIDTERM_PAGE_SUMMARY_PROMPT,
    MIDTERM_SESSION_MERGE_PROMPT,
)
from mem0.configs.prompts import ADDITIVE_EXTRACTION_PROMPT
from mem0.configs.query_prompts import QUERY_REFERENCE_RESOLUTION_PROMPT
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


class RetrievalRerankerConfig(BaseModel):
    """Production reranker controls owned by one retrieval layer."""

    method: Literal["none", "multi_vector_maxsim", "cross_encoder"] = "none"
    rerank_depth: int = Field(30, ge=1, le=100)
    dense_weight: float = Field(0.7, ge=0, le=1)
    backend: Optional[RerankerConfig] = Field(
        default=None,
        description="Layer-specific reranker backend; falls back to MemoryConfig.reranker for legacy configs",
    )


def _validate_llm_request_options(options: Dict[str, Any], *, field_name: str) -> Dict[str, Any]:
    conflicts = sorted({"messages", "response_format"} & set(options))
    if conflicts:
        raise ValueError(f"{field_name} cannot override reserved request fields: {', '.join(conflicts)}")
    return options


class MidTermMemoryConfig(BaseModel):
    enabled: bool = Field(True, description="Enable the mid-term memory layer")
    short_term_capacity: int = Field(10, description="Number of recent SQLite messages to keep per session")
    session_similarity_threshold: float = Field(0.8, description="Minimum score for assigning a page to a session")
    embedding_similarity_weight: float = Field(0.7, description="Weight for embedding similarity during topic routing")
    keyword_overlap_weight: float = Field(0.3, description="Weight for keyword overlap during topic routing")
    top_k_sessions: int = Field(5, ge=1, description="Number of mid-term sessions to retrieve")
    top_k_pages: int = Field(5, ge=1, description="Number of candidate mid-term pages to retrieve per session")
    max_total_pages: int = Field(4, ge=1, description="Maximum total mid-term pages to return")
    # The production retriever uses this only for global Page supplementation.
    # Keep the historical multiplier as the default while making experiments explicit.
    midterm_candidate_pool_multiplier: int = Field(4, ge=1, le=8)
    midterm_rag_threshold: float = Field(
        0.1,
        ge=0,
        le=1,
        description="Minimum raw RAG score for a mid-term page to enter context",
    )
    page_representation: Literal[
        "production",
        "P0",
        "summary",
        "P1",
        "user",
        "P2",
        "raw_dialogue",
        "P3",
        "user_assistant",
        "P4",
        "user_summary",
        "P5",
        "summary_raw",
        "P6",
        "user_keywords",
        "P7",
        "summary_keywords",
        "P8",
    ] = Field("production", description="Text representation embedded for each MidTerm Page")
    retrieval_method: Literal["dense", "dense_bm25_fusion"] = Field(
        "dense", description="Production Page candidate retrieval method"
    )
    fusion_method: Literal["normalized_score", "rrf"] = Field(
        "normalized_score", description="Dense/BM25 fusion algorithm"
    )
    dense_weight: float = Field(0.7, ge=0, le=1)
    rrf_rank_constant: int = Field(60, ge=1)
    reranker: RetrievalRerankerConfig = Field(default_factory=RetrievalRerankerConfig)
    page_summary_prompt: str = Field(default=MIDTERM_PAGE_SUMMARY_PROMPT, min_length=1)
    session_merge_prompt: str = Field(default=MIDTERM_SESSION_MERGE_PROMPT, min_length=1)
    page_summary_request_options: Dict[str, Any] = Field(default_factory=dict)
    session_merge_request_options: Dict[str, Any] = Field(default_factory=dict)

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

    @field_validator("page_summary_request_options", "session_merge_request_options")
    @classmethod
    def validate_request_options(cls, options: Dict[str, Any], info) -> Dict[str, Any]:
        return _validate_llm_request_options(options, field_name=info.field_name)


class FineGrainedLongTermConfig(BaseModel):
    """Retrieval and source-generation contract for per-QA extracted facts."""

    enabled: bool = True
    top_k: int = Field(20, ge=1, le=30)
    rag_threshold: float = Field(0.1, ge=0, le=1)
    candidate_pool_multiplier: int = Field(4, ge=1, le=6)
    other_session_weight: float = Field(0.7, ge=0, le=1)
    entity_similarity_threshold: float = Field(0.5, ge=0, le=1)
    semantic_weight: Optional[float] = Field(None, ge=0, le=1)
    bm25_weight: Optional[float] = Field(None, ge=0, le=1)
    entity_weight: Optional[float] = Field(None, ge=0, le=1)
    reranker: RetrievalRerankerConfig = Field(default_factory=RetrievalRerankerConfig)
    extraction_prompt: str = Field(default=ADDITIVE_EXTRACTION_PROMPT, min_length=1)
    extraction_request_options: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("extraction_request_options")
    @classmethod
    def validate_extraction_request_options(cls, options: Dict[str, Any]) -> Dict[str, Any]:
        return _validate_llm_request_options(options, field_name="extraction_request_options")

    @model_validator(mode="after")
    def validate_scoring_weights(self):
        if self.reranker.method not in {"none", "cross_encoder"}:
            raise ValueError("FineGrainedLongTerm reranker must be none or cross_encoder")
        weights = (self.semantic_weight, self.bm25_weight, self.entity_weight)
        if all(value is None for value in weights):
            return self
        if any(value is None for value in weights):
            raise ValueError("semantic_weight, bm25_weight, and entity_weight must be configured together")
        if not math.isclose(sum(float(value) for value in weights), 1.0, abs_tol=1e-9):
            raise ValueError("FineGrainedLongTerm scoring weights must sum to 1")
        return self


class PromotedLongTermConfig(BaseModel):
    """Retrieval contract for heat-promoted MidTerm Session summaries."""

    enabled: bool = True
    top_k: int = Field(4, ge=0, le=100)
    rag_threshold: float = Field(0.1, ge=0, le=1)
    retention_half_life_hours: float = Field(720.0, gt=0)
    retention_floor: float = Field(0.2, ge=0, le=1)
    reinforcement_gain: float = Field(0.25, ge=0)


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
        5,
        ge=1,
        le=5,
        description=(
            "多个 Agentic Query 分别执行 MidTerm 检索后，对结果进行合并、去重、排序，"
            "最终最多返回给模型的 MidTerm Page 数量"
        ),
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
        description="Legacy global reranker backend used only when a retrieval layer has no backend",
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
    query_rewrite_prompt: str = Field(
        default=QUERY_REFERENCE_RESOLUTION_PROMPT,
        min_length=1,
        description="Production ShortTerm-aware query rewrite prompt",
    )
    longterm_top_k: int = Field(
        20,
        ge=1,
        le=30,
        description="Compatibility alias for fine_grained_longterm.top_k",
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
    promoted_longterm_top_k: int = Field(
        4,
        ge=0,
        le=100,
        description="Compatibility alias for promoted_longterm.top_k",
    )
    promoted_longterm_rag_threshold: float = Field(
        0.1,
        ge=0,
        le=1,
        description="Compatibility alias for promoted_longterm.rag_threshold",
    )
    fine_grained_longterm: FineGrainedLongTermConfig = Field(
        description="Production configuration for per-QA extracted fact retrieval",
        default_factory=FineGrainedLongTermConfig,
    )
    promoted_longterm: PromotedLongTermConfig = Field(
        description="Production configuration for promoted MidTerm Session summaries",
        default_factory=PromotedLongTermConfig,
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

    @model_validator(mode="after")
    def synchronize_longterm_compatibility_fields(self):
        """Keep persisted legacy config names valid while new layer configs become canonical."""
        if "fine_grained_longterm" in self.model_fields_set:
            fine = self.fine_grained_longterm
            self.longterm_top_k = fine.top_k
            self.longterm_rag_threshold = fine.rag_threshold
            self.longterm_candidate_pool_multiplier = fine.candidate_pool_multiplier
            self.longterm_other_session_weight = fine.other_session_weight
            self.entity_similarity_threshold = fine.entity_similarity_threshold
        else:
            self.fine_grained_longterm = FineGrainedLongTermConfig(
                top_k=self.longterm_top_k,
                rag_threshold=self.longterm_rag_threshold,
                candidate_pool_multiplier=self.longterm_candidate_pool_multiplier,
                other_session_weight=self.longterm_other_session_weight,
                entity_similarity_threshold=self.entity_similarity_threshold,
            )

        if "promoted_longterm" in self.model_fields_set:
            promoted = self.promoted_longterm
            self.promoted_longterm_top_k = promoted.top_k
            self.promoted_longterm_rag_threshold = promoted.rag_threshold
            self.cross_session_longterm_rag_threshold = promoted.rag_threshold
            self.cross_session_retention_half_life_hours = promoted.retention_half_life_hours
            self.cross_session_retention_floor = promoted.retention_floor
            self.cross_session_reinforcement_gain = promoted.reinforcement_gain
        else:
            threshold = (
                self.promoted_longterm_rag_threshold
                if "promoted_longterm_rag_threshold" in self.model_fields_set
                else self.cross_session_longterm_rag_threshold
            )
            self.promoted_longterm = PromotedLongTermConfig(
                top_k=self.promoted_longterm_top_k,
                rag_threshold=threshold,
                retention_half_life_hours=self.cross_session_retention_half_life_hours,
                retention_floor=self.cross_session_retention_floor,
                reinforcement_gain=self.cross_session_reinforcement_gain,
            )
            self.promoted_longterm_rag_threshold = threshold

        missing_backends = [
            name
            for name, layer in (
                ("midterm", self.midterm),
                ("fine_grained_longterm", self.fine_grained_longterm),
            )
            if layer.reranker.method == "cross_encoder" and layer.reranker.backend is None and self.reranker is None
        ]
        if missing_backends:
            raise ValueError(
                "cross_encoder retrieval requires a layer-specific reranker backend or MemoryConfig.reranker "
                f"fallback: {', '.join(missing_backends)}"
            )
        return self


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
