"""Feature pipeline entrypoint: poll every sample point, aggregate, persist.

Run on a schedule (GitHub Actions cron). Each run appends one batch of
per-cell hourly observations to the feature store. Because the label depends on
observing the sky continuously, this job is the only thing that creates the
dataset: there is no static download to fall back on.
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import UTC, datetime

import pandas as pd

from skyjam.common.adsb_client import AdsbClient
from skyjam.common.config import get_settings
from skyjam.common.grid import active_points
from skyjam.common.storage import write_partition
from skyjam.features.aggregate import aggregate_cells, normalise_aircraft

logger = logging.getLogger(__name__)


def collect_snapshot() -> pd.DataFrame:
    """Poll every sample point once and return normalised observations."""
    settings = get_settings()
    observed_at = datetime.now(UTC)
    frames: list[pd.DataFrame] = []

    with AdsbClient(settings) as client:
        for point in active_points():
            try:
                snapshot = client.fetch_point(point.lat, point.lon, point.radius_nm)
            except Exception:
                # One unreachable region must not lose the whole run.
                logger.exception("failed to poll %s, continuing", point.name)
                continue

            frame = normalise_aircraft(snapshot.aircraft, observed_at)
            if not frame.empty:
                frame["sample_point"] = point.name
                frame["is_control_region"] = point.control
                frames.append(frame)
            logger.info(
                "polled %-22s aircraft=%4d usable=%4d",
                point.name,
                len(snapshot.aircraft),
                len(frame),
            )
            time.sleep(settings.inter_request_delay_s)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def run_once(store_raw: bool = True) -> pd.DataFrame:
    settings = get_settings()
    observations = collect_snapshot()
    if observations.empty:
        logger.warning("no usable observations in this run")
        return observations

    if store_raw:
        write_partition(observations, settings.raw_store_uri, "observations")

    cells = aggregate_cells(observations)
    write_partition(cells, settings.feature_store_uri, "cell_hourly")
    logger.info(
        "aggregated %d observations into %d cell-slots (%d degraded)",
        len(observations),
        len(cells),
        int(cells["is_degraded"].sum()) if not cells.empty else 0,
    )
    return cells


def main() -> None:
    parser = argparse.ArgumentParser(description="Poll ADS-B feeds once.")
    parser.add_argument(
        "--no-raw", action="store_true", help="skip persisting raw observations"
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    run_once(store_raw=not args.no_raw)


if __name__ == "__main__":
    main()
