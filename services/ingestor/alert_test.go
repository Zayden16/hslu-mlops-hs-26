package main

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/Zayden16/hslu-mlops-hs-26/services/ingestor/internal/notify"
)

type captured struct {
	mu     sync.Mutex
	titles []string
	prios  []string
}

func newAlerter(t *testing.T) (*sweepAlerter, *captured) {
	t.Helper()
	c := &captured{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c.mu.Lock()
		defer c.mu.Unlock()
		c.titles = append(c.titles, r.Header.Get("Title"))
		c.prios = append(c.prios, r.Header.Get("Priority"))
	}))
	t.Cleanup(srv.Close)
	return &sweepAlerter{
		n:      notify.New(srv.URL, ""),
		logger: slog.New(slog.NewTextHandler(io.Discard, nil)),
	}, c
}

func outcome(ok, of int, err error) sweepOutcome {
	return sweepOutcome{Slot: time.Date(2026, 11, 9, 14, 0, 0, 0, time.UTC), Succeeded: ok, Attempted: of, Err: err}
}

func TestAlertsOnlyOnStateChange(t *testing.T) {
	a, c := newAlerter(t)
	ctx := context.Background()

	a.observe(ctx, outcome(18, 18, nil)) // healthy: silent
	a.observe(ctx, outcome(14, 18, nil)) // degrades: alert
	a.observe(ctx, outcome(0, 18, nil))  // still degraded: no repeat
	a.observe(ctx, outcome(0, 18, nil))
	a.observe(ctx, outcome(18, 18, nil)) // recovers: alert
	a.observe(ctx, outcome(18, 18, nil)) // healthy: silent

	want := []string{"skyjam ingest degraded", "skyjam ingest recovered"}
	if len(c.titles) != 2 || c.titles[0] != want[0] || c.titles[1] != want[1] {
		t.Fatalf("got %v, want %v", c.titles, want)
	}
	if c.prios[0] != "high" {
		t.Fatalf("partial sweep should be high, got %q", c.prios[0])
	}
}

func TestTotalLossIsUrgent(t *testing.T) {
	a, c := newAlerter(t)
	a.observe(context.Background(), outcome(0, 18, errors.New("context deadline exceeded")))
	if len(c.prios) != 1 || c.prios[0] != "urgent" {
		t.Fatalf("0/18 must be urgent, got %v", c.prios)
	}
}

func TestPersistErrorWithAllDiscsIsDegraded(t *testing.T) {
	// All discs answered but the database write failed: the hour is still lost.
	a, c := newAlerter(t)
	a.observe(context.Background(), outcome(18, 18, errors.New("persist sweep: conn refused")))
	if len(c.titles) != 1 {
		t.Fatalf("a failed persist must alert, got %v", c.titles)
	}
}

func TestDisabledNotifierIsSilent(t *testing.T) {
	a := &sweepAlerter{n: notify.New("", ""), logger: slog.New(slog.NewTextHandler(io.Discard, nil))}
	a.observe(context.Background(), outcome(0, 18, nil)) // must not panic or block
	if !a.degraded {
		t.Fatal("state must still be tracked without a topic")
	}
}
