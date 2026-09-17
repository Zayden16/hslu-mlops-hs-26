"""Tests for the feature table, focused on leakage and gap handling.

These are the tests that matter most: a silent leak would produce an
excellent-looking model that cannot work in production.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from skyjam.common.schema import FORECAST_HORIZON_HOURS, TARGET_COLUMN
from skyjam.features.compute import build_features, feature_columns


def make_cells(n_hours: int = 72, cell: str = "831f1dfffffffff", pattern=None):
    """Synthetic per-cell hourly observations."""
    slots = pd.date_range("2026-09-01", periods=n_hours, freq="h", tz="UTC")
    if pattern is None:
        pattern = [0.0] * n_hours
    return pd.DataFrame(
        {
            "h3_cell": cell,
            "slot_start": slots,
            "aircraft_count": 20,
            "degraded_share": pattern,
            "median_nic": [8 if p < 0.3 else 4 for p in pattern],
            "p25_nic": [7 if p < 0.3 else 3 for p in pattern],
            "mean_nac_p": 9.0,
            "is_degraded": [int(p >= 0.3) for p in pattern],
        }
    )


class TestTarget:
    def test_target_is_the_future_state(self):
        # Degraded only in hours 40..49.
        pattern = [0.9 if 40 <= i < 50 else 0.0 for i in range(72)]
        features = build_features(make_cells(pattern=pattern))

        row = features[features["slot_start"] == pd.Timestamp("2026-09-02 16:00", tz="UTC")]
        # 16:00 is hour 40 of the series; target looks HORIZON hours ahead.
        assert not row.empty
        source = make_cells(pattern=pattern)
        future_slot = pd.Timestamp("2026-09-02 16:00", tz="UTC") + pd.Timedelta(
            hours=FORECAST_HORIZON_HOURS
        )
        expected = source.loc[source["slot_start"] == future_slot, "is_degraded"].iloc[0]
        assert row[TARGET_COLUMN].iloc[0] == expected

    def test_last_rows_without_future_are_dropped(self):
        features = build_features(make_cells(n_hours=48))
        latest_kept = features["slot_start"].max()
        source_latest = pd.Timestamp("2026-09-01", tz="UTC") + pd.Timedelta(hours=47)
        assert latest_kept <= source_latest - pd.Timedelta(hours=FORECAST_HORIZON_HOURS)

    def test_target_is_integer_and_binary(self):
        pattern = [0.9 if i % 5 == 0 else 0.0 for i in range(72)]
        features = build_features(make_cells(pattern=pattern))
        assert features[TARGET_COLUMN].dtype.kind in "iu"
        assert set(features[TARGET_COLUMN].unique()) <= {0, 1}


class TestNoLeakage:
    def test_feature_columns_exclude_the_target(self):
        features = build_features(make_cells())
        assert TARGET_COLUMN not in feature_columns(features)

    def test_lag_features_come_from_the_past(self):
        # A single degraded hour at index 30.
        pattern = [0.9 if i == 30 else 0.0 for i in range(72)]
        features = build_features(make_cells(pattern=pattern)).set_index("slot_start")

        spike = pd.Timestamp("2026-09-01", tz="UTC") + pd.Timedelta(hours=30)
        one_hour_later = spike + pd.Timedelta(hours=1)

        # The hour after the spike must see it in its 1h lag...
        assert features.loc[one_hour_later, "degraded_share_lag_1h"] == 0.9
        # ...and the spike hour itself must not see its own value as a lag.
        assert features.loc[spike, "degraded_share_lag_1h"] == 0.0

    def test_rolling_mean_excludes_the_current_hour(self):
        pattern = [0.0] * 40 + [1.0] + [0.0] * 31
        features = build_features(make_cells(pattern=pattern)).set_index("slot_start")
        spike = pd.Timestamp("2026-09-01", tz="UTC") + pd.Timedelta(hours=40)
        # The 6h rolling mean at the spike hour is shifted, so it predates it.
        assert features.loc[spike, "degraded_share_mean_6h"] == 0.0


class TestGapHandling:
    def test_lags_are_temporal_not_positional(self):
        """The failure mode called out in the course slides.

        With rows missing, a positional shift(1) would treat a 3-hour-old
        observation as if it were 1 hour old. Reindexing onto a regular grid
        must instead yield NaN for the unobserved lag.
        """
        cells = make_cells(n_hours=72)
        # Drop hours 10, 11, 12 to simulate skipped cron runs.
        keep = ~cells["slot_start"].isin(
            [
                pd.Timestamp("2026-09-01", tz="UTC") + pd.Timedelta(hours=h)
                for h in (10, 11, 12)
            ]
        )
        features = build_features(cells[keep]).set_index("slot_start")

        hour_13 = pd.Timestamp("2026-09-01", tz="UTC") + pd.Timedelta(hours=13)
        # Hour 13's "1 hour ago" is hour 12, which was never observed.
        assert np.isnan(features.loc[hour_13, "degraded_share_lag_1h"])

    def test_unobserved_current_slot_is_not_a_training_row(self):
        cells = make_cells(n_hours=72)
        missing = pd.Timestamp("2026-09-01", tz="UTC") + pd.Timedelta(hours=20)
        features = build_features(cells[cells["slot_start"] != missing])
        assert missing not in set(features["slot_start"])

    def test_multiple_ingest_runs_in_one_hour_are_collapsed(self):
        cells = make_cells(n_hours=72)
        duplicated = pd.concat([cells, cells], ignore_index=True)
        assert len(build_features(duplicated)) == len(build_features(cells))


class TestMultiCell:
    def test_cells_do_not_bleed_into_each_other(self):
        """Lags must be computed within a cell, never across the boundary."""
        quiet = make_cells(n_hours=72, cell="831f1dfffffffff", pattern=[0.0] * 72)
        loud = make_cells(n_hours=72, cell="831f1efffffffff", pattern=[0.9] * 72)
        features = build_features(pd.concat([quiet, loud], ignore_index=True))

        quiet_rows = features[features["h3_cell"] == "831f1dfffffffff"]
        # A quiet cell must never inherit the loud cell's history.
        assert quiet_rows["degraded_share_lag_1h"].dropna().max() == 0.0
        assert quiet_rows[TARGET_COLUMN].max() == 0

    def test_empty_input_returns_empty(self):
        assert build_features(pd.DataFrame()).empty
