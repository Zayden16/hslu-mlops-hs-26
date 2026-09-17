"""Single source of truth for the label and feature semantics.

Both the training pipeline and the inference pipeline import from here. If a
threshold changes, it changes in exactly one place.
"""

from __future__ import annotations

from typing import Final

# --- Label definition -------------------------------------------------------

# NIC (Navigation Integrity Category) is broadcast by the aircraft itself and
# describes the containment radius of its own position solution. Under GNSS
# jamming or spoofing the value collapses while the airframe is otherwise
# healthy. NIC < 7 means a containment radius worse than 0.2 NM, which is the
# threshold gpsjam.org uses to call a position "degraded".
NIC_DEGRADED_THRESHOLD: Final[int] = 7

# Empirically (see notebooks/01_signal_validation) low-altitude general aviation
# carries cheap avionics that report poor NIC everywhere, which swamps the
# jamming signal. Restricting to cruise altitude removes that confounder:
# Switzerland drops from 7.9% to 1.2% degraded, while Estonia stays above 50%.
MIN_ALTITUDE_FT: Final[int] = 20_000

# A cell needs a minimum number of independent aircraft before its degraded
# share is meaningful; otherwise one bad airframe creates a 100% "outage".
MIN_AIRCRAFT_PER_CELL: Final[int] = 5

# A cell counts as interference-affected when this share of qualifying aircraft
# report degraded navigation integrity.
CELL_DEGRADED_SHARE_THRESHOLD: Final[float] = 0.30

# --- Spatial / temporal grid ------------------------------------------------

# H3 resolution 2 gives cells of roughly 180 km edge length. This was chosen
# empirically, not by taste: on a real snapshot of 2272 cruise-level
# observations the resolutions compare as
#
#   res  edge_km  usable cells (>=5 aircraft)  median aircraft/cell  degraded cells
#    1     483             18                          32                  2
#    2     183             67                          10                  3
#    3      69            133                           3                  0
#    4      26             20                           1                  0
#
# At res 3 and finer the traffic floor eliminates exactly the interference
# cells we care about: the Gulf of Finland disc shows 52% degraded aircraft,
# but spread over 20 cells no single cell clears the minimum. Res 2 keeps the
# signal while still localising a jammer to a useful footprint.
H3_RESOLUTION: Final[int] = 2

# Observations are bucketed into hourly slots before aggregation.
SLOT_MINUTES: Final[int] = 60

# Prediction horizon: how many hourly slots ahead we forecast.
FORECAST_HORIZON_HOURS: Final[int] = 6

# Lags offered to the model, in hours. 168 = same hour one week earlier.
LAG_HOURS: Final[tuple[int, ...]] = (1, 2, 3, 6, 12, 24, 168)

# --- Feature columns --------------------------------------------------------

KEY_COLUMNS: Final[tuple[str, ...]] = ("h3_cell", "slot_start")

BASE_COLUMNS: Final[tuple[str, ...]] = (
    "aircraft_count",
    "degraded_share",
    "median_nic",
    "p25_nic",
    "mean_nac_p",
)

LABEL_COLUMN: Final[str] = "is_degraded"
TARGET_COLUMN: Final[str] = "target_is_degraded"
