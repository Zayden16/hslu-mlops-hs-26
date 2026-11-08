"""Read path for the PostgreSQL feature store.

The Go ingest service (`services/ingestor`) is the only writer. It stores raw
observations *unfiltered*: every aircraft with a position and a reported NIC,
with raw coordinates and no H3 column.

All of the modelling semantics therefore live here, on the read side, where
`schema.py` is the single source of truth shared by training and inference:

* the cruise-altitude floor,
* the H3 resolution,
* the NIC threshold and the per-cell degraded share,
* the minimum traffic per cell.

That is what makes the raw layer replayable: changing a threshold is a code
change plus a re-read, not a re-collection of data that cannot be re-collected.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


def _normalise_url(url: str) -> str:
    """Normalise the URL Railway injects into a SQLAlchemy + psycopg3 DSN.

    Two fixes are needed:

    1. Railway hands out the `postgres://` scheme, which SQLAlchemy 2 does not
       register at all.
    2. A bare `postgresql://` makes SQLAlchemy load the psycopg2 driver, which
       this project does not depend on. Pinning `+psycopg` selects psycopg 3.
    """
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def get_engine(database_url: str | None = None) -> Engine:
    """Build an engine from the argument or DATABASE_URL."""
    url = database_url or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Point it at the Railway Postgres instance "
            "(`railway run` injects it automatically)."
        )
    # pool_pre_ping avoids handing out connections that the server has already
    # closed, which happens with a long-lived notebook session.
    return create_engine(_normalise_url(url), pool_pre_ping=True)


def read_observations(
    engine: Engine,
    start: datetime | None = None,
    end: datetime | None = None,
    cruise_only: bool = True,
) -> pd.DataFrame:
    """Read raw observations, optionally bounded by slot.

    `cruise_only` applies the altitude floor from `schema.py` in SQL rather
    than in pandas, because the raw table is large and the floor typically
    removes about half the rows.
    """
    from skyjam.common.schema import MIN_ALTITUDE_FT

    clauses: list[str] = []
    params: dict[str, object] = {}

    if start is not None:
        clauses.append("slot_start >= :start")
        params["start"] = start
    if end is not None:
        clauses.append("slot_start <= :end")
        params["end"] = end
    if cruise_only:
        # NULL altitude means "ground" or "not reported", and neither can be
        # treated as cruise, so the NULL is excluded rather than imputed.
        clauses.append("altitude_ft >= :min_alt")
        params["min_alt"] = MIN_ALTITUDE_FT

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT observed_at, slot_start, hex, lat, lon, altitude_ft,
               nic, nac_p, sil, aircraft_type, sample_point, is_control_region
        FROM observations
        {where}
        ORDER BY slot_start, hex
    """  # noqa: S608 - clauses are built from a fixed allow-list, values are bound

    frame = pd.read_sql(text(sql), engine, params=params)
    if frame.empty:
        logger.warning("no observations matched the requested range")
        return frame

    for col in ("observed_at", "slot_start"):
        frame[col] = pd.to_datetime(frame[col], utc=True)
    return frame


def read_cell_hourly(
    engine: Engine,
    start: datetime | None = None,
    end: datetime | None = None,
) -> pd.DataFrame:
    """Aggregate raw observations into per-cell hourly rows.

    This reuses `features.aggregate.aggregate_cells`, the exact function the
    unit tests cover and the inference pipeline calls on a live snapshot, so
    the stored history and a freshly pulled snapshot are aggregated by
    identical code.
    """
    from skyjam.features.aggregate import aggregate_cells, assign_cells

    observations = read_observations(engine, start=start, end=end, cruise_only=True)
    if observations.empty:
        return pd.DataFrame()
    return aggregate_cells(assign_cells(observations))


def ingest_run_summary(engine: Engine, limit: int = 200) -> pd.DataFrame:
    """Recent sweeps, for monitoring coverage.

    Coverage is the metric that actually matters for this project: an hour
    that was never polled is permanently lost, so gaps need to be visible.
    """
    sql = """
        SELECT run_id, slot_start, started_at, finished_at,
               points_attempted, points_succeeded, aircraft_written,
               status, error
        FROM ingest_runs
        ORDER BY slot_start DESC
        LIMIT :limit
    """
    frame = pd.read_sql(text(sql), engine, params={"limit": limit})
    for col in ("slot_start", "started_at", "finished_at"):
        if col in frame:
            frame[col] = pd.to_datetime(frame[col], utc=True)
    return frame


