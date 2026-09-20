package ingest

import (
	"testing"
	"time"
)

// Slot bucketing must match pandas' dt.floor("60min") exactly, or the Go
// writer and the Python aggregation would disagree about which hour an
// observation belongs to.
func TestSlotStart(t *testing.T) {
	cases := []struct{ in, want string }{
		{"2026-09-20T14:37:22Z", "2026-09-20T14:00:00Z"},
		{"2026-09-20T14:00:00Z", "2026-09-20T14:00:00Z"},
		{"2026-09-20T14:59:59Z", "2026-09-20T14:00:00Z"},
		{"2026-09-20T00:00:01Z", "2026-09-20T00:00:00Z"},
	}
	for _, tc := range cases {
		in, err := time.Parse(time.RFC3339, tc.in)
		if err != nil {
			t.Fatal(err)
		}
		got := SlotStart(in).Format(time.RFC3339)
		if got != tc.want {
			t.Errorf("SlotStart(%s) = %s, want %s", tc.in, got, tc.want)
		}
	}
}

// A non-UTC input must still land in the correct UTC slot; truncating local
// time first would shift the hour for any offset zone.
func TestSlotStartNormalisesZone(t *testing.T) {
	zone := time.FixedZone("CEST", 2*60*60)
	in := time.Date(2026, 9, 20, 14, 37, 0, 0, zone) // 12:37 UTC
	got := SlotStart(in).Format(time.RFC3339)
	if got != "2026-09-20T12:00:00Z" {
		t.Fatalf("got %s, want 2026-09-20T12:00:00Z", got)
	}
}
