package ingest

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/Zayden16/hslu-mlops-hs-26/services/ingestor/internal/adsb"
	"github.com/Zayden16/hslu-mlops-hs-26/services/ingestor/internal/grid"
	"github.com/Zayden16/hslu-mlops-hs-26/services/ingestor/internal/store"
)

// Regression test for the October 2026 incident: when the sweep deadline
// expired under rate limiting, the completion write reused the expired
// context and the run stayed "running" forever. The run must end up failed.
func TestExpiredSweepIsStillRecordedAsFinished(t *testing.T) {
	url := os.Getenv("TEST_DATABASE_URL")
	if url == "" {
		t.Skip("TEST_DATABASE_URL not set")
	}
	bg := context.Background()
	st, err := store.Open(bg, url)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(st.Close)
	if err := st.Migrate(bg, os.DirFS("../../migrations")); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	conn, err := pgx.Connect(bg, url)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	t.Cleanup(func() { _ = conn.Close(bg) })
	if _, err := conn.Exec(bg, "TRUNCATE observations, ingest_runs"); err != nil {
		t.Fatalf("truncate: %v", err)
	}

	// Every request answers 429, like the exhausted daily quota.
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusTooManyRequests)
	}))
	t.Cleanup(api.Close)

	in := &Ingestor{
		Client:     adsb.New(api.URL, "test", time.Second, 0),
		Store:      st,
		Points:     []grid.SamplePoint{{Name: "a", Lat: 1, Lon: 1, RadiusNM: 10}, {Name: "b", Lat: 2, Lon: 2, RadiusNM: 10}},
		InterDelay: 300 * time.Millisecond,
		Logger:     slog.New(slog.NewTextHandler(io.Discard, nil)),
	}

	// Deadline expires during the pacing delay, before the sweep finishes.
	ctx, cancel := context.WithTimeout(bg, 150*time.Millisecond)
	defer cancel()
	res, _ := in.RunOnce(ctx)

	var status string
	var finished *time.Time
	if err := conn.QueryRow(bg,
		"SELECT status, finished_at FROM ingest_runs WHERE run_id = $1", res.RunID,
	).Scan(&status, &finished); err != nil {
		t.Fatalf("read run: %v", err)
	}
	if status != "failed" || finished == nil {
		t.Fatalf("run left as status=%q finished=%v, want failed with a finish time", status, finished)
	}
}
