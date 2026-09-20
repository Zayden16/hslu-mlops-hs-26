"""Build the modelling table: lagged features and the forward-looking target.

Leakage rules enforced here:

1. Every feature for slot *t* is derived from slots <= *t*. The target is the
   cell state at *t + horizon*, produced by shifting backwards in time.
2. Lags are computed on a *complete* hourly index per cell. A positional shift
   would be wrong whenever the ingest cron skipped a run, silently turning a
   "1 hour ago" feature into "3 hours ago". Reindexing onto a regular grid makes
   gaps explicit as NaN instead of quietly mislabelling them.
3. Missing slots are never forward-filled into the target. If we did not observe
   t+horizon, that row simply has no label and is dropped.
"""

from __future__ import annotations

import argparse
import logging

import pandas as pd

from skyjam.common.schema import (
    BASE_COLUMNS,
    FORECAST_HORIZON_HOURS,
    LABEL_COLUMN,
    LAG_HOURS,
    TARGET_COLUMN,
)

logger = logging.getLogger(__name__)


def _regular_hourly_index(frame: pd.DataFrame) -> pd.DataFrame:
    """Reindex each cell onto a gap-free hourly grid.

    Rows that did not exist become NaN, which is what we want: an unobserved
    hour is unknown, not quiet.
    """
    frame = frame.sort_values(["h3_cell", "slot_start"])
    full_range = pd.date_range(
        frame["slot_start"].min(), frame["slot_start"].max(), freq="h", tz="UTC"
    )
    cells = frame["h3_cell"].unique()
    index = pd.MultiIndex.from_product([cells, full_range], names=["h3_cell", "slot_start"])
    return (
        frame.set_index(["h3_cell", "slot_start"])
        .reindex(index)
        .reset_index()
    )


def build_features(cells: pd.DataFrame) -> pd.DataFrame:
    """Turn per-cell hourly observations into a supervised learning table."""
    if cells.empty:
        return pd.DataFrame()

    frame = cells.copy()
    frame["slot_start"] = pd.to_datetime(frame["slot_start"], utc=True)

    # Several ingest runs land in the same hour; collapse them per cell-slot.
    frame = (
        frame.groupby(["h3_cell", "slot_start"], as_index=False)
        .agg(
            aircraft_count=("aircraft_count", "max"),
            degraded_share=("degraded_share", "mean"),
            median_nic=("median_nic", "median"),
            p25_nic=("p25_nic", "min"),
            mean_nac_p=("mean_nac_p", "mean"),
            is_degraded=("is_degraded", "max"),
        )
    )

    frame = _regular_hourly_index(frame)
    grouped = frame.groupby("h3_cell", sort=False)

    # --- Temporal lags, computed on the regular grid ---
    for lag in LAG_HOURS:
        frame[f"degraded_share_lag_{lag}h"] = grouped["degraded_share"].shift(lag)
        frame[f"is_degraded_lag_{lag}h"] = grouped[LABEL_COLUMN].shift(lag)

    # --- Rolling context, strictly backward looking ---
    for window in (6, 24):
        frame[f"degraded_share_mean_{window}h"] = grouped["degraded_share"].transform(
            lambda s, w=window: s.shift(1).rolling(w, min_periods=1).mean()
        )
    frame["aircraft_count_mean_24h"] = grouped["aircraft_count"].transform(
        lambda s: s.shift(1).rolling(24, min_periods=1).mean()
    )

    # --- Calendar features: interference has a strong daily rhythm ---
    frame["hour_utc"] = frame["slot_start"].dt.hour
    frame["day_of_week"] = frame["slot_start"].dt.dayofweek

    # --- Target: the cell state `horizon` hours into the future ---
    frame[TARGET_COLUMN] = grouped[LABEL_COLUMN].shift(-FORECAST_HORIZON_HOURS)

    # Rows without an observed future state cannot be trained on.
    frame = frame[frame[TARGET_COLUMN].notna()].copy()
    frame[TARGET_COLUMN] = frame[TARGET_COLUMN].astype(int)

    # The current slot must itself have been observed to be a valid example.
    frame = frame[frame["degraded_share"].notna()].copy()
    return frame.reset_index(drop=True)


def feature_columns(frame: pd.DataFrame) -> list[str]:
    """Columns the model may consume. Excludes keys and anything future-derived."""
    forbidden = {TARGET_COLUMN, "h3_cell", "slot_start"}
    return [c for c in frame.columns if c not in forbidden]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the modelling table.")
    parser.add_argument(
        "--out",
        help="optional Parquet path to write the table to; otherwise only summarised",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    # Imported here so `build_features` stays importable, and unit-testable,
    # without a database or a configured DATABASE_URL.
    from skyjam.common.db import get_engine, read_cell_hourly

    cells = read_cell_hourly(get_engine())
    if cells.empty:
        logger.error("no observations in the feature store; is the ingest service running?")
        return

    features = build_features(cells)
    if features.empty:
        logger.error(
            "no rows survived: %d cell-slots exist but none have an observed "
            "state %d hours later yet",
            len(cells),
            FORECAST_HORIZON_HOURS,
        )
        return

    logger.info(
        "built %d rows over %d cells, positive rate %.3f, base columns %s",
        len(features),
        features["h3_cell"].nunique(),
        features[TARGET_COLUMN].mean(),
        list(BASE_COLUMNS),
    )

    if args.out:
        features.to_parquet(args.out, index=False)
        logger.info("wrote %s", args.out)


if __name__ == "__main__":
    main()
