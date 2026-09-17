"""Sampling grid: which points to poll to cover the area of interest.

The API is queried per point with a radius, so the region of interest is covered
by a set of overlapping discs. Points are chosen to cover European airspace plus
the regions where GNSS interference is routinely reported (Baltic, Black Sea,
Eastern Mediterranean), with control regions in Western Europe that let us show
the model is not simply memorising "east is bad".
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SamplePoint:
    name: str
    lat: float
    lon: float
    radius_nm: int = 250
    #: Regions we expect to be quiet; kept explicitly so the class balance and
    #: the false-positive rate can be reported per region.
    control: bool = False


# Roughly 250 NM ~ 463 km radius per disc. Spacing is deliberately tighter than
# the diameter so neighbouring discs overlap and no corridor falls between them.
SAMPLE_POINTS: tuple[SamplePoint, ...] = (
    # --- Reported interference regions ---
    SamplePoint("baltic_kaliningrad", 54.7, 20.5),
    SamplePoint("gulf_of_finland", 59.4, 24.8),
    SamplePoint("baltic_south", 55.5, 16.5),
    SamplePoint("finland_north", 65.5, 26.0),
    SamplePoint("black_sea_west", 44.5, 31.0),
    SamplePoint("black_sea_east", 43.0, 38.0),
    SamplePoint("eastern_med", 34.8, 33.5),
    SamplePoint("levant_north", 36.5, 36.5),
    SamplePoint("caucasus", 41.0, 44.0),
    SamplePoint("poland_east", 52.2, 23.0),
    SamplePoint("romania_moldova", 46.5, 27.5),
    # --- Control regions, expected quiet ---
    SamplePoint("alps_switzerland", 47.0, 8.3, control=True),
    SamplePoint("uk_south", 51.5, -0.5, control=True),
    SamplePoint("iberia_central", 40.4, -3.7, control=True),
    SamplePoint("france_central", 46.5, 2.5, control=True),
    SamplePoint("germany_central", 50.5, 9.5, control=True),
    SamplePoint("italy_central", 42.5, 12.5, control=True),
    SamplePoint("scandinavia_west", 59.9, 10.7, control=True),
)


def active_points(include_controls: bool = True) -> tuple[SamplePoint, ...]:
    if include_controls:
        return SAMPLE_POINTS
    return tuple(p for p in SAMPLE_POINTS if not p.control)
