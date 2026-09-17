"""Thin client for the public ADS-B aggregator feeds.

The v2 `point` endpoint returns every aircraft currently tracked within a radius
of a coordinate, including the self-reported navigation quality fields (`nic`,
`nac_p`, `sil`) that this project is built on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from skyjam.common.config import Settings, get_settings

logger = logging.getLogger(__name__)

# The public endpoint returns 429 when polled too aggressively; treat it as
# retryable with exponential backoff rather than losing the snapshot.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class AdsbRequestError(RuntimeError):
    """Raised for a response we should retry."""


@dataclass(frozen=True)
class Snapshot:
    """One raw API response plus the server timestamp it carries."""

    lat: float
    lon: float
    radius_nm: int
    now_ms: int
    aircraft: list[dict[str, Any]]


class AdsbClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = httpx.Client(
            base_url=self.settings.adsb_base_url,
            headers={"User-Agent": self.settings.user_agent},
            timeout=self.settings.request_timeout_s,
        )

    def __enter__(self) -> AdsbClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    @retry(
        retry=retry_if_exception_type((AdsbRequestError, httpx.TransportError)),
        wait=wait_exponential(multiplier=3, min=3, max=60),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def fetch_point(self, lat: float, lon: float, radius_nm: int = 250) -> Snapshot:
        """Fetch all aircraft within `radius_nm` of a coordinate."""
        response = self._client.get(f"/point/{lat}/{lon}/{radius_nm}")
        if response.status_code in RETRYABLE_STATUS:
            raise AdsbRequestError(
                f"retryable status {response.status_code} for {lat},{lon}"
            )
        response.raise_for_status()
        payload = response.json()
        return Snapshot(
            lat=lat,
            lon=lon,
            radius_nm=radius_nm,
            now_ms=int(payload.get("now", 0)),
            aircraft=payload.get("ac") or [],
        )
