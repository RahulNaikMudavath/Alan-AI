"""Configuration loaded from environment variables and an optional .env file."""

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Directory that contains this package (the project root).
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Runtime settings.

    Every field can be overridden with a ``RECALLKIT_``-prefixed environment
    variable, e.g. ``RECALLKIT_DATABASE_PATH``. Relative paths are resolved
    against the project root so the app behaves the same regardless of the
    directory it is started from.
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

    @field_validator("database_path")
    @classmethod
    def _resolve_against_project_root(cls, value: Path) -> Path:
        return value if value.is_absolute() else (PROJECT_ROOT / value)
