"""Runtime configuration for the Python pipelines, loaded from environment / .env.

The store location is deliberately absent: it is `DATABASE_URL`, read in
`common.db`, because Railway injects that name and splitting it across two
settings objects would invite them to disagree.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All tunables in one place so pipelines cannot drift apart."""

    model_config = SettingsConfigDict(
        env_prefix="SKYJAM_", env_file=".env", extra="ignore"
    )

    adsb_base_url: str = "https://api.adsb.lol/v2"
    user_agent: str = "skyjam/0.1 (HSLU MLOps student project)"

    # Polling. The feed is rate-limited by nginx with no Retry-After and no
    # quota headers: measured, a burst is cut off after 2-3 requests and
    # recovers after ~15s. These defaults match services/ingestor, so an
    # on-demand inference sweep is paced exactly like the capture service.
    request_timeout_s: float = 30.0
    inter_request_delay_s: float = 12.0
    max_retries: int = 4


def get_settings() -> Settings:
    return Settings()
