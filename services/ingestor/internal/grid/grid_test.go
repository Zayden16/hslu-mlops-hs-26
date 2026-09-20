package grid

import (
	"bufio"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"testing"
)

// The sampling grid exists in both Go (writer) and Python (reader, docs,
// per-region reporting). If they drift, observations get attributed to the
// wrong region and the control-vs-interference comparison silently breaks.
// This test parses the Python source and asserts the two agree exactly.
func TestGridMatchesPython(t *testing.T) {
	path := filepath.Join("..", "..", "..", "..", "src", "skyjam", "common", "grid.py")
	f, err := os.Open(path)
	if err != nil {
		t.Skipf("python grid not available (%v)", err)
	}
	defer f.Close()

	// SamplePoint("name", lat, lon[, radius][, control=True])
	re := regexp.MustCompile(`SamplePoint\(\s*"([^"]+)"\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*(.*?)\)`)

	type pyPoint struct {
		lat, lon float64
		radius   int
		control  bool
	}
	py := map[string]pyPoint{}

	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := scanner.Text()
		if strings.HasPrefix(strings.TrimSpace(line), "#") {
			continue
		}
		m := re.FindStringSubmatch(line)
		if m == nil {
			continue
		}
		lat, err := strconv.ParseFloat(m[2], 64)
		if err != nil {
			t.Fatalf("bad lat for %s: %v", m[1], err)
		}
		lon, err := strconv.ParseFloat(m[3], 64)
		if err != nil {
			t.Fatalf("bad lon for %s: %v", m[1], err)
		}

		rest := m[4]
		p := pyPoint{lat: lat, lon: lon, radius: 250, control: strings.Contains(rest, "control=True")}
		// An explicit positional radius, if present, is the first bare number
		// in the trailing arguments.
		if bare := regexp.MustCompile(`,\s*(\d+)\s*(?:,|$)`).FindStringSubmatch(rest); bare != nil {
			if r, err := strconv.Atoi(bare[1]); err == nil {
				p.radius = r
			}
		}
		py[m[1]] = p
	}
	if err := scanner.Err(); err != nil {
		t.Fatal(err)
	}

	if len(py) == 0 {
		t.Fatal("parsed no sample points from the python grid; the parser is broken")
	}
	if len(py) != len(SamplePoints) {
		t.Fatalf("python has %d points, go has %d", len(py), len(SamplePoints))
	}

	for _, g := range SamplePoints {
		p, ok := py[g.Name]
		if !ok {
			t.Errorf("point %q exists in go but not in python", g.Name)
			continue
		}
		if p.lat != g.Lat || p.lon != g.Lon {
			t.Errorf("point %q: python (%v,%v) != go (%v,%v)", g.Name, p.lat, p.lon, g.Lat, g.Lon)
		}
		if p.radius != g.RadiusNM {
			t.Errorf("point %q: python radius %d != go %d", g.Name, p.radius, g.RadiusNM)
		}
		if p.control != g.Control {
			t.Errorf("point %q: python control %v != go %v", g.Name, p.control, g.Control)
		}
	}
}

// Duplicate names would collide in per-region reporting.
func TestNamesUnique(t *testing.T) {
	seen := map[string]bool{}
	for _, p := range SamplePoints {
		if seen[p.Name] {
			t.Errorf("duplicate sample point name %q", p.Name)
		}
		seen[p.Name] = true
	}
}

// Control regions are what make "the model didn't just learn east is bad"
// demonstrable, so losing them silently would undermine the evaluation.
func TestHasBothRegionKinds(t *testing.T) {
	var controls, interference int
	for _, p := range SamplePoints {
		if p.Control {
			controls++
		} else {
			interference++
		}
	}
	if controls == 0 || interference == 0 {
		t.Fatalf("need both kinds: %d control, %d interference", controls, interference)
	}
}
