"""Environment-driven configuration for the orchestrator."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    devin_token: str = field(default_factory=lambda: os.getenv("DEVIN_SERVICE_USER_TOKEN", ""))
    devin_org_id: str = field(default_factory=lambda: os.getenv("DEVIN_ORG_ID", ""))
    devin_api_base: str = field(
        default_factory=lambda: os.getenv("DEVIN_API_BASE", "https://api.devin.ai").rstrip("/")
    )
    github_webhook_secret: str = field(
        default_factory=lambda: os.getenv("GITHUB_WEBHOOK_SECRET", "")
    )
    github_token: str = field(default_factory=lambda: os.getenv("GITHUB_TOKEN", ""))
    github_api_base: str = field(
        default_factory=lambda: os.getenv("GITHUB_API_BASE", "https://api.github.com").rstrip("/")
    )
    target_repo: str = field(default_factory=lambda: os.getenv("TARGET_REPO", "e4c5/superset"))
    trigger_label: str = field(default_factory=lambda: os.getenv("TRIGGER_LABEL", "devin-fix"))
    max_acu_limit: int = field(default_factory=lambda: _int_env("MAX_ACU_LIMIT", 10))
    poll_interval_seconds: int = field(
        default_factory=lambda: _int_env("POLL_INTERVAL_SECONDS", 30)
    )
    session_max_wait_seconds: int = field(
        default_factory=lambda: _int_env("SESSION_MAX_WAIT_SECONDS", 3600)
    )
    database_path: str = field(default_factory=lambda: os.getenv("DATABASE_PATH", "data/state.db"))
    report_path: str = field(default_factory=lambda: os.getenv("REPORT_PATH", "data/report.md"))
    max_retries: int = field(default_factory=lambda: _int_env("MAX_RETRIES", 5))
    dry_run: bool = field(
        default_factory=lambda: os.getenv("DRY_RUN", "false").lower() in {"1", "true", "yes"}
    )

    @property
    def devin_id_prefix(self) -> str:
        return os.getenv("DEVIN_ID_PREFIX", "devin-s6440-issue-")


def get_settings() -> Settings:
    """Read settings fresh from the environment.

    Not cached so tests (and ``.env`` reloads) observe changes.
    """
    return Settings()
