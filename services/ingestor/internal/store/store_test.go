package store

import (
	"context"
	"os"
	"testing"
	"time"
)

// These exercise the real SQL. They are skipped unless a database is provided,
// so `go test ./...` stays runnable without Docker, but CI and local runs with
// TEST_DATABASE_URL get genuine coverage of the COPY + ON CONFLICT path.
func testStore(t *testing.T) *Store {
	t.Helper()
	url := os.Getenv("TEST_DATABASE_URL")
	if url == "" {
		t.Skip("TEST_DATABASE_URL not set")
	}
	ctx := context.Background()
	s, err := Open(ctx, url)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(s.Close)

	if err := s.Migrate(ctx, os.DirFS("../../migrations")); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	// Each test starts from a known state.
	if _, err := s.pool.Exec(ctx, "TRUNCATE observations, ingest_runs"); err != nil {
		t.Fatalf("truncate: %v", err)
	}
	return s
}

func obs(hex string, slot time.Time, point string, alt *float64) Observation {
	return Observation{
		ObservedAt:      slot.Add(37 * time.Second),
		SlotStart:       slot,
		Hex:             hex,
		Lat:             54.7,
		Lon:             20.5,
		AltitudeFt:      alt,
		NIC:             4,
		SamplePoint:     point,
		IsControlRegion: false,
		IngestRunID:     "run-1",
	}
}

// Migrations run on every boot, so they must be safe to re-apply.
func TestMigrateIsIdempotent(t *testing.T) {
	s := testStore(t)
	ctx := context.Background()
	if err := s.Migrate(ctx, os.DirFS("../../migrations")); err != nil {
		t.Fatalf("second migrate failed: %v", err)
	}
}

// The critical property: replaying a sweep must not duplicate rows, because
// retries and restarts will happen and duplicated aircraft would corrupt the
// degraded-share statistic that the label is built on.
func TestInsertObservationsIsIdempotent(t *testing.T) {
	s := testStore(t)
	ctx := context.Background()
	slot := time.Date(2026, 9, 20, 14, 0, 0, 0, time.UTC)

	batch := []Observation{
		obs("aaa", slot, "baltic_kaliningrad", nil),
		obs("bbb", slot, "baltic_kaliningrad", nil),
	}

	n, err := s.InsertObservations(ctx, batch)
	if err != nil {
		t.Fatalf("first insert: %v", err)
	}
	if n != 2 {
		t.Fatalf("first insert wrote %d, want 2", n)
	}

	n, err = s.InsertObservations(ctx, batch)
	if err != nil {
		t.Fatalf("replay must not error, got: %v", err)
	}
	if n != 0 {
		t.Fatalf("replay wrote %d rows, want 0", n)
	}

	var total int
	if err := s.pool.QueryRow(ctx, "SELECT COUNT(*) FROM observations").Scan(&total); err != nil {
		t.Fatal(err)
	}
	if total != 2 {
		t.Fatalf("table holds %d rows, want 2", total)
	}
}

// Discs overlap on purpose, so the same airframe legitimately appears under
// two sample points in one sweep. Both rows must survive: dropping one would
// lose the provenance needed for per-region reporting.
func TestSameAircraftFromTwoPointsBothStored(t *testing.T) {
	s := testStore(t)
	ctx := context.Background()
	slot := time.Date(2026, 9, 20, 14, 0, 0, 0, time.UTC)

	n, err := s.InsertObservations(ctx, []Observation{
		obs("aaa", slot, "baltic_kaliningrad", nil),
		obs("aaa", slot, "baltic_south", nil),
	})
	if err != nil {
		t.Fatal(err)
	}
	if n != 2 {
		t.Fatalf("wrote %d, want 2 (overlapping discs are not duplicates)", n)
	}
}

