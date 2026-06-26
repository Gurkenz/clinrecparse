from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

DEFAULT_CONFIG_PATH = Path("config/default.yaml")


class PathSettings(BaseModel):
    data_root: Path
    snapshots: Path
    references: Path
    documents: Path
    indexes: Path
    reports: Path
    logs: Path


class HttpSettings(BaseModel):
    timeout_seconds: float = Field(gt=0)
    retries: int = Field(ge=0)
    backoff_initial_seconds: float = Field(gt=0)
    backoff_max_seconds: float = Field(gt=0)
    user_agent: str = "clinrecparse/0.1 contact: local-development"
    circuit_breaker_5xx_threshold: int = Field(default=5, ge=1)

    @field_validator("backoff_max_seconds")
    @classmethod
    def max_backoff_not_smaller_than_initial(cls, value: float, info: Any) -> float:
        initial = info.data.get("backoff_initial_seconds")
        if initial is not None and value < initial:
            raise ValueError("backoff_max_seconds must be >= backoff_initial_seconds")
        return value


class RateLimitSettings(BaseModel):
    requests_per_second: float = Field(gt=0, le=2)


class ConcurrencySettings(BaseModel):
    default: int = Field(ge=1)
    max: int = Field(ge=1, le=2)

    @field_validator("max")
    @classmethod
    def max_not_smaller_than_default(cls, value: int, info: Any) -> int:
        default = info.data.get("default")
        if default is not None and value < default:
            raise ValueError("concurrency.max must be >= concurrency.default")
        return value


class DiscoverySettings(BaseModel):
    unavailable_retry_ttl_days: int = Field(default=7, ge=0)


class BankSettings(BaseModel):
    max_catalog_drop_percent: float = Field(default=5, ge=0, le=100)
    max_identity_conflicts: int = Field(default=0, ge=0)
    require_manual_apply_on_removed: bool = True


class LoggingSettings(BaseModel):
    level: str
    jsonl_path: Path


class Settings(BaseModel):
    paths: PathSettings
    http: HttpSettings
    rate_limit: RateLimitSettings
    concurrency: ConcurrencySettings
    discovery: DiscoverySettings
    bank: BankSettings = Field(default_factory=BankSettings)
    logging: LoggingSettings


def load_settings(config_path: Path | str = DEFAULT_CONFIG_PATH) -> Settings:
    path = Path(config_path)
    with path.open("r", encoding="utf-8") as file:
        raw_config = yaml.safe_load(file) or {}
    return Settings.model_validate(raw_config)


def ensure_data_directories(settings: Settings) -> None:
    for path in (
        settings.paths.snapshots,
        settings.paths.references,
        settings.paths.documents,
        settings.paths.indexes,
        settings.paths.reports,
        settings.paths.logs,
    ):
        path.mkdir(parents=True, exist_ok=True)
