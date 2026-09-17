"""Runtime configuration, loaded from environment / .env."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All tunables in one place so pipelines cannot drift apart."""

    model_config = SettingsConfigDict(
        env_prefix="SKYJAM_", env_file=".env", extra="ignore"
    )

    adsb_base_url: str = "https://api.adsb.lol/v2"
    user_agent: str = "skyjam/0.1 (HSLU MLOps student project)"

    feature_store_uri: str = "data/feature_store"
    raw_store_uri: str = "data/raw"

    # Polling: the public API rate-limits aggressive clients, so we pace requests.
    request_timeout_s: float = 30.0
    inter_request_delay_s: float = 2.0
    max_retries: int = 4


def get_settings() -> Settings:
    return Settings()
