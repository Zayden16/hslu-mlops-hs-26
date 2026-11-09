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
import sys

import h3
import pandas as pd

from skyjam.common.schema import (
    BASE_COLUMNS,
    FORECAST_HORIZON_HOURS,
    LABEL_COLUMN,
    LAG_HOURS,
    MIN_AIRCRAFT_PER_CELL,
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
    # Lineage columns are added by the feature store on write; they identify
    # the build, carry no signal, and would leak the build time into a model.
    forbidden = {TARGET_COLUMN, "h3_cell", "slot_start", "feature_version", "feature_run_id"}
    return [c for c in frame.columns if c not in forbidden]


def validate_features(features: pd.DataFrame) -> list[str]:
    """Data-quality gate run before the feature store is overwritten.

    Returns a list of human-readable problems; empty means the table is safe
    to publish. Each check guards a failure mode that would otherwise reach
    training silently.
    """
    problems: list[str] = []
    if features.duplicated(["h3_cell", "slot_start"]).any():
        problems.append("duplicate (h3_cell, slot_start) keys")
    if not set(features[TARGET_COLUMN].unique()) <= {0, 1}:
        problems.append("target is not binary")
    share_cols = [c for c in features.columns if "degraded_share" in c and "delta" not in c]
    for col in share_cols:
        s = features[col].dropna()
        if len(s) and (s.min() < 0 or s.max() > 1):
            problems.append(f"{col} outside [0, 1]")
    if features["degraded_share"].isna().any():
        problems.append("current-slot degraded_share missing on a training row")
    if (features["aircraft_count"] < MIN_AIRCRAFT_PER_CELL).any():
        problems.append("row below the traffic floor")
    rate = features[TARGET_COLUMN].mean()
    if not 0.005 <= rate <= 0.5:
        problems.append(f"positive rate {rate:.3f} outside the plausible 0.5-50% band")
    return problems


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

    from skyjam.common import alerts

    try:
        _run(args)
    except SystemExit as exc:
        if exc.code not in (0, None):
            alerts.send(
                "skyjam features FAILED",
                "The hourly feature build exited with an error; the previous "
                "feature table is still in place. Check: railway logs -s features",
                priority="high",
                tags="rotating_light",
            )
        raise
    except Exception as exc:
        alerts.send(
            "skyjam features CRASHED",
            f"{type(exc).__name__}: {exc}",
            priority="high",
            tags="rotating_light",
        )
        raise


def _run(args: argparse.Namespace) -> None:
    # Imported here so `build_features` stays importable, and unit-testable,
    # without a database or a configured DATABASE_URL.
    from datetime import UTC, datetime

    from skyjam.common import alerts
    from skyjam.common.db import get_engine, read_cell_hourly, write_features

    engine = get_engine()

    # Watchdog for the ingestor, which cannot report its own death. Runs before
    # the build and does not stop it: stale features are still valid features.
    stale = alerts.check_capture(engine)
    if stale:
        logger.error("capture watchdog: %s", stale)
        alerts.send("skyjam capture STALE", stale, priority="urgent", tags="warning,satellite")

    cells = read_cell_hourly(engine)
    if cells.empty:
        logger.error("no observations in the feature store; is the ingest service running?")
        sys.exit(1)

    features = build_features(cells)
    if features.empty:
        logger.error(
            "no rows survived: %d cell-slots exist but none have an observed "
            "state %d hours later yet",
            len(cells),
            FORECAST_HORIZON_HOURS,
        )
        sys.exit(1)

    problems = validate_features(features)
    if problems:
        # Fail the cron run (non-zero exit shows as failed on Railway) and keep
        # the previous table: a bad build must never replace a good one.
        for p in problems:
            logger.error("validation failed: %s", p)
        sys.exit(1)

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
