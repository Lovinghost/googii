from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    model_provider: Literal["openai", "anthropic"] = "openai"
    model_name: str = "gpt-4.1-mini"
    workspace_root: Path = Path("workspace")
    database_path: Path = Path("storage/checkpoints.sqlite")
    sandbox_image: str = "python:3.12-slim"
    sandbox_cpus: float = Field(default=1.0, gt=0, le=8)
    sandbox_memory: str = "512m"
    sandbox_pids_limit: int = Field(default=256, ge=32, le=4096)
    command_timeout_max: int = Field(default=300, ge=1, le=3600)
    agent_max_iterations: int = Field(default=20, ge=1, le=100)
    model_context_limit: int = Field(default=32_000, ge=4_096)
    output_token_reserve_ratio: float = Field(default=0.15, ge=0.05, le=0.4)
    context_safety_buffer_ratio: float = Field(default=0.10, ge=0.05, le=0.3)
    recent_message_limit: int = Field(default=10, ge=2, le=50)
    tool_inline_max_bytes: int = Field(default=8 * 1024, ge=1024)
    max_tool_calls: int = Field(default=40, ge=1, le=200)
    max_no_progress_steps: int = Field(default=3, ge=1, le=10)
    max_wall_time_seconds: int = Field(default=900, ge=10, le=7200)
    event_history_limit: int = Field(default=1000, ge=100, le=10_000)

    @field_validator("workspace_root", "database_path", mode="after")
    @classmethod
    def absolute_path(cls, value: Path) -> Path:
        return value.expanduser().resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()
