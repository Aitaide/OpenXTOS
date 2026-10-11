// xtos-info: XTOS native system information command.
// Replaces the common pattern of uname + os-release + realtime + cpu/mem/net queries.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

type SystemInfo struct {
	ProductName    string `json:"product_name"`
	ProductLabel   string `json:"product_label"`
	Version        string `json:"version"`
	ReleaseName    string `json:"release_name"`
	ImageType      string `json:"image_type"`
	KernelRelease  string `json:"kernel_release"`
	KernelArch     string `json:"kernel_arch"`
	RealtimeActive bool   `json:"realtime_active"`
	CPUCores       int    `json:"cpu_cores"`
	MemoryTotalKB  int64  `json:"memory_total_kb"`
	MemoryAvailKB  int64  `json:"memory_avail_kb"`
	Storage        string `json:"storage"`
	BuildID        string `json:"build_id"`
	SourceState    string `json:"source_state"`
	Hostname       string `json:"hostname"`
	UptimeSeconds  int64  `json:"uptime_seconds"`
	Interfaces     []NetInterface `json:"interfaces"`
}

type NetInterface struct {
	Name       string `json:"name"`
	Up         bool   `json:"up"`
	Addresses  []string `json:"addresses,omitempty"`
	SpeedMbps  int    `json:"speed_mbps"`
	Carrier    bool   `json:"carrier"`
}

// readFirstLine reads the first line of a sysfs/proc file, trimming whitespace.
func readFirstLine(path string) string {
	data, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	lines := strings.SplitN(string(data), "\n", 2)
	return strings.TrimSpace(lines[0])
}

// readInt reads an integer from a sysfs/proc file.
func readInt(path string) int64 {
	value, err := strconv.ParseInt(readFirstLine(path), 10, 64)
	if err != nil {
		return 0
	}
	return value
}

// parseKeyValue reads a key: value line from /proc/meminfo style files.
func meminfoValue(content, key string) int64 {
	for _, line := range strings.Split(content, "\n") {
		if strings.HasPrefix(line, key+":") {
			fields := strings.Fields(line)
			if len(fields) >= 2 {
				value, _ := strconv.ParseInt(fields[1], 10, 64)
				return value
			}
		}
	}
	return 0
}

// countCPUs counts unique processor entries in /proc/cpuinfo.
func countCPUs() int {
	data, err := os.ReadFile("/proc/cpuinfo")
	if err != nil {
		return 0
	}
	count := 0
	for _, line := range strings.Split(string(data), "\n") {
		if strings.HasPrefix(line, "processor") {
			count++
		}
	}
	return count
}

// kernelArch returns the machine architecture from uname.
func kernelArch() string {
	var uts syscall.Utsname
	if err := syscall.Uname(&uts); err != nil {
		return "unknown"
	}
	return charsToString(uts.Machine[:])
}

// charsToString converts a fixed-size int8 array to a Go string.
func charsToString(chars []int8) string {
	var builder strings.Builder
	for _, c := range chars {
		if c == 0 {
			break
		}
		builder.WriteByte(byte(c))
	}
	return builder.String()
}

// collectInterfaces enumerates network interfaces with their status.
func collectInterfaces() []NetInterface {
	var result []NetInterface
	entries, err := os.ReadDir("/sys/class/net")
	if err != nil {
		return result
	}
	for _, entry := range entries {
		name := entry.Name()
		if name == "lo" {
			continue
		}
		iface := NetInterface{Name: name}
		// Check operstate
		state := readFirstLine(filepath.Join("/sys/class/net", name, "operstate"))
		iface.Up = state == "up" || state == "unknown"
		// Speed
		iface.SpeedMbps = int(readInt(filepath.Join("/sys/class/net", name, "speed")))
		// Carrier
		carrier := readInt(filepath.Join("/sys/class/net", name, "carrier"))
		iface.Carrier = carrier == 1
		// Addresses via Go net package
		if netIface, err := net.InterfaceByName(name); err == nil {
			addrs, _ := netIface.Addrs()
			for _, addr := range addrs {
				if !strings.Contains(addr.String(), ":") || strings.Contains(addr.String(), "fe80") {
					// Include IPv4 and global IPv6
					if strings.Contains(addr.String(), "fe80") {
						continue // skip link-local
					}
					iface.Addresses = append(iface.Addresses, addr.String())
				}
			}
		}
		result = append(result, iface)
	}
	return result
}

// systemUptime reads /proc/uptime and returns the integer part.
func systemUptime() int64 {
	data, err := os.ReadFile("/proc/uptime")
	if err != nil {
		return 0
	}
	fields := strings.Fields(string(data))
	if len(fields) < 1 {
		return 0
	}
	value, _ := strconv.ParseFloat(fields[0], 64)
	return int64(value)
}

