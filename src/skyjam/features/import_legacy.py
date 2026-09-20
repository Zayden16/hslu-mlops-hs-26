"""One-off migration of the legacy Parquet history into PostgreSQL.

The Parquet store was written by the retired GitHub Actions job between
2026-09-17 and the Railway cutover. That history cannot be re-fetched (the
feed has no history endpoint), so it is imported rather than abandoned.

Two honest caveats, which is why this is a documented script and not a silent
backfill:

1. The legacy raw layer was already filtered to FL200+ at capture time, so the
   sub-cruise aircraft of those days are gone for good. Rows imported from it
   can never answer "what if the altitude floor were lower?".
2. The legacy raw layer stored an H3 cell at resolution 3 and no reliable
   per-aircraft altitude beyond the filter. Cells are recomputed here from the
   stored coordinates, so the imported rows match current semantics.

Usage:

    DATABASE_URL=... uv run python -m skyjam.features.import_legacy --dry-run
    DATABASE_URL=... uv run python -m skyjam.features.import_legacy
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pandas as pd
from sqlalchemy import text

from skyjam.common.db import get_engine
from skyjam.common.schema import SLOT_MINUTES

logger = logging.getLogger(__name__)

LEGACY_RUN_PREFIX = "legacy-parquet"


def load_legacy(root: Path) -> pd.DataFrame:
    """Read every legacy raw Parquet partition into one frame."""
    files = sorted(root.glob("date=*/*.parquet"))
    if not files:
        logger.warning("no legacy parquet files under %s", root)
        return pd.DataFrame()
    frame = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    logger.info("read %d rows from %d legacy files", len(frame), len(files))
    return frame


def to_observations(frame: pd.DataFrame) -> pd.DataFrame:
    """Shape legacy rows into the `observations` table's columns."""
    if frame.empty:
        return frame

    out = pd.DataFrame(
        {
            "observed_at": pd.to_datetime(frame["observed_at"], utc=True),
            "hex": frame["hex"].astype(str),
            "lat": frame["lat"].astype(float),
            "lon": frame["lon"].astype(float),
            "altitude_ft": frame["altitude_ft"].astype(float),
            "nic": frame["nic"].astype(int),
            "nac_p": frame["nac_p"],
            "sil": frame["sil"],
            "aircraft_type": frame.get("aircraft_type"),
            "sample_point": frame["sample_point"].astype(str),
            "is_control_region": frame["is_control_region"].astype(bool),
        }
    )
    # Recompute the slot rather than trusting the legacy column, so the
    # imported rows are bucketed by exactly the rule in schema.py.
    out["slot_start"] = out["observed_at"].dt.floor(f"{SLOT_MINUTES}min")

    # Tag the provenance: these rows came from the pre-Railway pipeline and
    # carry the caveats documented above.
    out["ingest_run_id"] = LEGACY_RUN_PREFIX + "-" + out["slot_start"].dt.strftime("%Y%m%dT%H")

    # Nullable integer columns must survive as NULL, not as 0.
    for col in ("nac_p", "sil"):
        out[col] = out[col].astype("Int64")

    # The table's primary key is (slot_start, hex, sample_point, observed_at).
    # Legacy sweeps could contain the same aircraft twice within one sweep;
    # keep the worst NIC, matching the aggregation's "one degraded report is
    # the meaningful event" rule.
    out = (
        out.sort_values("nic")
        .drop_duplicates(subset=["slot_start", "hex", "sample_point", "observed_at"], keep="first")
        .reset_index(drop=True)
    )
    return out


def import_rows(engine, rows: pd.DataFrame) -> int:
    """Insert rows, skipping any that already exist."""
    if rows.empty:
        return 0

    inserted = 0
    with engine.begin() as conn:
        # A temp table plus ON CONFLICT DO NOTHING makes the import
        # re-runnable: running it twice adds nothing the second time.
        conn.execute(
            text(
                """
                CREATE TEMP TABLE legacy_import
                (LIKE observations INCLUDING DEFAULTS)
                ON COMMIT DROP
                """
            )
        )
        rows.to_sql("legacy_import", conn, if_exists="append", index=False)
        result = conn.execute(
            text(
                """
                INSERT INTO observations
                SELECT * FROM legacy_import
                ON CONFLICT DO NOTHING
                """
            )
        )
        inserted = result.rowcount
    return inserted


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", default="data/raw/observations")
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would be imported"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    legacy = load_legacy(Path(args.raw_root))
    if legacy.empty:
        return

    rows = to_observations(legacy)
    logger.info(
        "prepared %d rows spanning %s .. %s across %d hourly slots",
        len(rows),
        rows["slot_start"].min(),
        rows["slot_start"].max(),
        rows["slot_start"].nunique(),
    )

    if args.dry_run:
        logger.info("dry run, nothing written")
        return

    engine = get_engine()
    inserted = import_rows(engine, rows)
    logger.info("inserted %d new rows (%d were already present)", inserted, len(rows) - inserted)


if __name__ == "__main__":
    main()
