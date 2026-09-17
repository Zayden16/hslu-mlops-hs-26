"""Parquet-backed store, partitioned by UTC date.

This is the "poor man's feature store" the course allows: versioned Parquet
under a stable URI, local for development and a bucket in the cloud. The read
path is point-in-time correct because every row carries the slot it describes
and partitions are append-only.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)


def _partition_dir(root: str | Path, dataset: str, day: datetime) -> Path:
    return Path(root) / dataset / f"date={day.strftime('%Y-%m-%d')}"


def write_partition(
    frame: pd.DataFrame,
    root: str | Path,
    dataset: str,
    day: datetime | None = None,
    run_id: str | None = None,
) -> Path:
    """Append a batch as its own Parquet file inside the day's partition.

    Files are never overwritten: each run writes a uniquely named file, so a
    re-run or a backfill cannot silently destroy earlier observations.
    """
    if frame.empty:
        logger.info("nothing to write for dataset=%s", dataset)
        return Path(root) / dataset

    day = day or datetime.now(UTC)
    run_id = run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    target_dir = _partition_dir(root, dataset, day)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"part-{run_id}.parquet"
    frame.to_parquet(path, index=False)
    logger.info("wrote %d rows to %s", len(frame), path)
    return path


def read_dataset(root: str | Path, dataset: str) -> pd.DataFrame:
    """Read every partition of a dataset into one frame."""
    base = Path(root) / dataset
    files = sorted(base.glob("date=*/*.parquet"))
    if not files:
        logger.warning("no parquet files under %s", base)
        return pd.DataFrame()
    frames = [pd.read_parquet(f) for f in files]
    return pd.concat(frames, ignore_index=True)
