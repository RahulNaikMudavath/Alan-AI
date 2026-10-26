"""Build providers from settings.

Keeping construction in one place means the app and the CLI cannot drift, and
misconfiguration fails immediately with an actionable message instead of a
confusing runtime error on the first write.
"""

from app.config import Settings
from app.llm.base import EmbeddingProvider
from app.llm.fake import DeterministicEmbeddingProvider
from app.llm.openai_compatible import OpenAICompatibleEmbeddingProvider


def build_embedding_provider(settings: Settings) -> EmbeddingProvider | None:
    """Return the configured provider, or None when embeddings are disabled."""
    if settings.embedding_provider == "none":
        return None

    if settings.embedding_provider == "fake":
        return DeterministicEmbeddingProvider(dimensions=settings.embedding_dimensions)

    if settings.embedding_provider == "openai_compatible":
        if settings.embedding_api_key is None or not (
            settings.embedding_api_key.get_secret_value().strip()
        ):
            raise ValueError(
                "RECALLKIT_EMBEDDING_API_KEY must be set when "
                "RECALLKIT_EMBEDDING_PROVIDER=openai_compatible"
            )
        return OpenAICompatibleEmbeddingProvider(
            base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key.get_secret_value(),
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            timeout=settings.embedding_timeout_seconds,
        )

    # Unreachable while the setting is a Literal, kept for a future provider.
    raise ValueError(f"unknown embedding provider: {settings.embedding_provider}")