// A NULL altitude must round-trip as NULL, not as zero, so the cruise filter
// can exclude it rather than treating it as ground level.
func TestNullAltitudeRoundTrips(t *testing.T) {
	s := testStore(t)
	ctx := context.Background()
	slot := time.Date(2026, 9, 20, 14, 0, 0, 0, time.UTC)
	alt := 35000.0

	if _, err := s.InsertObservations(ctx, []Observation{
		obs("aaa", slot, "baltic_kaliningrad", nil),
		obs("bbb", slot, "baltic_kaliningrad", &alt),
	}); err != nil {
		t.Fatal(err)
	}

	var nulls int
	if err := s.pool.QueryRow(ctx,
		"SELECT COUNT(*) FROM observations WHERE altitude_ft IS NULL").Scan(&nulls); err != nil {
		t.Fatal(err)
	}
	if nulls != 1 {
		t.Fatalf("got %d null altitudes, want 1", nulls)
	}
}

// The audit table is what distinguishes "the scheduler died" from "the sweep
// ran and the sky was empty", which matters because missed hours are
// unrecoverable and need to be noticed.
func TestUpsertRunTracksLifecycle(t *testing.T) {
	s := testStore(t)
	ctx := context.Background()
	slot := time.Date(2026, 9, 20, 14, 0, 0, 0, time.UTC)
	started := slot.Add(time.Minute)

	if err := s.UpsertRun(ctx, RunRecord{
		RunID: "run-1", StartedAt: started, SlotStart: slot,
		PointsAttempted: 18, Status: "running",
	}); err != nil {
		t.Fatal(err)
	}

	finished := started.Add(2 * time.Minute)
	if err := s.UpsertRun(ctx, RunRecord{
		RunID: "run-1", StartedAt: started, FinishedAt: &finished, SlotStart: slot,
		PointsAttempted: 18, PointsSucceeded: 17, AircraftWritten: 1234,
		Status: "partial", Error: "one disc timed out",
	}); err != nil {
		t.Fatal(err)
	}

	var (
		status  string
		written int
		errText *string
		rows    int
	)
	if err := s.pool.QueryRow(ctx,
		"SELECT COUNT(*) FROM ingest_runs").Scan(&rows); err != nil {
		t.Fatal(err)
	}
	if rows != 1 {
		t.Fatalf("expected the run to be updated in place, got %d rows", rows)
	}
	if err := s.pool.QueryRow(ctx,
		"SELECT status, aircraft_written, error FROM ingest_runs WHERE run_id='run-1'").
		Scan(&status, &written, &errText); err != nil {
		t.Fatal(err)
	}
	if status != "partial" || written != 1234 {
		t.Fatalf("status=%s written=%d", status, written)
	}
	if errText == nil || *errText == "" {
		t.Fatal("expected the partial-failure reason to be retained")
	}
}

func TestStatsReportsHistory(t *testing.T) {
	s := testStore(t)
	ctx := context.Background()
	slot1 := time.Date(2026, 9, 20, 14, 0, 0, 0, time.UTC)
	slot2 := slot1.Add(time.Hour)

	if _, err := s.InsertObservations(ctx, []Observation{
		obs("aaa", slot1, "baltic_kaliningrad", nil),
		obs("bbb", slot1, "baltic_kaliningrad", nil),
		obs("aaa", slot2, "baltic_kaliningrad", nil),
	}); err != nil {
		t.Fatal(err)
	}

	st, err := s.Stats(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if st.Observations != 3 {
		t.Errorf("observations = %d, want 3", st.Observations)
	}
	if st.SlotsCovered != 2 {
		t.Errorf("slots = %d, want 2", st.SlotsCovered)
	}
	if st.LatestSlot == nil || !st.LatestSlot.Equal(slot2) {
		t.Errorf("latest slot = %v, want %v", st.LatestSlot, slot2)
	}
}

// An empty sweep must be a no-op rather than an error, so a quiet API
// response does not look like a failure.
func TestInsertEmptyIsNoOp(t *testing.T) {
	s := testStore(t)
	n, err := s.InsertObservations(context.Background(), nil)
	if err != nil || n != 0 {
		t.Fatalf("n=%d err=%v", n, err)
	}
}
