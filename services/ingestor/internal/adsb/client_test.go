package adsb

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// The feed reports alt_baro as a number in feet or the literal string
// "ground". Treating "ground" as 0 would put parked aircraft into the dataset
// as if they were flying at sea level.
func TestAltitudeFt(t *testing.T) {
	cases := []struct {
		name   string
		raw    string
		want   float64
		wantOK bool
	}{
		{"cruise", `35000`, 35000, true},
		{"low", `1200.5`, 1200.5, true},
		{"ground string", `"ground"`, 0, false},
		{"absent", ``, 0, false},
		{"null", `null`, 0, false},
		{"zero is a real altitude", `0`, 0, true},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			a := Aircraft{}
			if tc.raw != "" {
				a.AltBaro = json.RawMessage(tc.raw)
			}
			got, ok := a.AltitudeFt()
			if ok != tc.wantOK {
				t.Fatalf("ok = %v, want %v", ok, tc.wantOK)
			}
			if ok && got != tc.want {
				t.Fatalf("alt = %v, want %v", got, tc.want)
			}
		})
	}
}

// NIC 0 (worst containment radius) must survive decoding as a present value,
// because collapsing it with "absent" would erase the strongest possible
// interference signal.
func TestDecodePreservesZeroNIC(t *testing.T) {
	body := `{"now":1,"ac":[{"hex":"abc","lat":1,"lon":2,"nic":0},{"hex":"def","lat":3,"lon":4}]}`
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(body))
	}))
	defer srv.Close()

	c := New(srv.URL, "test", 5*time.Second, 1)
	snap, err := c.FetchPoint(context.Background(), 1, 2, 250)
	if err != nil {
		t.Fatal(err)
	}
	if len(snap.Aircraft) != 2 {
		t.Fatalf("got %d aircraft", len(snap.Aircraft))
	}
	if snap.Aircraft[0].NIC == nil || *snap.Aircraft[0].NIC != 0 {
		t.Fatal("NIC 0 must decode as present and zero")
	}
	if snap.Aircraft[1].NIC != nil {
		t.Fatal("missing NIC must decode as nil, not 0")
	}
}

// A transient 429 is the normal response to polling too fast; losing the
// sweep because of it would lose an unrecoverable hour.
func TestRetriesTransientStatus(t *testing.T) {
	var calls int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if calls < 2 {
			w.WriteHeader(http.StatusTooManyRequests)
			return
		}
		_, _ = w.Write([]byte(`{"now":1,"ac":[]}`))
	}))
	defer srv.Close()

	c := New(srv.URL, "test", 5*time.Second, 4)
	c.BaseBackoff = 20 * time.Millisecond
	// Keep the test fast: the first retry waits ~3s, so bound it generously
	// but still assert it succeeds rather than gives up.
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	if _, err := c.FetchPoint(ctx, 1, 2, 250); err != nil {
		t.Fatalf("expected success after retry, got %v", err)
	}
	if calls < 2 {
		t.Fatalf("expected a retry, got %d calls", calls)
	}
}

// A 404 is a client mistake and retrying it just wastes the sweep window.
func TestGivesUpAfterMaxRetries(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
	}))
	defer srv.Close()

	c := New(srv.URL, "test", 5*time.Second, 1)
	if _, err := c.FetchPoint(context.Background(), 1, 2, 250); err == nil {
		t.Fatal("expected error for 404")
	}
}
