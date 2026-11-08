"""Leakage tests on the real cell-hour pipeline (MS1 review B2).

Unlike test_compute.py, which feeds `build_features` hand-made cell rows, these
tests start from raw aircraft observations and run the exact production path:
`assign_cells` -> `aggregate_cells` -> `build_features`. A small known example
makes the expected values computable by hand.
"""

from __future__ import annotations

import h3
import numpy as np
import pandas as pd
import pytest

from skyjam.common.schema import FORECAST_HORIZON_HOURS, H3_RESOLUTION, TARGET_COLUMN
from skyjam.features.aggregate import aggregate_cells, assign_cells
from skyjam.features.compute import build_features

LAT, LON = 59.5, 24.5  # Gulf of Finland
CELL = h3.latlng_to_cell(LAT, LON, H3_RESOLUTION)
NEIGHBOUR = sorted(h3.grid_ring(CELL, 1))[0]
NBR_LAT, NBR_LON = h3.cell_to_latlng(NEIGHBOUR)
T0 = pd.Timestamp("2026-10-01 00:00", tz="UTC")


def raw_hour(hour: int, degraded: int, lat=LAT, lon=LON, n: int = 10, tag="a"):
    """`n` cruise aircraft seen in one hour, `degraded` of them with NIC 0."""
    observed_at = T0 + pd.Timedelta(hours=hour, minutes=5)
    return pd.DataFrame(
        {
            "observed_at": observed_at,
            "hex": [f"{tag}{i:05d}" for i in range(n)],
            "lat": lat,
            "lon": lon,
            "altitude_ft": 35_000.0,
            "nic": [0 if i < degraded else 8 for i in range(n)],
            "nac_p": 9,
        }
    )


def pipeline(raw: pd.DataFrame) -> pd.DataFrame:
    return build_features(aggregate_cells(assign_cells(raw)))


def share_series(n_hours=48, skip=(), pattern=None):
    pattern = pattern or (lambda h: h % 10)
    return pd.concat(
        [raw_hour(h, pattern(h)) for h in range(n_hours) if h not in skip],
        ignore_index=True,
    )


def row_at(features, hour, cell=CELL):
    sel = features[
        (features["h3_cell"] == cell)
        & (features["slot_start"] == T0 + pd.Timedelta(hours=hour))
    ]
    assert len(sel) == 1, f"expected one row at hour {hour}"
    return sel.iloc[0]


def test_lag_never_spans_a_missing_hour():
    # Hour 20 was never polled. The 1 h lag at hour 21 must be unknown, not
    # the value from hour 19.
    features = pipeline(share_series(skip={20}))
    assert np.isnan(row_at(features, 21)["degraded_share_lag_1h"])
    assert row_at(features, 21)["degraded_share_lag_2h"] == pytest.approx(0.9)
    # The missing hour itself is never a training row.
    assert not (features["slot_start"] == T0 + pd.Timedelta(hours=20)).any()


def test_rolling_window_excludes_the_present_slot():
    features = pipeline(share_series())
    row = row_at(features, 30)
    expected = np.mean([(h % 10) / 10 for h in range(24, 30)])
    assert row["degraded_share_mean_6h"] == pytest.approx(expected)


def test_no_feature_at_t_uses_data_after_t():
    # Perturb every hour after T. Every feature at T must stay identical;
    # only the target (which is meant to look ahead) may change.
    base = pipeline(share_series())
    t = 25
    perturbed = pipeline(share_series(pattern=lambda h: h % 10 if h <= t else 10))
    a, b = row_at(base, t), row_at(perturbed, t)
    cols = [c for c in base.columns if c != TARGET_COLUMN]
    pd.testing.assert_series_equal(a[cols], b[cols], check_names=False)


def test_target_comes_from_t_plus_horizon():
    # Clean except hour 30, where 6 of 10 aircraft are degraded.
    features = pipeline(share_series(pattern=lambda h: 6 if h == 30 else 0))
    assert row_at(features, 30 - FORECAST_HORIZON_HOURS)[TARGET_COLUMN] == 1
    assert row_at(features, 30 - FORECAST_HORIZON_HOURS - 1)[TARGET_COLUMN] == 0


def test_traffic_floor_drops_thin_cells_instead_of_calling_them_clean():
    raw = pd.concat(
        [raw_hour(h, 0) for h in range(30)] + [raw_hour(30, 0, n=3, tag="t")],
        ignore_index=True,
    )
    cells = aggregate_cells(assign_cells(raw))
    assert not (cells["slot_start"] == T0 + pd.Timedelta(hours=30)).any()


class TestNeighbours:
    def build(self, nbr_pattern, skip_nbr=()):
        own = share_series(pattern=lambda h: 0)
        nbr = pd.concat(
            [
                raw_hour(h, nbr_pattern(h), lat=NBR_LAT, lon=NBR_LON, tag="n")
                for h in range(48)
                if h not in skip_nbr
            ],
            ignore_index=True,
        )
        return pipeline(pd.concat([own, nbr], ignore_index=True))

    def test_neighbour_state_is_seen_by_the_cell(self):
        features = self.build(lambda h: 8 if h >= 20 else 0)
        row = row_at(features, 22)
        assert row["nbr_observed"] == 1
        assert row["nbr_degraded_share_mean"] == pytest.approx(0.8)
        assert row["nbr_degraded_count"] == 1
        assert row_at(features, 19)["nbr_degraded_share_mean"] == 0
        assert row_at(features, 22)["nbr_degraded_share_delta_3h"] == pytest.approx(0.8)

    def test_unobserved_neighbour_is_unknown_not_clean(self):
        features = self.build(lambda h: 8, skip_nbr={25})
        row = row_at(features, 25)
        assert row["nbr_observed"] == 0
        assert np.isnan(row["nbr_degraded_share_mean"])
        assert np.isnan(row_at(features, 26)["nbr_degraded_share_mean_lag_1h"])

    def test_neighbour_features_do_not_look_ahead(self):
        t = 25
        a = row_at(self.build(lambda h: 2), t)
        b = row_at(self.build(lambda h: 2 if h <= t else 9), t)
        cols = [c for c in a.index if c.startswith("nbr_")]
        pd.testing.assert_series_equal(a[cols], b[cols], check_names=False)
