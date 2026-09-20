"""Tests for the PostgreSQL read path.

The SQL-touching tests need a database and are skipped without one, so the
suite still runs anywhere. The important property they protect is that the
*semantics* live on the read side: raw storage is unfiltered, and the altitude
floor / H3 resolution / thresholds are applied here from `schema.py`.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import text

from skyjam.common.db import (
    _normalise_url,
    get_engine,
    read_cell_hourly,
    read_observations,
    slot_coverage,
)
from skyjam.common.schema import H3_RESOLUTION, MIN_ALTITUDE_FT

DDL = """
CREATE TABLE IF NOT EXISTS observations (
    observed_at   TIMESTAMPTZ  NOT NULL,
    slot_start    TIMESTAMPTZ  NOT NULL,
    hex           TEXT         NOT NULL,
    lat           DOUBLE PRECISION NOT NULL,
    lon           DOUBLE PRECISION NOT NULL,
    altitude_ft   DOUBLE PRECISION,
    nic           SMALLINT     NOT NULL,
    nac_p         SMALLINT,
    sil           SMALLINT,
    aircraft_type TEXT,
    sample_point       TEXT    NOT NULL,
    is_control_region  BOOLEAN NOT NULL,
    ingest_run_id TEXT NOT NULL,
    PRIMARY KEY (slot_start, hex, sample_point, observed_at)
);
"""


def test_normalise_url_handles_railway_scheme():
    """Railway hands out `postgres://`, which SQLAlchemy 2 does not register."""
    assert _normalise_url("postgres://u:p@h:5432/d").startswith("postgresql+psycopg://")


def test_normalise_url_pins_psycopg3():
    """A bare postgresql:// would load psycopg2, which is not a dependency."""
    assert _normalise_url("postgresql://u:p@h:5432/d").startswith("postgresql+psycopg://")


def test_normalise_url_leaves_explicit_driver_alone():
    url = "postgresql+psycopg://u:p@h:5432/d"
    assert _normalise_url(url) == url


@pytest.fixture
def engine():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    eng = get_engine(url)
    with eng.begin() as conn:
        conn.execute(text(DDL))
        conn.execute(text("TRUNCATE observations"))
    return eng


def _insert(engine, rows: list[dict]):
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO observations
                (observed_at, slot_start, hex, lat, lon, altitude_ft, nic,
                 nac_p, sil, aircraft_type, sample_point, is_control_region,
                 ingest_run_id)
                VALUES (:observed_at, :slot_start, :hex, :lat, :lon,
                        :altitude_ft, :nic, :nac_p, :sil, :aircraft_type,
                        :sample_point, :is_control_region, :ingest_run_id)
                """
            ),
            rows,
        )


def _row(hex_id: str, slot: datetime, *, alt: float | None, nic: int = 8, lat=54.7, lon=20.5):
    return {
        "observed_at": slot + timedelta(seconds=37),
        "slot_start": slot,
        "hex": hex_id,
        "lat": lat,
        "lon": lon,
        "altitude_ft": alt,
        "nic": nic,
        "nac_p": 9,
        "sil": 3,
        "aircraft_type": "A320",
        "sample_point": "baltic_kaliningrad",
        "is_control_region": False,
        "ingest_run_id": "test-run",
    }


def test_cruise_filter_is_applied_on_read(engine):
    """The floor lives in schema.py, not in stored data."""
    slot = datetime(2026, 9, 20, 14, tzinfo=UTC)
    _insert(
        engine,
        [
            _row("low", slot, alt=MIN_ALTITUDE_FT - 1),
            _row("high", slot, alt=MIN_ALTITUDE_FT),
            _row("null", slot, alt=None),
        ],
    )

    cruise = read_observations(engine, cruise_only=True)
    assert set(cruise["hex"]) == {"high"}, "only cruise-altitude aircraft qualify"

    everything = read_observations(engine, cruise_only=False)
    assert len(everything) == 3, "raw storage keeps sub-cruise and ground aircraft"


def test_null_altitude_is_excluded_not_imputed(engine):
    """A missing altitude must not be silently treated as ground or as cruise."""
    slot = datetime(2026, 9, 20, 14, tzinfo=UTC)
    _insert(engine, [_row("null", slot, alt=None)])
    assert read_observations(engine, cruise_only=True).empty


def test_time_bounds_are_inclusive(engine):
    slot1 = datetime(2026, 9, 20, 14, tzinfo=UTC)
    slot2 = slot1 + timedelta(hours=1)
    slot3 = slot1 + timedelta(hours=2)
    _insert(
        engine,
        [
            _row("a", slot1, alt=35000),
            _row("b", slot2, alt=35000),
            _row("c", slot3, alt=35000),
        ],
    )
    got = read_observations(engine, start=slot1, end=slot2)
    assert set(got["hex"]) == {"a", "b"}


def test_read_cell_hourly_applies_configured_resolution(engine):
    """Cells are derived at read time, so the resolution comes from schema.py."""
    import h3

    slot = datetime(2026, 9, 20, 14, tzinfo=UTC)
    # Enough aircraft in one place to clear the traffic floor.
    _insert(engine, [_row(f"ac{i}", slot, alt=35000) for i in range(8)])

    cells = read_cell_hourly(engine)
    assert not cells.empty
    assert {h3.get_resolution(c) for c in cells["h3_cell"]} == {H3_RESOLUTION}


def test_slot_coverage_exposes_gaps(engine):
    """A missed hour must appear as a zero row, not vanish.

    Gaps are the defining risk of this project, so they have to be visible
    rather than implied by an absent row.
    """
    slot1 = datetime(2026, 9, 20, 14, tzinfo=UTC)
    slot3 = slot1 + timedelta(hours=2)
    _insert(engine, [_row("a", slot1, alt=35000), _row("c", slot3, alt=35000)])

    cov = slot_coverage(engine)
    assert len(cov) == 3, "the missing middle hour must still be a row"
    gap = cov[cov["slot_start"] == slot1 + timedelta(hours=1)]
    assert len(gap) == 1
    assert int(gap.iloc[0]["observations"]) == 0


def test_empty_database_returns_empty_frame(engine):
    assert read_observations(engine).empty
    assert read_cell_hourly(engine).empty


def test_timestamps_are_utc_aware(engine):
    slot = datetime(2026, 9, 20, 14, tzinfo=UTC)
    _insert(engine, [_row("a", slot, alt=35000)])
    got = read_observations(engine)
    assert isinstance(got["slot_start"].dtype, pd.DatetimeTZDtype)
    assert got["slot_start"].iloc[0] == slot
