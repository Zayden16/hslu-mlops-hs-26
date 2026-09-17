"""Turn raw aircraft observations into per-cell, per-hour observations.

This module is deliberately free of I/O so that the aggregation rules can be
unit tested directly, and so the exact same code path can be reused by the
inference pipeline on a freshly pulled snapshot.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import h3
import numpy as np
import pandas as pd

from skyjam.common.schema import (
    CELL_DEGRADED_SHARE_THRESHOLD,
    H3_RESOLUTION,
    MIN_AIRCRAFT_PER_CELL,
    MIN_ALTITUDE_FT,
    NIC_DEGRADED_THRESHOLD,
    SLOT_MINUTES,
)


def _altitude_ft(value: Any) -> float | None:
    """ADS-B reports `alt_baro` as a number or the literal string 'ground'."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def normalise_aircraft(
    aircraft: list[dict[str, Any]], observed_at: datetime
) -> pd.DataFrame:
    """Flatten raw API aircraft records into the columns we care about.

    Only aircraft with a usable position, an altitude at or above cruise, and a
    reported NIC are kept. Everything else cannot contribute to the label.
    """
    rows: list[dict[str, Any]] = []
    for ac in aircraft:
        lat, lon, nic = ac.get("lat"), ac.get("lon"), ac.get("nic")
        if lat is None or lon is None or nic is None:
            continue
        altitude = _altitude_ft(ac.get("alt_baro"))
        if altitude is None or altitude < MIN_ALTITUDE_FT:
            continue
        rows.append(
            {
                "observed_at": observed_at,
                "hex": ac.get("hex"),
                "lat": float(lat),
                "lon": float(lon),
                "altitude_ft": altitude,
                "nic": int(nic),
                "nac_p": ac.get("nac_p"),
                "sil": ac.get("sil"),
                "aircraft_type": ac.get("t"),
            }
        )

    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    frame["h3_cell"] = [
        h3.latlng_to_cell(lat, lon, H3_RESOLUTION)
        for lat, lon in zip(frame["lat"], frame["lon"], strict=True)
    ]
    frame["slot_start"] = frame["observed_at"].dt.floor(f"{SLOT_MINUTES}min")
    return frame


def aggregate_cells(observations: pd.DataFrame) -> pd.DataFrame:
    """Aggregate aircraft observations into one row per (cell, hourly slot).

    Each aircraft is counted once per slot even if several snapshots caught it,
    so that a slow-moving aircraft lingering in one cell cannot dominate the
    degraded share of that cell.
    """
    expected = [
        "h3_cell",
        "slot_start",
        "aircraft_count",
        "degraded_share",
        "median_nic",
        "p25_nic",
        "mean_nac_p",
        "is_degraded",
    ]
    if observations.empty:
        return pd.DataFrame(columns=expected)

    # Deduplicate: one row per aircraft per cell per slot, keeping its worst
    # reported NIC, because a single degraded report is the meaningful event.
    deduped = (
        observations.sort_values("nic")
        .groupby(["h3_cell", "slot_start", "hex"], as_index=False)
        .first()
    )

    deduped["is_degraded_ac"] = deduped["nic"] < NIC_DEGRADED_THRESHOLD

    grouped = deduped.groupby(["h3_cell", "slot_start"], as_index=False).agg(
        aircraft_count=("hex", "nunique"),
        degraded_share=("is_degraded_ac", "mean"),
        median_nic=("nic", "median"),
        p25_nic=("nic", lambda s: float(np.percentile(s, 25))),
        mean_nac_p=("nac_p", "mean"),
    )

    # Cells with too little traffic carry no reliable signal. They are dropped
    # rather than imputed: an unobserved cell is not a quiet cell.
    grouped = grouped[grouped["aircraft_count"] >= MIN_AIRCRAFT_PER_CELL].copy()

    grouped["is_degraded"] = (
        grouped["degraded_share"] >= CELL_DEGRADED_SHARE_THRESHOLD
    ).astype(int)
    return grouped[expected].reset_index(drop=True)


def snapshot_to_cells(
    aircraft: list[dict[str, Any]], observed_at: datetime | None = None
) -> pd.DataFrame:
    """Convenience path used by the inference pipeline: raw JSON to cell rows."""
    observed_at = observed_at or datetime.now(UTC)
    return aggregate_cells(normalise_aircraft(aircraft, observed_at))