// gather collects all system information.
func gather() SystemInfo {
	info := SystemInfo{
		ProductName:   readFirstLine("/etc/xtos-release"),
		KernelRelease: readFirstLine("/proc/sys/kernel/osrelease"),
		KernelArch:    kernelArch(),
		CPUCores:      countCPUs(),
		Storage:       "Live overlay (RAM), persistence=0",
		Hostname:      readFirstLine("/proc/sys/kernel/hostname"),
		UptimeSeconds: systemUptime(),
	}

	// XTOS identity from xtos-release key=value format
	if data, err := os.ReadFile("/etc/xtos-release"); err == nil {
		for _, line := range strings.Split(string(data), "\n") {
			if value, found := strings.CutPrefix(line, "XTOS_VERSION="); found {
				info.Version = value
			}
			if value, found := strings.CutPrefix(line, "XTOS_LABEL="); found {
				info.ProductLabel = value
			}
			if value, found := strings.CutPrefix(line, "XTOS_RELEASE_NAME="); found {
				info.ReleaseName = value
			}
			if value, found := strings.CutPrefix(line, "XTOS_IMAGE_TYPE="); found {
				info.ImageType = value
			}
		}
	}

	// Build metadata from xtos-build.json
	if data, err := os.ReadFile("/etc/xtos-build.json"); err == nil {
		var build map[string]string
		if json.Unmarshal(data, &build) == nil {
			info.BuildID = build["build_id"]
			info.SourceState = build["source_state"]
		}
	}

	// Realtime status
	info.RealtimeActive = readFirstLine("/sys/kernel/realtime") == "1"

	// Memory from /proc/meminfo
	if data, err := os.ReadFile("/proc/meminfo"); err == nil {
		content := string(data)
		info.MemoryTotalKB = meminfoValue(content, "MemTotal")
		info.MemoryAvailKB = meminfoValue(content, "MemAvailable")
	}

	// Network interfaces
	info.Interfaces = collectInterfaces()

	return info
}

// formatText renders human-readable output.
func formatText(info SystemInfo) string {
	var b strings.Builder
	if info.ProductLabel != "" {
		b.WriteString(info.ProductLabel)
	} else {
		b.WriteString("XTOS")
	}
	if info.Version != "" {
		b.WriteString(" " + info.Version)
	}
	if info.ReleaseName != "" {
		b.WriteString(" (" + info.ReleaseName + ")")
	}
	b.WriteString("\n")

	b.WriteString(fmt.Sprintf("Kernel:    %s %s\n", info.KernelRelease, info.KernelArch))
	rt := "inactive"
	if info.RealtimeActive {
		rt = "PREEMPT_RT active"
	}
	b.WriteString(fmt.Sprintf("Realtime:  %s\n", rt))
	b.WriteString(fmt.Sprintf("CPU:       %d cores, %s\n", info.CPUCores, info.KernelArch))
	if info.MemoryTotalKB > 0 {
		b.WriteString(fmt.Sprintf("Memory:    %d MB total, %d MB available\n",
			info.MemoryTotalKB/1024, info.MemoryAvailKB/1024))
	}
	b.WriteString(fmt.Sprintf("Storage:   %s\n", info.Storage))
	b.WriteString(fmt.Sprintf("Hostname:  %s\n", info.Hostname))

	uptime := time.Duration(info.UptimeSeconds) * time.Second
	b.WriteString(fmt.Sprintf("Uptime:    %s\n", uptime.Truncate(time.Second)))

	if len(info.Interfaces) > 0 {
		b.WriteString("Network:\n")
		for _, iface := range info.Interfaces {
			status := "down"
			if iface.Up {
				status = "up"
			}
			addrs := strings.Join(iface.Addresses, ", ")
			if addrs == "" {
				addrs = "no address"
			}
			b.WriteString(fmt.Sprintf("  %-8s %s  %s", iface.Name, status, addrs))
			if iface.SpeedMbps > 0 {
				b.WriteString(fmt.Sprintf("  %dMb/s", iface.SpeedMbps))
			}
			b.WriteString("\n")
		}
	}

	if info.BuildID != "" {
		state := info.SourceState
		if state == "" {
			state = "unknown"
		}
		b.WriteString(fmt.Sprintf("Build:     %s (source: %s)\n", info.BuildID, state))
	}

	return b.String()
}

func main() {
	jsonFlag := flag.Bool("json", false, "output machine-readable JSON")
	flag.Parse()

	info := gather()

	if *jsonFlag {
		output, err := json.MarshalIndent(info, "", "  ")
		if err != nil {
			fmt.Fprintln(os.Stderr, "xtos-info: "+err.Error())
			os.Exit(1)
		}
		fmt.Println(string(output))
		return
	}

	fmt.Print(formatText(info))
}
