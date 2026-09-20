# skyjam-ingestor

The feature pipeline's capture stage, deployed on Railway.

## Why Go, why a service

The ADS-B feed has no history endpoint: an hour that is not polled is lost
forever. The original GitHub Actions cron achieved only ~53 % hourly capture
(measured over 74 h: 39 distinct hours from 61 runs) because GitHub drops
scheduled triggers under load and offers no retry. A long-lived process with
its own wall-clock-aligned ticker does not drop ticks.

## What it does, and deliberately does not do

It captures. It does **not** compute the label.

Every aircraft with a position and a reported NIC is stored with raw
coordinates, a nullable altitude, and no H3 column. The altitude floor, H3
resolution, NIC threshold and traffic floor all live in
`src/skyjam/common/schema.py` and are applied when reading. Duplicating those
rules into Go is exactly the training/serving skew the course grades against,
and storing pre-filtered data is what made the old `backfill.py` promise
impossible to honour.

## Rate limiting

The feed is limited by nginx with no `Retry-After` and no quota headers, and
the limit is documented as "dynamic based on environment load". Measured:
a burst is cut off after 2-3 requests and recovers after ~15 s. Hence a 12 s
default gap between discs and a 16 s base retry backoff. At 2 s spacing only
10-12 of 18 discs completed; at 12 s a full sweep returns 18/18 in ~7-8 min.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATABASE_URL` | required | injected by Railway when linked to Postgres |
| `SKYJAM_INGEST_INTERVAL` | `30m` | gap between sweeps, aligned to the wall clock |
| `SKYJAM_INTER_REQUEST_DELAY` | `12s` | gap between discs, tuned to the rate limit |
| `SKYJAM_REQUEST_TIMEOUT` | `30s` | per-request timeout |
| `SKYJAM_MAX_RETRIES` | `4` | attempts per disc |
| `SKYJAM_RUN_ONCE` | unset | run a single sweep and exit |
| `PORT` | `8080` | health server port |

## Endpoints

- `GET /health` — liveness plus a database ping, used by the Railway healthcheck.
- `GET /stats` — observation count, distinct hourly slots covered, latest slot.

## Tests

```bash
go test ./...                     # unit tests, no database needed
TEST_DATABASE_URL=postgres://... go test ./...   # adds the SQL integration tests
```

The store tests assert the properties that matter: replaying a sweep inserts
zero duplicate rows, the same aircraft seen by two overlapping discs is kept
twice with its provenance, and a NULL altitude round-trips as NULL rather than
as sea level.

`internal/grid` parses `src/skyjam/common/grid.py` and fails if the Go and
Python sampling grids disagree, so the writer and the reader cannot drift.
