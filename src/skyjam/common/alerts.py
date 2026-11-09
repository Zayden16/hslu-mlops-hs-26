"""ntfy alerts and the capture watchdog, run by the hourly features cron.

The Go ingestor alerts on its own failed sweeps, but it cannot report its own
death. The features cron runs in a separate container every hour, so it also
checks that capture is fresh and alerts if the newest hour is too old.

Alerts are best-effort: a failed notification is logged and never raised, so a
broken alert channel can never fail the feature build itself.
"""

from __future__ import annotations

import logging
import os
import urllib.request
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

# The ingestor sweeps at :00 and finishes in about 4 minutes; the features cron
# runs at :20. So the newest slot is normally 20 minutes old. Two hours means at
# least one full sweep is missing.
MAX_CAPTURE_LAG = timedelta(hours=2)


def send(title: str, body: str, priority: str = "default", tags: str = "") -> bool:
    """Post one alert to SKYJAM_NTFY_URL. Returns False when disabled or failed."""
    url = os.environ.get("SKYJAM_NTFY_URL", "")
    if not url:
        return False
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = tags
    token = os.environ.get("SKYJAM_NTFY_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body.encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - URL is operator config
            return 200 <= resp.status < 300
    except Exception as exc:  # noqa: BLE001 - alerting must never break the pipeline
        logger.error("alert not delivered: %s", exc)
        return False


def capture_lag(engine: Engine, now: datetime | None = None) -> timedelta | None:
    """Age of the newest observed slot, or None when there is no data at all."""
    now = now or datetime.now(UTC)
    with engine.connect() as conn:
        latest = conn.execute(text("SELECT MAX(slot_start) FROM observations")).scalar()
    if latest is None:
        return None
    return now - latest


def check_capture(engine: Engine, now: datetime | None = None) -> str | None:
    """Return a problem description if capture is stale, else None."""
    lag = capture_lag(engine, now)
    if lag is None:
        return "no observations in the database at all"
    if lag > MAX_CAPTURE_LAG:
        hours = lag.total_seconds() / 3600
        return f"newest captured hour is {hours:.1f} h old; the ingestor may be down"
    return None
