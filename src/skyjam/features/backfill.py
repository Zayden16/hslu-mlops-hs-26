"""Backfill: rebuild the feature store from retained raw observations.

The live ADS-B feed has no history endpoint, so we cannot backfill the *past*
from the source: the only way to obtain history is to keep polling. What we can
and must backfill is the derived layer. Every raw snapshot is retained, so when
a definition changes (H3 resolution, altitude floor, degraded threshold) the
whole feature store is regenerated from raw rather than being silently mixed
between old and new semantics.

This is the reproducibility guarantee: raw is immutable, features are derived.
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime

import h3
import pandas as pd

from skyjam.common.config import get_settings
from skyjam.common.schema import H3_RESOLUTION
from skyjam.common.storage import read_dataset, write_partition
from skyjam.features.aggregate import aggregate_cells

logger = logging.getLogger(__name__)


def recell(observations: pd.DataFrame, resolution: int = H3_RESOLUTION) -> pd.DataFrame:
    """Recompute H3 cells so a resolution change can be applied retroactively."""
    frame = observations.copy()
    frame["h3_cell"] = [
        h3.latlng_to_cell(lat, lon, resolution)
        for lat, lon in zip(frame["lat"], frame["lon"], strict=True)
    ]
    return frame


def backfill(
    start: datetime | None = None, end: datetime | None = None
) -> pd.DataFrame:
    settings = get_settings()
    raw = read_dataset(settings.raw_store_uri, "observations")
    if raw.empty:
        logger.error("no raw observations retained; nothing to backfill")
        return raw

    raw["observed_at"] = pd.to_datetime(raw["observed_at"], utc=True)
    if start is not None:
        raw = raw[raw["observed_at"] >= pd.Timestamp(start, tz="UTC")]
    if end is not None:
        raw = raw[raw["observed_at"] <= pd.Timestamp(end, tz="UTC")]
    if raw.empty:
        logger.error("no raw observations in the requested range")
        return raw

    raw["slot_start"] = raw["observed_at"].dt.floor("60min")
    cells = aggregate_cells(recell(raw))

    # Rewrite each day's partition from scratch so the derived layer cannot end
    # up holding a mixture of old and new feature definitions.
    for day, chunk in cells.groupby(cells["slot_start"].dt.date):
        write_partition(
            chunk,
            settings.feature_store_uri,
            "cell_hourly",
            day=datetime.combine(day, datetime.min.time()),
            run_id=f"backfill-{datetime.now().strftime('%Y%m%dT%H%M%S')}",
        )
    logger.info(
        "backfilled %d cell-slots across %d days from %d raw observations",
        len(cells),
        cells["slot_start"].dt.date.nunique(),
        len(raw),
    )
    return cells


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild features from raw.")
    parser.add_argument("--start", type=lambda s: datetime.fromisoformat(s))
    parser.add_argument("--end", type=lambda s: datetime.fromisoformat(s))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    backfill(args.start, args.end)


if __name__ == "__main__":
    main()
