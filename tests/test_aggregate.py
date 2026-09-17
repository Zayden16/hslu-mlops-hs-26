"""Tests for the aggregation rules that define the label."""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd

from skyjam.common.schema import (
    CELL_DEGRADED_SHARE_THRESHOLD,
    MIN_AIRCRAFT_PER_CELL,
    MIN_ALTITUDE_FT,
)
from skyjam.features.aggregate import aggregate_cells, normalise_aircraft

OBSERVED_AT = datetime(2026, 9, 17, 10, 30, tzinfo=UTC)


def make_aircraft(hex_id: str, nic: int, alt: int = 35_000, lat=59.4, lon=24.8):
    return {
        "hex": hex_id,
        "lat": lat,
        "lon": lon,
        "alt_baro": alt,
        "nic": nic,
        "nac_p": 10,
        "sil": 3,
        "t": "A320",
    }


class TestNormalise:
    def test_keeps_valid_cruise_aircraft(self):
        frame = normalise_aircraft([make_aircraft("abc123", 8)], OBSERVED_AT)
        assert len(frame) == 1
        assert frame.loc[0, "nic"] == 8
        assert frame.loc[0, "h3_cell"].startswith("8")

    def test_drops_low_altitude_general_aviation(self):
        """The GA confounder: cheap avionics report poor NIC everywhere."""
        low = make_aircraft("ga0001", 4, alt=MIN_ALTITUDE_FT - 1)
        assert normalise_aircraft([low], OBSERVED_AT).empty

    def test_drops_ground_traffic(self):
        ground = make_aircraft("gnd001", 5, alt=0)
        ground["alt_baro"] = "ground"
        assert normalise_aircraft([ground], OBSERVED_AT).empty

    def test_drops_aircraft_without_position_or_nic(self):
        no_nic = make_aircraft("xxx001", 8)
        del no_nic["nic"]
        no_pos = make_aircraft("xxx002", 8)
        no_pos["lat"] = None
        assert normalise_aircraft([no_nic, no_pos], OBSERVED_AT).empty

    def test_slot_is_floored_to_the_hour(self):
        frame = normalise_aircraft([make_aircraft("abc123", 8)], OBSERVED_AT)
        assert frame.loc[0, "slot_start"] == datetime(2026, 9, 17, 10, 0, tzinfo=UTC)


class TestAggregate:
    def test_empty_input_returns_empty_frame_with_schema(self):
        result = aggregate_cells(pd.DataFrame())
        assert result.empty
        assert "is_degraded" in result.columns

    def test_sparse_cell_is_dropped(self):
        """Below the traffic floor a cell's share is noise, not signal."""
        aircraft = [
            make_aircraft(f"ac{i:04d}", 4) for i in range(MIN_AIRCRAFT_PER_CELL - 1)
        ]
        observations = normalise_aircraft(aircraft, OBSERVED_AT)
        assert aggregate_cells(observations).empty

    def test_degraded_cell_is_flagged(self):
        # 6 aircraft, 4 degraded => share 0.67, above threshold.
        aircraft = [make_aircraft(f"bad{i:04d}", 4) for i in range(4)]
        aircraft += [make_aircraft(f"ok{i:04d}", 9) for i in range(2)]
        result = aggregate_cells(normalise_aircraft(aircraft, OBSERVED_AT))
        assert len(result) == 1
        assert result.loc[0, "aircraft_count"] == 6
        assert result.loc[0, "is_degraded"] == 1
        assert result.loc[0, "degraded_share"] > CELL_DEGRADED_SHARE_THRESHOLD

    def test_clean_cell_is_not_flagged(self):
        aircraft = [make_aircraft(f"ok{i:04d}", 9) for i in range(8)]
        result = aggregate_cells(normalise_aircraft(aircraft, OBSERVED_AT))
        assert result.loc[0, "is_degraded"] == 0
        assert result.loc[0, "degraded_share"] == 0.0

    def test_boundary_share_counts_as_degraded(self):
        # Exactly 30% degraded (3 of 10) must satisfy the >= threshold.
        aircraft = [make_aircraft(f"bad{i:04d}", 4) for i in range(3)]
        aircraft += [make_aircraft(f"ok{i:04d}", 9) for i in range(7)]
        result = aggregate_cells(normalise_aircraft(aircraft, OBSERVED_AT))
        assert result.loc[0, "degraded_share"] == CELL_DEGRADED_SHARE_THRESHOLD
        assert result.loc[0, "is_degraded"] == 1

    def test_repeated_aircraft_counted_once_per_slot(self):
        """A lingering aircraft seen in many snapshots must not dominate a cell."""
        aircraft = [make_aircraft(f"ok{i:04d}", 9) for i in range(6)]
        observations = normalise_aircraft(aircraft, OBSERVED_AT)
        # Same aircraft observed again 10 minutes later, inside the same hour.
        later = normalise_aircraft(aircraft, OBSERVED_AT.replace(minute=40))
        combined = pd.concat([observations, later], ignore_index=True)

        result = aggregate_cells(combined)
        assert len(result) == 1
        assert result.loc[0, "aircraft_count"] == 6

    def test_worst_nic_per_aircraft_is_kept(self):
        """A brief degradation within the hour is the meaningful event."""
        good = normalise_aircraft(
            [make_aircraft(f"ac{i:04d}", 9) for i in range(6)], OBSERVED_AT
        )
        degraded = normalise_aircraft(
            [make_aircraft(f"ac{i:04d}", 3) for i in range(6)],
            OBSERVED_AT.replace(minute=45),
        )
        result = aggregate_cells(pd.concat([good, degraded], ignore_index=True))
        assert result.loc[0, "degraded_share"] == 1.0
        assert result.loc[0, "is_degraded"] == 1

    def test_distinct_regions_land_in_distinct_cells(self):
        baltic = [make_aircraft(f"bal{i:04d}", 4, lat=59.4, lon=24.8) for i in range(6)]
        alps = [make_aircraft(f"alp{i:04d}", 9, lat=47.0, lon=8.3) for i in range(6)]
        result = aggregate_cells(normalise_aircraft(baltic + alps, OBSERVED_AT))
        assert len(result) == 2
        assert set(result["is_degraded"]) == {0, 1}
