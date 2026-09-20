// Command ingestor is the skyjam feature-pipeline ingest service.
//
// Why this exists as a long-lived Go service rather than a GitHub Actions
// cron: the ADS-B feed has no history endpoint, so an hour that is not polled
// is permanently lost. Measured over the first days of the Actions schedule,
// only about half of the expected hourly runs actually fired, because GitHub
// drops scheduled triggers under load and offers no retry. A process with its
// own ticker does not drop ticks, and Railway keeps it running.
package main

import (
	"context"
	"embed"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"sync"
	"syscall"
	"time"

	"github.com/Zayden16/hslu-mlops-hs-26/services/ingestor/internal/adsb"
	"github.com/Zayden16/hslu-mlops-hs-26/services/ingestor/internal/grid"
	"github.com/Zayden16/hslu-mlops-hs-26/services/ingestor/internal/ingest"
	"github.com/Zayden16/hslu-mlops-hs-26/services/ingestor/internal/store"
)

//go:embed migrations/*.sql
var migrationsFS embed.FS

type config struct {
	DatabaseURL string
	BaseURL     string
	UserAgent   string
	Interval    time.Duration
	InterDelay  time.Duration
	Timeout     time.Duration
	MaxRetries  int
	Port        string
	RunOnce     bool
	SkipMigrate bool
}

func loadConfig() (config, error) {
	c := config{
		DatabaseURL: env("DATABASE_URL", ""),
		BaseURL:     env("SKYJAM_ADSB_BASE_URL", "https://api.adsb.lol/v2"),
		UserAgent:   env("SKYJAM_USER_AGENT", "skyjam/0.1 (HSLU MLOps student project)"),
		Port:        env("PORT", "8080"),
		RunOnce:     env("SKYJAM_RUN_ONCE", "") == "true",
		SkipMigrate: env("SKYJAM_SKIP_MIGRATE", "") == "true",
	}
	if c.DatabaseURL == "" {
		return c, errors.New("DATABASE_URL is required (Railway injects it when the service is linked to Postgres)")
	}

	var err error
	// Sweeps are cheap (~2 min) and the label is hourly, so polling more often
	// than hourly costs little and buys redundancy against a restart landing
	// badly. Several sweeps in one hour collapse during aggregation.
	if c.Interval, err = envDuration("SKYJAM_INGEST_INTERVAL", 30*time.Minute); err != nil {
		return c, err
	}
	if c.InterDelay, err = envDuration("SKYJAM_INTER_REQUEST_DELAY", 12*time.Second); err != nil {
		return c, err
	}
	if c.Timeout, err = envDuration("SKYJAM_REQUEST_TIMEOUT", 30*time.Second); err != nil {
		return c, err
	}
	if c.MaxRetries, err = envInt("SKYJAM_MAX_RETRIES", 4); err != nil {
		return c, err
	}
	return c, nil
}

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envDuration(key string, def time.Duration) (time.Duration, error) {
	raw := os.Getenv(key)
	if raw == "" {
		return def, nil
	}
	d, err := time.ParseDuration(raw)
	if err != nil {
		return 0, fmt.Errorf("%s=%q is not a duration: %w", key, raw, err)
	}
	return d, nil
}

func envInt(key string, def int) (int, error) {
	raw := os.Getenv(key)
	if raw == "" {
		return def, nil
	}
	n, err := strconv.Atoi(raw)
	if err != nil {
		return 0, fmt.Errorf("%s=%q is not an integer: %w", key, raw, err)
	}
	return n, nil
}

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	if err := run(logger); err != nil {
		logger.Error("fatal", "err", err)
		os.Exit(1)
	}
}

func run(logger *slog.Logger) error {
	cfg, err := loadConfig()
	if err != nil {
		return err
	}

	// Shut down cleanly on Railway redeploys so a sweep in flight is not
	// killed mid-transaction.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	st, err := store.Open(ctx, cfg.DatabaseURL)
	if err != nil {
		return err
	}
	defer st.Close()

	if !cfg.SkipMigrate {
		sub, err := fs.Sub(migrationsFS, "migrations")
		if err != nil {
			return err
		}
		if err := st.Migrate(ctx, sub); err != nil {
			return err
		}
		logger.Info("migrations applied")
	}

	ing := &ingest.Ingestor{
		Client:     adsb.New(cfg.BaseURL, cfg.UserAgent, cfg.Timeout, cfg.MaxRetries),
		Store:      st,
		Points:     grid.SamplePoints,
		InterDelay: cfg.InterDelay,
		Logger:     logger,
	}

	// One-shot mode exists so the same binary can be used for a manual
	// backfill-style run or a smoke test, without a second code path.
	if cfg.RunOnce {
		_, err := ing.RunOnce(ctx)
		return err
	}

	srv := healthServer(cfg.Port, st, logger)
	go func() {
		logger.Info("health server listening", "port", cfg.Port)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Error("health server failed", "err", err)
		}
	}()

	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		loop(ctx, ing, cfg.Interval, logger)
	}()

	<-ctx.Done()
	logger.Info("shutdown signal received, draining")

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	_ = srv.Shutdown(shutdownCtx)
	wg.Wait()
	logger.Info("stopped cleanly")
	return nil
}

// loop sweeps immediately, then on a ticker aligned to the wall clock.
//
// Alignment matters: sweeps drifting slowly through the hour would cluster
// near a slot boundary and occasionally straddle it, so an hour could receive
// two sweeps or none. Anchoring to real time keeps roughly even coverage.
func loop(ctx context.Context, ing *ingest.Ingestor, interval time.Duration, logger *slog.Logger) {
	// Sweep at once on boot, so a redeploy does not skip a slot.
	sweep(ctx, ing, logger)

	for {
		wait := time.Until(time.Now().Add(interval).Truncate(interval))
		if wait <= 0 {
			wait = interval
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(wait):
			sweep(ctx, ing, logger)
		}
	}
}

func sweep(ctx context.Context, ing *ingest.Ingestor, logger *slog.Logger) {
	if ctx.Err() != nil {
		return
	}
	// A hung sweep must not block the next one indefinitely.
	sweepCtx, cancel := context.WithTimeout(ctx, 20*time.Minute)
	defer cancel()

	if _, err := ing.RunOnce(sweepCtx); err != nil {
		// Never fatal: the next tick is another chance, and exiting would
		// lose every subsequent hour.
		logger.Error("sweep failed", "err", err)
	}
}

// healthServer exposes liveness and, more usefully, how much history exists.
func healthServer(port string, st *store.Store, logger *slog.Logger) *http.Server {
	mux := http.NewServeMux()

	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
		defer cancel()
		if err := st.Ping(ctx); err != nil {
			http.Error(w, "database unreachable", http.StatusServiceUnavailable)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]string{"status": "ok"})
	})

	mux.HandleFunc("/stats", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
		defer cancel()
		stats, err := st.Stats(ctx)
		if err != nil {
			http.Error(w, "stats unavailable", http.StatusServiceUnavailable)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(stats)
	})

	return &http.Server{
		Addr:              ":" + port,
		Handler:           mux,
		ReadHeaderTimeout: 10 * time.Second,
	}
}
