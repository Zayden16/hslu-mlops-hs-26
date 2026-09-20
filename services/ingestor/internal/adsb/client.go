// Package adsb is a thin client for the public ADS-B aggregator feed.
//
// The v2 `point` endpoint returns every aircraft currently tracked within a
// radius of a coordinate, including the self-reported navigation quality
// fields (nic, nac_p, sil) that this project is built on.
package adsb

import (
	"context"
	"encoding/json"
	"fmt"
	"math/rand"
	"net/http"
	"time"
)

// retryableStatus are responses worth retrying rather than losing the sweep.
// The public endpoint returns 429 when polled too aggressively.
var retryableStatus = map[int]bool{429: true, 500: true, 502: true, 503: true, 504: true}

// Aircraft is one aircraft as reported by the feed.
//
// Every numeric field is a pointer because "absent" and "zero" are genuinely
// different here: NIC 0 means the aircraft reports the worst possible
// containment radius, which is a strong interference signal, while a missing
// NIC means the aircraft never reported one and must be excluded entirely.
// Collapsing those two into 0 would manufacture fake interference.
type Aircraft struct {
	Hex  string   `json:"hex"`
	Lat  *float64 `json:"lat"`
	Lon  *float64 `json:"lon"`
	NIC  *int     `json:"nic"`
	NACp *int     `json:"nac_p"`
	SIL  *int     `json:"sil"`
	Type string   `json:"t"`

	// AltBaro is either a number (feet) or the literal string "ground".
	AltBaro json.RawMessage `json:"alt_baro"`
}

// AltitudeFt returns the barometric altitude in feet, and false when the
// aircraft is on the ground or reported no usable altitude.
func (a Aircraft) AltitudeFt() (float64, bool) {
	if len(a.AltBaro) == 0 {
		return 0, false
	}
	// json.Unmarshal happily decodes `null` into a float64 and leaves it at
	// zero, which would silently record a missing altitude as sea level.
	if string(a.AltBaro) == "null" {
		return 0, false
	}
	var f float64
	if err := json.Unmarshal(a.AltBaro, &f); err == nil {
		return f, true
	}
	// Anything non-numeric ("ground") is not an altitude.
	return 0, false
}

// Snapshot is one raw API response plus the server timestamp it carries.
type Snapshot struct {
	Lat      float64
	Lon      float64
	RadiusNM int
	NowMS    int64
	Aircraft []Aircraft
}

type response struct {
	Now json.Number `json:"now"`
	AC  []Aircraft  `json:"ac"`
}

// Client polls the feed. Safe for sequential use by the ingest loop.
type Client struct {
	BaseURL    string
	UserAgent  string
	HTTPClient *http.Client
	MaxRetries int
	// BaseBackoff is the first retry delay. Overridable so tests do not have
	// to wait out the real, deliberately long, rate-limit recovery window.
	BaseBackoff time.Duration
}

// New builds a client with sane timeouts.
func New(baseURL, userAgent string, timeout time.Duration, maxRetries int) *Client {
	return &Client{
		BaseURL:     baseURL,
		UserAgent:   userAgent,
		HTTPClient:  &http.Client{Timeout: timeout},
		MaxRetries:  maxRetries,
		BaseBackoff: 16 * time.Second,
	}
}

// FetchPoint returns all aircraft within radiusNM of a coordinate, retrying
// transient failures with exponential backoff and jitter.
func (c *Client) FetchPoint(ctx context.Context, lat, lon float64, radiusNM int) (*Snapshot, error) {
	url := fmt.Sprintf("%s/point/%g/%g/%d", c.BaseURL, lat, lon, radiusNM)

	var lastErr error
	for attempt := 0; attempt < c.MaxRetries; attempt++ {
		if attempt > 0 {
			// The feed is rate-limited by nginx with no Retry-After header
			// and no quota headers, and the limit is documented as "dynamic
			// based on environment load". Measured behaviour: a burst is cut
			// off after 2-3 requests and recovers after ~15s. So the first
			// retry already waits longer than that recovery window instead of
			// starting at 3s, which just burned an attempt.
			base := c.BaseBackoff
			if base <= 0 {
				base = 16 * time.Second
			}
			backoff := base * (1 << uint(attempt-1))
			if backoff > 60*time.Second {
				backoff = 60 * time.Second
			}
			// Jitter so retries from a restarting service, or from several
			// discs failing together, do not resynchronise into a new burst.
			jitter := base / 4
			if jitter > 0 {
				backoff += time.Duration(rand.Int63n(int64(jitter)))
			}
			select {
			case <-ctx.Done():
				return nil, ctx.Err()
			case <-time.After(backoff):
			}
		}

		snap, err := c.fetchOnce(ctx, url, lat, lon, radiusNM)
		if err == nil {
			return snap, nil
		}
		// A cancelled context is not a transient failure.
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		lastErr = err
	}
	return nil, fmt.Errorf("after %d attempts: %w", c.MaxRetries, lastErr)
}

func (c *Client) fetchOnce(ctx context.Context, url string, lat, lon float64, radiusNM int) (*Snapshot, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", c.UserAgent)
	req.Header.Set("Accept", "application/json")

	resp, err := c.HTTPClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if retryableStatus[resp.StatusCode] {
		return nil, fmt.Errorf("retryable status %d for %g,%g", resp.StatusCode, lat, lon)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("status %d for %g,%g", resp.StatusCode, lat, lon)
	}

	var r response
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil {
		return nil, fmt.Errorf("decode: %w", err)
	}
	nowMS, _ := r.Now.Int64()

	return &Snapshot{
		Lat:      lat,
		Lon:      lon,
		RadiusNM: radiusNM,
		NowMS:    nowMS,
		Aircraft: r.AC,
	}, nil
}
