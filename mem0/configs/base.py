import os
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field

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
    enabled: bool = Field(False, description="Enable the mid-term memory layer")
    short_term_capacity: int = Field(10, description="Number of recent SQLite messages to keep per session")
    session_similarity_threshold: float = Field(0.65, description="Minimum score for assigning a page to a session")
    embedding_similarity_weight: float = Field(0.7, description="Weight for embedding similarity during topic routing")
    keyword_overlap_weight: float = Field(0.3, description="Weight for keyword overlap during topic routing")
    top_k_sessions: int = Field(5, ge=0, description="Number of mid-term sessions to retrieve")
    top_k_pages: int = Field(5, ge=0, description="Number of candidate mid-term pages to retrieve per session")
    max_total_pages: int = Field(4, ge=0, description="Maximum total mid-term pages to return")
    heat_alpha: float = Field(1.0, description="Session heat weight for visit count")
    heat_beta: float = Field(0.5, description="Session heat weight for interaction count")
    heat_gamma: float = Field(1.0, description="Session heat weight for recency")
    promotion_heat_threshold: float = Field(5.0, description="Reserved threshold for promoting hot sessions")


class UserProfileConfig(BaseModel):
    enabled: bool = False
    update_on_add: bool = True
    extraction_mode: Literal["explicit_only", "explicit_and_inferred"] = "explicit_only"
    allow_dynamic_attributes: bool = False
    max_dynamic_attributes: int = Field(100, ge=0)
    max_input_user_messages: int = Field(4, ge=1)
    max_operations_per_update: int = Field(8, ge=1)
    max_value_json_bytes: int = Field(16384, ge=1)
    include_metadata_by_default: bool = False


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
    midterm: MidTermMemoryConfig = Field(
        description="Configuration for the optional mid-term memory layer",
        default_factory=MidTermMemoryConfig,
    )
    profile: UserProfileConfig = Field(
        description="Configuration for the cross-session user profile layer",
        default_factory=UserProfileConfig,
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
