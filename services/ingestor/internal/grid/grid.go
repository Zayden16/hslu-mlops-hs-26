// Package grid defines which points are polled to cover the area of interest.
//
// This mirrors src/skyjam/common/grid.py exactly. The duplication is
// deliberate and bounded: it is a list of coordinates, not a modelling rule.
// Label semantics are never duplicated into Go, because that is where
// training/serving skew actually comes from. A parity test keeps the two
// lists honest.
package grid

// SamplePoint is one polling disc.
type SamplePoint struct {
	Name     string
	Lat      float64
	Lon      float64
	RadiusNM int
	// Control marks regions expected to be quiet, so class balance and
	// false-positive rate can be reported per region.
	Control bool
}

// SamplePoints covers European airspace plus the regions where GNSS
// interference is routinely reported, with Western-European controls.
// Spacing is tighter than the disc diameter so no corridor falls between them.
var SamplePoints = []SamplePoint{
	// --- Reported interference regions ---
	{Name: "baltic_kaliningrad", Lat: 54.7, Lon: 20.5, RadiusNM: 250},
	{Name: "gulf_of_finland", Lat: 59.4, Lon: 24.8, RadiusNM: 250},
	{Name: "baltic_south", Lat: 55.5, Lon: 16.5, RadiusNM: 250},
	{Name: "finland_north", Lat: 65.5, Lon: 26.0, RadiusNM: 250},
	{Name: "black_sea_west", Lat: 44.5, Lon: 31.0, RadiusNM: 250},
	{Name: "black_sea_east", Lat: 43.0, Lon: 38.0, RadiusNM: 250},
	{Name: "eastern_med", Lat: 34.8, Lon: 33.5, RadiusNM: 250},
	{Name: "levant_north", Lat: 36.5, Lon: 36.5, RadiusNM: 250},
	{Name: "caucasus", Lat: 41.0, Lon: 44.0, RadiusNM: 250},
	{Name: "poland_east", Lat: 52.2, Lon: 23.0, RadiusNM: 250},
	{Name: "romania_moldova", Lat: 46.5, Lon: 27.5, RadiusNM: 250},
	// --- Control regions, expected quiet ---
	{Name: "alps_switzerland", Lat: 47.0, Lon: 8.3, RadiusNM: 250, Control: true},
	{Name: "uk_south", Lat: 51.5, Lon: -0.5, RadiusNM: 250, Control: true},
	{Name: "iberia_central", Lat: 40.4, Lon: -3.7, RadiusNM: 250, Control: true},
	{Name: "france_central", Lat: 46.5, Lon: 2.5, RadiusNM: 250, Control: true},
	{Name: "germany_central", Lat: 50.5, Lon: 9.5, RadiusNM: 250, Control: true},
	{Name: "italy_central", Lat: 42.5, Lon: 12.5, RadiusNM: 250, Control: true},
	{Name: "scandinavia_west", Lat: 59.9, Lon: 10.7, RadiusNM: 250, Control: true},
}
