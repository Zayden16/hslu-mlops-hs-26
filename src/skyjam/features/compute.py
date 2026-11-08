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

import h3
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


NEIGHBOUR_LAG_HOURS: tuple[int, ...] = (1, 3, 6)


def _neighbour_state(frame: pd.DataFrame) -> pd.DataFrame:
    """Same-slot state of the six adjacent H3 cells (ring 1).

    Interference regions grow and shrink as a whole (MS1 review A3 / E1), so a
    cell's neighbours carry the onset before the cell itself flips. Only
    neighbours observed in the same slot contribute: an unobserved neighbour
    is unknown, never counted as clean. Values at slot *t* use slot *t* only,
    so the result obeys the same leakage rule as the per-cell columns.
    """
    observed = frame.loc[
        frame["degraded_share"].notna(),
        ["h3_cell", "slot_start", "degraded_share", LABEL_COLUMN],
    ]
    pairs = pd.DataFrame(
        [
            (cell, nbr)
            for cell in frame["h3_cell"].unique()
            for nbr in h3.grid_ring(cell, 1)
        ],
        columns=["h3_cell", "nbr_cell"],
    )
    joined = pairs.merge(
        observed.rename(columns={"h3_cell": "nbr_cell"}), on="nbr_cell"
    )
    return joined.groupby(["h3_cell", "slot_start"], as_index=False).agg(
        nbr_observed=("nbr_cell", "size"),
        nbr_degraded_share_mean=("degraded_share", "mean"),
        nbr_degraded_share_max=("degraded_share", "max"),
        nbr_degraded_count=(LABEL_COLUMN, "sum"),
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

    # --- Neighbour-cell context, same slot plus lags on the regular grid ---
    frame = frame.merge(_neighbour_state(frame), on=["h3_cell", "slot_start"], how="left")
    frame["nbr_observed"] = frame["nbr_observed"].fillna(0)
    frame = frame.sort_values(["h3_cell", "slot_start"]).reset_index(drop=True)
    grouped = frame.groupby("h3_cell", sort=False)
    for lag in NEIGHBOUR_LAG_HOURS:
        frame[f"nbr_degraded_share_mean_lag_{lag}h"] = grouped[
            "nbr_degraded_share_mean"
        ].shift(lag)
    # Spreading front: neighbours getting worse while the cell is still clean.
    frame["nbr_degraded_share_delta_3h"] = (
        frame["nbr_degraded_share_mean"] - frame["nbr_degraded_share_mean_lag_3h"]
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
        help="optional Parquet path to also write the table to",
    )
    parser.add_argument(
        "--no-store",
        action="store_true",
        help="build and summarise only, do not write the feature store table",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    # Imported here so `build_features` stays importable, and unit-testable,
    # without a database or a configured DATABASE_URL.
    from datetime import UTC, datetime

    from skyjam.common.db import get_engine, read_cell_hourly, write_features

    engine = get_engine()
    cells = read_cell_hourly(engine)
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

    if not args.no_store:
        run_id = datetime.now(UTC).strftime("features-%Y%m%dT%H%M%SZ")
        write_features(engine, features, run_id=run_id, cell_slots=len(cells))
        logger.info("feature store updated, run %s", run_id)

    if args.out:
        features.to_parquet(args.out, index=False)
        logger.info("wrote %s", args.out)


if __name__ == "__main__":
    main()
