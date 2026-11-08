-- Close out sweeps that can never finish.
--
-- Before the bookkeeping fix in internal/ingest (detached completion context),
-- a sweep whose 20-minute deadline expired could not record its own end, so
-- ~110 rows from the October 2026 quota incident stayed status='running'.
-- A sweep is bounded at 20 minutes, so anything still 'running' an hour after
-- it started is dead. Idempotent: migrations re-run on every start, and a live
-- sweep (started < 1 h ago) is never touched.
UPDATE ingest_runs
SET status = 'abandoned',
    finished_at = started_at + INTERVAL '20 minutes',
    error = COALESCE(error, 'sweep deadline expired before completion was recorded (pre-fix)')
WHERE status = 'running'
  AND started_at < NOW() - INTERVAL '1 hour';
