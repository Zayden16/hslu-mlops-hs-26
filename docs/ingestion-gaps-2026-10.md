# Ingestion gaps, October 2026: the adsb.lol daily quota

Investigated 2026-10-27. Status: mitigated (hourly sweeps). Lost hours cannot be recovered.

## Summary

From 2026-10-17 to 2026-10-25 the ingestor lost roughly 10 hourly slots per day,
almost always about 14:00 to 23:59 UTC. The cause is a per-IP quota on the
adsb.lol API of **500 requests per day**. At a 30-minute interval the ingestor
sent about 864 requests per day (18 sample points per sweep, 48 sweeps), so the
quota ran out every afternoon and every request was refused until the daily
reset at 00:00 UTC.

The fix is to sweep hourly (`SKYJAM_INGEST_INTERVAL=60m`), about 432 requests
per day. That leaves around 70 requests of headroom for retries.

## Evidence

### Missing slots follow a daily pattern

Hourly slots with no observations at all (UTC hours):

| Date | Missing hours |
| --- | --- |
| 10-17 | 16-22 |
| 10-18 | 10, 11 |
| 10-19 | 14-23 |
| 10-20 | 14-21, 23 |
| 10-21 | 14-23 |
| 10-22 | 13-23 |
| 10-23 | 14-23 |
| 10-24 | 14-23 |
| 10-25 | 15-23 |

In total 116 of the slots since 2026-09-17 are empty: 78 in this window, and
the rest on 2026-09-17 to 09-20, the first days of the service. Those early
gaps have a different cause. The deployment history shows failed deploys
around then, but I haven't checked this in detail.

### The ingestor's logs show 429 errors then timeouts

On 2026-10-24 the 13:30 sweep was already partly rate limited (14 of 18
points). From 14:00 every point failed with `after 4 attempts: retryable
status 429`, and the sweep then hit its 20-minute timeout:

```
14:20:00 poll failed point=poland_east err="context deadline exceeded"
14:20:00 failed to record run completion err="upsert run: context deadline exceeded"
14:20:00 sweep complete points_ok="0/18" rows_written=0 status="failed"
```

This repeated every sweep until the first sweep after midnight UTC, which got
18 of 18 points.

### Side effect in `ingest_runs`

Because the run-completion update also used the expired timeout, these sweeps
were never marked finished. About 120 records in `ingest_runs` are stuck with
`status = 'running'` and 0 points. They are leftovers, not real running
sweeps.

### The API reports the quota in its headers

Response headers from `GET https://api.adsb.lol/v2/point/...` on 2026-10-27:

```
x-ratelimit-limit: 4, 4;w=1, 60;w=60, 500;w=86400
```

That means 4 requests per second, 60 per minute and 500 per day.

## Was the quota introduced recently?

Very likely, but adsb.lol hasn't announced it, so this is inferred:

- From 2026-09-21 to 10-16 the ingestor ran at the same 30-minute interval
  (about 864 requests per day) with no gaps. A 500-per-day quota would have
  cut off every afternoon.
- The gaps start suddenly on 2026-10-17 with no change on our side.
- When the API was first measured (see `services/ingestor/README.md`) it
  returned 429 with no quota headers.
- Another adsb.lol user first noted the 500-per-day header on 2026-10-20
  (github.com/itsMattGuenther/preflight issue #11).

One thing doesn't fit: on 2026-10-26 the ingestor completed 44 sweeps (about
790 requests) without being blocked. Enforcement seems inconsistent, which
fits the adsb.lol README's statement that rate limits are "dynamic based on
environment load". Hourly sweeps stay under the quota either way.

## Changes made

- Railway variable `SKYJAM_INGEST_INTERVAL=60m` set on the `ingestor`
  service in `hslu-mlops-26` / `production` (redeployed 2026-10-27 13:55 UTC).
- Code default changed from 30m to 60m in `services/ingestor/main.go`, and the
  README config table updated with a warning not to go below 60m (commit
  `168b31a`).

## Follow-ups

- **Modelling:** the lost slots are permanent because the feed has no history
  endpoint. Lag features (1-24 h, 168 h) and evaluation splits need to handle
  missing slots instead of treating them as zero traffic.
- **Verify:** on 2026-10-28, check that 2026-10-27 14:00-23:00 has data. Part of
  today's quota had already been used before the fix.
- **Bug (fixed 2026-11-08, `2f2b658`):** the final writes now use a separate
  short timeout, so a timed-out sweep is recorded as `failed`. Migration
  `002_close_stale_runs.sql` marked the 112 stuck rows `abandoned`.
- **Robustness:** read `x-ratelimit-remaining` and stop retrying once the daily
  quota is used up, instead of spending 20 minutes on certain failures.
- **Monitoring:** alert when the latest complete slot falls more than 2 hours
  behind.
