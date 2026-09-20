// Package ingest runs sweeps of the sampling grid and persists them.
package ingest

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"github.com/Zayden16/hslu-mlops-hs-26/services/ingestor/internal/adsb"
	"github.com/Zayden16/hslu-mlops-hs-26/services/ingestor/internal/grid"
	"github.com/Zayden16/hslu-mlops-hs-26/services/ingestor/internal/store"
)

// SlotMinutes must match SLOT_MINUTES in src/skyjam/common/schema.py.
// Observations are bucketed into hourly slots.
const SlotMinutes = 60

// Ingestor polls the grid and writes to the store.
type Ingestor struct {
	Client      *adsb.Client
	Store       *store.Store
	Points      []grid.SamplePoint
	InterDelay  time.Duration
	Logger      *slog.Logger
	SweepPerRun int
}

// Result summarises one sweep.
type Result struct {
	RunID      string
	SlotStart  time.Time
	Attempted  int
	Succeeded  int
	Written    int64
	Duration   time.Duration
	FirstError error
}

// SlotStart floors a timestamp to its hourly slot, matching the Python
// `dt.floor("60min")` used by the aggregation.
func SlotStart(t time.Time) time.Time {
	return t.UTC().Truncate(time.Duration(SlotMinutes) * time.Minute)
}

// RunOnce polls every sample point once and persists the result.
//
// A failure at one point never aborts the sweep: the regions are independent
// and a lost disc is better than a lost hour, since missed hours can never be
// recovered from the source.
func (in *Ingestor) RunOnce(ctx context.Context) (Result, error) {
	started := time.Now().UTC()
	slot := SlotStart(started)
	runID := fmt.Sprintf("%s-%d", slot.Format("20060102T150405"), started.UnixMilli()%100000)

	res := Result{RunID: runID, SlotStart: slot, Attempted: len(in.Points)}

	// Record the attempt up front, so a crash mid-sweep still leaves evidence
	// that this slot was tried.
	if err := in.Store.UpsertRun(ctx, store.RunRecord{
		RunID:           runID,
		StartedAt:       started,
		SlotStart:       slot,
		PointsAttempted: len(in.Points),
		Status:          "running",
	}); err != nil {
		return res, fmt.Errorf("record run start: %w", err)
	}

	observations := make([]store.Observation, 0, 16384)

	for i, p := range in.Points {
		if ctx.Err() != nil {
			break
		}
		// Pace requests: the public API rate-limits aggressive clients.
		if i > 0 {
			select {
			case <-ctx.Done():
			case <-time.After(in.InterDelay):
			}
		}

		snap, err := in.Client.FetchPoint(ctx, p.Lat, p.Lon, p.RadiusNM)
		if err != nil {
			in.Logger.Error("poll failed, continuing",
				"point", p.Name, "err", err)
			if res.FirstError == nil {
				res.FirstError = err
			}
			continue
		}
		res.Succeeded++

		observedAt := time.Now().UTC()
		kept := 0
		for _, ac := range snap.Aircraft {
			// Only two exclusions, and both are "we cannot use this row at
			// all", not modelling choices: no position means it cannot be
			// placed in a cell, no NIC means it carries no signal.
			if ac.Lat == nil || ac.Lon == nil || ac.NIC == nil {
				continue
			}
			var altPtr *float64
			if alt, ok := ac.AltitudeFt(); ok {
				altPtr = &alt
			}
			observations = append(observations, store.Observation{
				ObservedAt:      observedAt,
				SlotStart:       SlotStart(observedAt),
				Hex:             ac.Hex,
				Lat:             *ac.Lat,
				Lon:             *ac.Lon,
				AltitudeFt:      altPtr,
				NIC:             *ac.NIC,
				NACp:            ac.NACp,
				SIL:             ac.SIL,
				AircraftType:    ac.Type,
				SamplePoint:     p.Name,
				IsControlRegion: p.Control,
				IngestRunID:     runID,
			})
			kept++
		}
		in.Logger.Info("polled",
			"point", p.Name, "aircraft", len(snap.Aircraft), "usable", kept)
	}

	written, err := in.Store.InsertObservations(ctx, observations)
	if err != nil {
		finished := time.Now().UTC()
		_ = in.Store.UpsertRun(ctx, store.RunRecord{
			RunID:           runID,
			StartedAt:       started,
			FinishedAt:      &finished,
			SlotStart:       slot,
			PointsAttempted: res.Attempted,
			PointsSucceeded: res.Succeeded,
			Status:          "failed",
			Error:           err.Error(),
		})
		return res, fmt.Errorf("persist sweep: %w", err)
	}
	res.Written = written
	res.Duration = time.Since(started)

	status := "ok"
	errText := ""
	switch {
	case res.Succeeded == 0:
		status = "failed"
	case res.Succeeded < res.Attempted:
		status = "partial"
	}
	if res.FirstError != nil {
		errText = res.FirstError.Error()
	}

	finished := time.Now().UTC()
	if err := in.Store.UpsertRun(ctx, store.RunRecord{
		RunID:           runID,
		StartedAt:       started,
		FinishedAt:      &finished,
		SlotStart:       slot,
		PointsAttempted: res.Attempted,
		PointsSucceeded: res.Succeeded,
		AircraftWritten: int(written),
		Status:          status,
		Error:           errText,
	}); err != nil {
		in.Logger.Error("failed to record run completion", "err", err)
	}

	in.Logger.Info("sweep complete",
		"run_id", runID,
		"slot", slot.Format(time.RFC3339),
		"points_ok", fmt.Sprintf("%d/%d", res.Succeeded, res.Attempted),
		"rows_written", written,
		"collected", len(observations),
		"duration", res.Duration.Round(time.Second).String(),
		"status", status)

	return res, nil
}
