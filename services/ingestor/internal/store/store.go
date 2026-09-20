// Package store persists observations to PostgreSQL.
package store

import (
	"context"
	"fmt"
	"io/fs"
	"sort"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Observation is one aircraft seen by one disc in one sweep, stored
// unfiltered. Thresholds are applied at read time, never here.
type Observation struct {
	ObservedAt      time.Time
	SlotStart       time.Time
	Hex             string
	Lat             float64
	Lon             float64
	AltitudeFt      *float64
	NIC             int
	NACp            *int
	SIL             *int
	AircraftType    string
	SamplePoint     string
	IsControlRegion bool
	IngestRunID     string
}

// Store wraps a connection pool.
type Store struct {
	pool *pgxpool.Pool
}

// Open connects and verifies the database is actually reachable, so a bad
// URL fails at startup rather than silently at the first sweep.
func Open(ctx context.Context, databaseURL string) (*Store, error) {
	cfg, err := pgxpool.ParseConfig(databaseURL)
	if err != nil {
		return nil, fmt.Errorf("parse database url: %w", err)
	}
	// The workload is one short burst per sweep; a large pool is pointless.
	cfg.MaxConns = 4
	cfg.MaxConnLifetime = time.Hour

	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, fmt.Errorf("connect: %w", err)
	}

	pingCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	if err := pool.Ping(pingCtx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("ping: %w", err)
	}
	return &Store{pool: pool}, nil
}

// Close releases the pool.
func (s *Store) Close() { s.pool.Close() }

// Ping reports whether the database is reachable, for the health endpoint.
func (s *Store) Ping(ctx context.Context) error { return s.pool.Ping(ctx) }

// Migrate applies every .sql file in the given filesystem, in filename order
// so the numeric prefixes decide sequence. The migrations are written to be
// idempotent (CREATE TABLE IF NOT EXISTS) so running them on every boot is
// safe, which keeps deployment to a single step.
//
// The filesystem is passed in rather than embedded here because go:embed
// cannot reach outside its own package directory.
func (s *Store) Migrate(ctx context.Context, migrations fs.FS) error {
	names, err := fs.Glob(migrations, "*.sql")
	if err != nil {
		return fmt.Errorf("glob migrations: %w", err)
	}
	if len(names) == 0 {
		return fmt.Errorf("no migrations found")
	}
	sort.Strings(names)

	for _, name := range names {
		body, err := fs.ReadFile(migrations, name)
		if err != nil {
			return fmt.Errorf("read %s: %w", name, err)
		}
		if _, err := s.pool.Exec(ctx, string(body)); err != nil {
			return fmt.Errorf("apply %s: %w", name, err)
		}
	}
	return nil
}

// InsertObservations bulk-loads a sweep.
//
// CopyFrom cannot express ON CONFLICT, and the primary key makes a replayed
// sweep a duplicate-key error rather than a no-op. So the batch lands in a
// TEMP table first and is then moved with ON CONFLICT DO NOTHING: full COPY
// throughput, and retrying a partially-written sweep stays safe.
func (s *Store) InsertObservations(ctx context.Context, obs []Observation) (int64, error) {
	if len(obs) == 0 {
		return 0, nil
	}

	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return 0, fmt.Errorf("begin: %w", err)
	}
	defer tx.Rollback(ctx) //nolint:errcheck // no-op once committed

	if _, err := tx.Exec(ctx, `
		CREATE TEMP TABLE staged_observations
		(LIKE observations INCLUDING DEFAULTS)
		ON COMMIT DROP
	`); err != nil {
		return 0, fmt.Errorf("create temp table: %w", err)
	}

	columns := []string{
		"observed_at", "slot_start", "hex", "lat", "lon", "altitude_ft",
		"nic", "nac_p", "sil", "aircraft_type", "sample_point",
		"is_control_region", "ingest_run_id",
	}
	if _, err := tx.CopyFrom(ctx,
		pgx.Identifier{"staged_observations"},
		columns,
		pgx.CopyFromSlice(len(obs), func(i int) ([]any, error) {
			o := obs[i]
			return []any{
				o.ObservedAt, o.SlotStart, o.Hex, o.Lat, o.Lon, o.AltitudeFt,
				o.NIC, o.NACp, o.SIL, o.AircraftType, o.SamplePoint,
				o.IsControlRegion, o.IngestRunID,
			}, nil
		}),
	); err != nil {
		return 0, fmt.Errorf("copy: %w", err)
	}

	tag, err := tx.Exec(ctx, `
		INSERT INTO observations
		SELECT * FROM staged_observations
		ON CONFLICT DO NOTHING
	`)
	if err != nil {
		return 0, fmt.Errorf("merge: %w", err)
	}

	if err := tx.Commit(ctx); err != nil {
		return 0, fmt.Errorf("commit: %w", err)
	}
	return tag.RowsAffected(), nil
}

// RunRecord is the audit row for one sweep.
type RunRecord struct {
	RunID           string
	StartedAt       time.Time
	FinishedAt      *time.Time
	SlotStart       time.Time
	PointsAttempted int
	PointsSucceeded int
	AircraftWritten int
	Status          string
	Error           string
}

// UpsertRun records a sweep so that a missing hour can be distinguished from
// an hour that ran and found nothing.
func (s *Store) UpsertRun(ctx context.Context, r RunRecord) error {
	var errText *string
	if r.Error != "" {
		errText = &r.Error
	}
	_, err := s.pool.Exec(ctx, `
		INSERT INTO ingest_runs
			(run_id, started_at, finished_at, slot_start, points_attempted,
			 points_succeeded, aircraft_written, status, error)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
		ON CONFLICT (run_id) DO UPDATE SET
			finished_at      = EXCLUDED.finished_at,
			points_attempted = EXCLUDED.points_attempted,
			points_succeeded = EXCLUDED.points_succeeded,
			aircraft_written = EXCLUDED.aircraft_written,
			status           = EXCLUDED.status,
			error            = EXCLUDED.error
	`, r.RunID, r.StartedAt, r.FinishedAt, r.SlotStart, r.PointsAttempted,
		r.PointsSucceeded, r.AircraftWritten, r.Status, errText)
	if err != nil {
		return fmt.Errorf("upsert run: %w", err)
	}
	return nil
}

// Stats is a small summary used by the health endpoint.
type Stats struct {
	Observations int64      `json:"observations"`
	SlotsCovered int64      `json:"slots_covered"`
	LatestSlot   *time.Time `json:"latest_slot"`
}

// Stats reports how much history exists, which is the number that actually
// matters for this project.
func (s *Store) Stats(ctx context.Context) (Stats, error) {
	var st Stats
	err := s.pool.QueryRow(ctx, `
		SELECT COUNT(*), COUNT(DISTINCT slot_start), MAX(slot_start)
		FROM observations
	`).Scan(&st.Observations, &st.SlotsCovered, &st.LatestSlot)
	return st, err
}
