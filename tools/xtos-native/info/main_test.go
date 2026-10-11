package main

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
)

// TestFormatTextIncludesCoreFields verifies the human-readable output has essential sections.
func TestFormatTextIncludesCoreFields(t *testing.T) {
	info := SystemInfo{
		ProductName:    "XTOS",
		ProductLabel:   "XTOS Bate版本",
		Version:        "0.9.0-devpreview.1",
		ReleaseName:    "Developer Preview 1",
		KernelRelease:  "6.6.0-rt15-xtos-live.1",
		KernelArch:     "x86_64",
		RealtimeActive: true,
		CPUCores:       2,
		MemoryTotalKB:  3000000,
		MemoryAvailKB:  2800000,
		Storage:        "Live overlay (RAM), persistence=0",
		Hostname:       "xtos-live",
		BuildID:        "test-build",
		SourceState:    "dirty",
		Interfaces: []NetInterface{
			{Name: "eth0", Up: true, Addresses: []string{"10.0.2.15/24"}, SpeedMbps: 1000, Carrier: true},
		},
	}

	text := formatText(info)
	for _, expected := range []string{
		"XTOS Bate版本 0.9.0-devpreview.1 (Developer Preview 1)",
		"6.6.0-rt15-xtos-live.1 x86_64",
		"PREEMPT_RT active",
		"2 cores",
		"2929 MB total",
		"eth0",
		"10.0.2.15/24",
		"1000Mb/s",
		"test-build",
		"dirty",
	} {
		if !strings.Contains(text, expected) {
			t.Errorf("formatText missing %q in:\n%s", expected, text)
		}
	}
}

// TestFormatTextRealtimeInactive checks the inactive rendering.
func TestFormatTextRealtimeInactive(t *testing.T) {
	info := SystemInfo{RealtimeActive: false}
	text := formatText(info)
	if !strings.Contains(text, "inactive") {
		t.Errorf("expected 'inactive' when realtime is off, got:\n%s", text)
	}
}

// TestJSONOutput round-trips the struct through JSON.
func TestJSONOutput(t *testing.T) {
	info := SystemInfo{
		Version:        "1.0.0",
		RealtimeActive: true,
		Interfaces:     []NetInterface{{Name: "eth0", Up: true}},
	}
	data, err := json.Marshal(info)
	if err != nil {
		t.Fatal(err)
	}
	var decoded SystemInfo
	if err := json.Unmarshal(data, &decoded); err != nil {
		t.Fatal(err)
	}
	if decoded.Version != "1.0.0" || !decoded.RealtimeActive || decoded.Interfaces[0].Name != "eth0" {
		t.Errorf("JSON round-trip lost data: %+v", decoded)
	}
}

// TestReadFirstLineMissingFile returns empty string for nonexistent paths.
func TestReadFirstLineMissingFile(t *testing.T) {
	if got := readFirstLine("/nonexistent/path"); got != "" {
		t.Errorf("expected empty string, got %q", got)
	}
}

// TestMeminfoValue parses standard meminfo format.
func TestMeminfoValue(t *testing.T) {
	content := "MemTotal:       3000000 kB\nMemFree:         2000000 kB\nMemAvailable:    2800000 kB\n"
	if got := meminfoValue(content, "MemTotal"); got != 3000000 {
		t.Errorf("MemTotal: got %d, want 3000000", got)
	}
	if got := meminfoValue(content, "MemAvailable"); got != 2800000 {
		t.Errorf("MemAvailable: got %d, want 2800000", got)
	}
	if got := meminfoValue(content, "Missing"); got != 0 {
		t.Errorf("Missing key: got %d, want 0", got)
	}
}

// TestCountCPUs handles missing procfs gracefully.
func TestCountCPUsMissingProc(t *testing.T) {
	// /proc/cpuinfo exists on Linux; just verify it returns a positive count.
	if count := countCPUs(); count < 1 && os.Getenv("XTOS_SKIP_PROC") == "" {
		t.Errorf("expected at least 1 CPU, got %d", count)
	}
}

// TestCharsToString converts int8 arrays correctly.
func TestCharsToString(t *testing.T) {
	input := []int8{'x', '8', '6', '_', '6', '4', 0, 0}
	if got := charsToString(input); got != "x86_64" {
		t.Errorf("got %q, want x86_64", got)
	}
}
