from typing import Optional

from pydantic import BaseModel, Field


class RerankerConfig(BaseModel):
    """Configuration for rerankers."""

    provider: str = Field(description="Reranker provider (e.g., 'cohere', 'sentence_transformer')", default="cohere")
    config: Optional[dict] = Field(description="Provider-specific reranker configuration", default=None)
    max_concurrency: int = Field(
        default=1,
        ge=1,
        le=32,
        description="Maximum concurrent calls made through the shared production reranker instance",
    )

    model_config = {"extra": "forbid"}
