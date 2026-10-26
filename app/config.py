"""Configuration loaded from environment variables and an optional .env file."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Directory that contains this package (the project root).
PROJECT_ROOT = Path(__file__).resolve().parent.parent

EmbeddingProviderName = Literal["fake", "openai_compatible", "none"]

# Dimensions the OpenAI-compatible defaults are known to produce.
_DEFAULT_OPENAI_MODEL = "text-embedding-3-small"
_DEFAULT_OPENAI_DIMENSIONS = 1536
_DEFAULT_FAKE_DIMENSIONS = 256


class Settings(BaseSettings):
    """Runtime settings.

    Every field can be overridden with a ``RECALLKIT_``-prefixed environment
    variable, e.g. ``RECALLKIT_DATABASE_PATH``. Relative paths are resolved
    against the project root so the app behaves the same regardless of the
    directory it is started from.

    The default embedding provider is ``fake``: it is deterministic, runs
    offline, and costs nothing. It measures *word overlap*, not meaning, so it
    is suitable for development and tests but not for real retrieval quality.
    """

    model_config = SettingsConfigDict(
        env_prefix="RECALLKIT_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "RecallKit"
    environment: str = "development"
    database_path: Path = PROJECT_ROOT / "data" / "recallkit.db"

    # -- embeddings --------------------------------------------------------
    embedding_provider: EmbeddingProviderName = "fake"
    # Empty means "use the provider's default", resolved below.
    embedding_model: str = ""
    # Zero means "use the provider's default", resolved below.
    embedding_dimensions: int = 0
    embedding_base_url: str = "https://api.openai.com/v1"
    # SecretStr keeps the value out of logs, reprs and tracebacks.
    embedding_api_key: SecretStr | None = None
    embedding_timeout_seconds: float = Field(default=10.0, gt=0)

    # -- retrieval ---------------------------------------------------------
    semantic_max_candidates: int = Field(default=5000, ge=1)
    # Candidates at or below this cosine score are dropped. A floor, not a
    # guarantee: real embeddings frequently give unrelated text a small
    # positive score, so raise it only with measurement.
    semantic_min_similarity: float = Field(default=0.0, ge=-1.0, le=1.0)
    hybrid_candidate_pool: int = Field(default=50, ge=1, le=500)
    hybrid_keyword_weight: float = Field(default=1.0, ge=0)
    hybrid_semantic_weight: float = Field(default=1.0, ge=0)
    hybrid_recency_weight: float = Field(default=0.3, ge=0)
    rrf_k: int = Field(default=60, ge=1)

    @field_validator("database_path")
    @classmethod
    def _resolve_against_project_root(cls, value: Path) -> Path:
        return value if value.is_absolute() else (PROJECT_ROOT / value)

    @model_validator(mode="after")
    def _resolve_embedding_defaults(self) -> Settings:
        """Fill in model and dimension defaults for the chosen provider.

        Keeping this here means ``RECALLKIT_EMBEDDING_PROVIDER`` alone is a
        valid configuration, while still letting both values be overridden.
        """
        if self.embedding_provider == "fake":
            self.embedding_dimensions = self.embedding_dimensions or _DEFAULT_FAKE_DIMENSIONS
            self.embedding_model = (
                self.embedding_model or f"recallkit-fake-hash-v1-{self.embedding_dimensions}"
            )
        elif self.embedding_provider == "openai_compatible":
            self.embedding_model = self.embedding_model or _DEFAULT_OPENAI_MODEL
            self.embedding_dimensions = self.embedding_dimensions or _DEFAULT_OPENAI_DIMENSIONS
        return self
