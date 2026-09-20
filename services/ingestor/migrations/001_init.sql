-- skyjam storage schema.
--
-- Design rule: this database stores *observations*, not judgements. The label
-- semantics (altitude floor, NIC threshold, H3 resolution, traffic floor) live
-- in src/skyjam/common/schema.py and are applied at read time. Nothing here
-- bakes a threshold into stored data, because the whole point of keeping raw
-- is being able to change those thresholds later and rebuild.
--
-- This is deliberately different from the original Parquet layout, which
-- filtered to FL200+ and computed H3 res-3 *before* writing, making the
-- documented backfill guarantee impossible to honour.

CREATE TABLE IF NOT EXISTS observations (
    -- Identity of one aircraft seen in one sweep.
    observed_at   TIMESTAMPTZ  NOT NULL,
    slot_start    TIMESTAMPTZ  NOT NULL,
    hex           TEXT         NOT NULL,

    -- Position, stored as raw coordinates. No H3 column on purpose: the
    -- resolution is a modelling decision and must stay revisitable.
    lat           DOUBLE PRECISION NOT NULL,
    lon           DOUBLE PRECISION NOT NULL,

    -- altitude_ft is NULL when the aircraft reported "ground" or omitted it.
    -- Stored unfiltered so the cruise-altitude floor can be changed later.
    altitude_ft   DOUBLE PRECISION,

    -- Navigation quality, the actual signal this project is built on.
    nic           SMALLINT     NOT NULL,
    nac_p         SMALLINT,
    sil           SMALLINT,

    aircraft_type TEXT,

    -- Provenance: which disc caught it, and whether that disc is a control
    -- region. Kept so per-region false-positive rates stay reportable.
    sample_point       TEXT    NOT NULL,
    is_control_region  BOOLEAN NOT NULL,

    ingest_run_id TEXT NOT NULL,

    -- Discs overlap by design, so the same airframe is seen by several points
    -- in the same sweep. One row per aircraft per sweep per disc; the reader
    -- deduplicates across discs. Makes every write idempotent on retry.
    PRIMARY KEY (slot_start, hex, sample_point, observed_at)
);

-- The dominant read pattern is "all observations in a time range", for
-- aggregation and for rebuilding features.
CREATE INDEX IF NOT EXISTS observations_slot_idx
    ON observations (slot_start);

-- Audit trail: one row per sweep, so a gap in the data can be told apart from
-- a sweep that ran and found nothing. Without this, "no rows for 03:00" is
-- ambiguous between "scheduler died" and "API returned empty", which matters
-- because missing hours are permanently unrecoverable.
CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id            TEXT PRIMARY KEY,
    started_at        TIMESTAMPTZ NOT NULL,
    finished_at       TIMESTAMPTZ,
    slot_start        TIMESTAMPTZ NOT NULL,
    points_attempted  INT NOT NULL DEFAULT 0,
    points_succeeded  INT NOT NULL DEFAULT 0,
    aircraft_written  INT NOT NULL DEFAULT 0,
    status            TEXT NOT NULL,
    error             TEXT
);

CREATE INDEX IF NOT EXISTS ingest_runs_slot_idx
    ON ingest_runs (slot_start);