def slot_coverage(engine: Engine) -> pd.DataFrame:
    """Per-hour coverage of the observation history, including gaps.

    A missing hour shows up as a row with zero observations rather than as an
    absent row, so the gaps the model has to cope with are explicit.
    """
    sql = """
        WITH bounds AS (
            SELECT MIN(slot_start) AS lo, MAX(slot_start) AS hi FROM observations
        ),
        slots AS (
            SELECT generate_series(lo, hi, INTERVAL '1 hour') AS slot_start
            FROM bounds
        )
        SELECT s.slot_start,
               COUNT(o.hex)                AS observations,
               COUNT(DISTINCT o.hex)       AS aircraft,
               COUNT(DISTINCT o.sample_point) AS points
        FROM slots s
        LEFT JOIN observations o ON o.slot_start = s.slot_start
        GROUP BY s.slot_start
        ORDER BY s.slot_start
    """
    frame = pd.read_sql(text(sql), engine)
    if not frame.empty:
        frame["slot_start"] = pd.to_datetime(frame["slot_start"], utc=True)
    return frame


# --- Feature store write path ------------------------------------------------

# Bumped whenever a feature definition or threshold in schema.py changes, so a
# training run can name exactly which feature semantics it was trained on.
FEATURE_VERSION = "v2-neighbours"

FEATURE_TABLE = "cell_hourly_features"
FEATURE_RUNS_TABLE = "feature_runs"

FEATURE_RUNS_DDL = f"""
CREATE TABLE IF NOT EXISTS {FEATURE_RUNS_TABLE} (
    run_id           TEXT PRIMARY KEY,
    feature_version  TEXT NOT NULL,
    built_at         TIMESTAMPTZ NOT NULL,
    first_slot       TIMESTAMPTZ,
    last_slot        TIMESTAMPTZ,
    cell_slots       INT NOT NULL,
    labelled_rows    INT NOT NULL,
    positive_rate    DOUBLE PRECISION,
    status           TEXT NOT NULL
)
"""


def write_features(
    engine: Engine,
    features: pd.DataFrame,
    run_id: str,
    cell_slots: int,
) -> None:
    """Replace the materialised feature table and record the build.

    The feature table is a pure function of the raw observations and
    `schema.py`, so every run rebuilds it from the full history instead of
    appending. Writing into a staging table and swapping it in one
    transaction means a reader never sees a half-written table, and a
    failed build leaves the previous version in place.
    """
    from datetime import UTC
    from datetime import datetime as dt

    staging = f"{FEATURE_TABLE}_staging"
    out = features.copy()
    out["feature_version"] = FEATURE_VERSION
    out["feature_run_id"] = run_id

    with engine.begin() as conn:
        conn.execute(text(FEATURE_RUNS_DDL))
        conn.execute(text(f"DROP TABLE IF EXISTS {staging}"))
    # Postgres caps a statement at 65535 bind parameters; size chunks by width.
    chunk = max(1, 60_000 // max(1, len(out.columns)))
    out.to_sql(staging, engine, index=False, chunksize=chunk, method="multi")
    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {FEATURE_TABLE}"))
        conn.execute(text(f"ALTER TABLE {staging} RENAME TO {FEATURE_TABLE}"))
        conn.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS {FEATURE_TABLE}_slot_idx "
                f"ON {FEATURE_TABLE} (slot_start)"
            )
        )
        conn.execute(
            text(
                f"""
                INSERT INTO {FEATURE_RUNS_TABLE}
                (run_id, feature_version, built_at, first_slot, last_slot,
                 cell_slots, labelled_rows, positive_rate, status)
                VALUES (:run_id, :v, :built_at, :first, :last, :cells, :rows,
                        :pos, 'succeeded')
                """
            ),
            {
                "run_id": run_id,
                "v": FEATURE_VERSION,
                "built_at": dt.now(UTC),
                "first": out["slot_start"].min() if len(out) else None,
                "last": out["slot_start"].max() if len(out) else None,
                "cells": cell_slots,
                "rows": len(out),
                "pos": float(out["target_is_degraded"].mean()) if len(out) else None,
            },
        )


def read_features(engine: Engine) -> pd.DataFrame:
    """Read the materialised feature table, the entry point for training."""
    frame = pd.read_sql(text(f"SELECT * FROM {FEATURE_TABLE}"), engine)  # noqa: S608
    if not frame.empty:
        frame["slot_start"] = pd.to_datetime(frame["slot_start"], utc=True)
    return frame
