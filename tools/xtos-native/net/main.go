// xtos-net: XTOS native network interface management.
// Replaces the developer-facing subset of iproute2 (link show/up/down, addr show/add/flush).
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"syscall"
	"unsafe"
)

// Interface state and configuration from sysfs + ioctl.
type InterfaceInfo struct {
	Name       string   `json:"name"`
	Up         bool     `json:"up"`
	OperState  string   `json:"oper_state"`
	MAC        string   `json:"mac"`
	MTU        int      `json:"mtu"`
	SpeedMbps  int      `json:"speed_mbps"`
	Carrier    bool     `json:"carrier"`
	Duplex     string   `json:"duplex"`
	Addresses  []string `json:"addresses,omitempty"`
}

var safeName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,14}$`)

// readFirstLine reads the first line of a sysfs file.
func readFirstLine(path string) string {
	data, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	lines := strings.SplitN(string(data), "\n", 2)
	return strings.TrimSpace(lines[0])
}

// readInt reads an integer from a sysfs file.
func readInt(path string) int {
	value, err := strconv.Atoi(readFirstLine(path))
	if err != nil {
		return 0
	}
	return value
}

// validateName rejects unsafe interface names.
func validateName(name string) error {
	if name == "lo" || !safeName.MatchString(name) {
		return fmt.Errorf("invalid interface name: %q", name)
	}
	if _, err := os.Stat(filepath.Join("/sys/class/net", name)); err != nil {
		return fmt.Errorf("interface %q does not exist", name)
	}
	return nil
}

// getFlags reads interface flags via ioctl.
func getFlags(name string) (uint32, error) {
	socket, err := syscall.Socket(syscall.AF_INET, syscall.SOCK_DGRAM, 0)
	if err != nil {
		return 0, err
	}
	defer syscall.Close(socket)

	ifreq := struct {
		Name  [16]byte
		Flags uint16
	}{}
	copy(ifreq.Name[:], name)
	_, _, errno := syscall.Syscall(syscall.SYS_IOCTL, uintptr(socket),
		syscall.SIOCGIFFLAGS, uintptr(unsafe.Pointer(&ifreq)))
	if errno != 0 {
		return 0, errno
	}
	return uint32(ifreq.Flags), nil
}

// setFlags modifies interface flags via ioctl.
func setFlags(name string, flags uint32) error {
	socket, err := syscall.Socket(syscall.AF_INET, syscall.SOCK_DGRAM, 0)
	if err != nil {
		return err
	}
	defer syscall.Close(socket)

	ifreq := struct {
		Name  [16]byte
		Flags uint16
	}{}
	copy(ifreq.Name[:], name)
	ifreq.Flags = uint16(flags)
	_, _, errno := syscall.Syscall(syscall.SYS_IOCTL, uintptr(socket),
		syscall.SIOCSIFFLAGS, uintptr(unsafe.Pointer(&ifreq)))
	if errno != 0 {
		return errno
	}
	return nil
}

// interfaceUp brings an interface up.
func interfaceUp(name string) error {
	flags, err := getFlags(name)
	if err != nil {
		return err
	}
	return setFlags(name, flags|syscall.IFF_UP)
}

// interfaceDown takes an interface down.
func interfaceDown(name string) error {
	flags, err := getFlags(name)
	if err != nil {
		return err
	}
	return setFlags(name, flags & ^uint32(syscall.IFF_UP))
}

// addAddress assigns an IPv4 address using netlink.
func addAddress(name, cidr string) error {
	ip, ipnet, err := net.ParseCIDR(cidr)
	if err != nil {
		return fmt.Errorf("invalid CIDR %q: %w", cidr, err)
	}
	if ip.To4() == nil {
		return fmt.Errorf("only IPv4 addresses are supported")
	}
	prefix, _ := ipnet.Mask.Size()
	return netlinkAddAddress(name, ip.To4(), prefix)
}

// flushAddresses removes all IPv4 addresses from an interface.
func flushAddresses(name string) error {
	iface, err := net.InterfaceByName(name)
	if err != nil {
		return err
	}
	addrs, err := iface.Addrs()
	if err != nil {
		return err
	}
	var lastErr error
	for _, addr := range addrs {
		if ipnet, ok := addr.(*net.IPNet); ok && ipnet.IP.To4() != nil {
			ones, _ := ipnet.Mask.Size()
			if err := netlinkDelAddress(name, ipnet.IP.To4(), ones); err != nil {
				lastErr = err
			}
		}
	}
	return lastErr
}

// netlinkAddAddress sends RTM_NEWADDR via AF_NETLINK.
func netlinkAddAddress(name string, ip []byte, prefix int) error {
	return netlinkAddrOp(syscall.RTM_NEWADDR, name, ip, prefix)
}

// netlinkDelAddress sends RTM_DELADDR via AF_NETLINK.
func netlinkDelAddress(name string, ip []byte, prefix int) error {
	return netlinkAddrOp(syscall.RTM_DELADDR, name, ip, prefix)
}

// netlinkAddrOp performs an address operation via netlink.
func netlinkAddrOp(op int, name string, ip []byte, prefix int) error {
	socket, err := syscall.Socket(syscall.AF_NETLINK, syscall.SOCK_RAW, syscall.NETLINK_ROUTE)
	if err != nil {
		return err
	}
	defer syscall.Close(socket)

	ifindex := 0
	if iface, err := net.InterfaceByName(name); err == nil {
		ifindex = iface.Index
	} else {
		return err
	}

	// Build netlink message: header + ifaddrmsg + rta + ip
	type ifaddrmsg struct {
		Family    uint8
		Prefixlen uint8
		Flags     uint8
		Scope     uint8
		Index     uint32
	}
	type rtattr struct {
		Len   uint16
		Type  uint16
	}

	msgLen := syscall.SizeofNlMsghdr + 8 + 8 + 4 + len(ip) // aligned
	buf := make([]byte, msgLen)

	// Netlink header
	nl := (*syscall.NlMsghdr)(unsafe.Pointer(&buf[0]))
	nl.Len = uint32(msgLen)
	nl.Type = uint16(op)
	nl.Flags = syscall.NLM_F_REQUEST | syscall.NLM_F_ACK | syscall.NLM_F_CREATE
	nl.Seq = 1
	nl.Pid = uint32(os.Getpid())

	// ifaddrmsg
	ifa := (*ifaddrmsg)(unsafe.Pointer(&buf[syscall.SizeofNlMsghdr]))
	ifa.Family = syscall.AF_INET
	ifa.Prefixlen = uint8(prefix)
	ifa.Scope = syscall.RT_SCOPE_UNIVERSE
	ifa.Index = uint32(ifindex)

	// rtattr for IFA_LOCALADDR
	offset := syscall.SizeofNlMsghdr + 8
	attr := (*rtattr)(unsafe.Pointer(&buf[offset]))
	attr.Len = uint16(4 + len(ip))
	attr.Type = syscall.IFA_LOCALADDR
	copy(buf[offset+4:], ip)

	// Send
	dst := &syscall.SockaddrNetlink{Family: syscall.AF_NETLINK}
	if err := syscall.Sendto(socket, buf, 0, dst); err != nil {
		return err
	}

	// Receive ACK
	ack := make([]byte, 4096)
	n, _, err := syscall.Recvfrom(socket, ack, 0)
	if err != nil {
		return err
	}
	if n < int(syscall.SizeofNlMsghdr) {
		return fmt.Errorf("short netlink response")
	}
	resp := (*syscall.NlMsghdr)(unsafe.Pointer(&ack[0]))
	if resp.Type == syscall.NLMSG_ERROR {
		if n >= int(syscall.SizeofNlMsghdr)+4 {
			errno := *(*int32)(unsafe.Pointer(&ack[syscall.SizeofNlMsghdr]))
			if errno != 0 {
				return fmt.Errorf("netlink error: %d", errno)
			}
		}
	}
	return nil
}

// collectInterface gathers complete information for one interface.
func collectInterface(name string) InterfaceInfo {
	info := InterfaceInfo{Name: name}
	base := filepath.Join("/sys/class/net", name)

	info.OperState = readFirstLine(filepath.Join(base, "operstate"))
	flags, err := getFlags(name)
	if err == nil {
		info.Up = flags&syscall.IFF_UP != 0
	}
	info.MTU = readInt(filepath.Join(base, "mtu"))
	info.SpeedMbps = readInt(filepath.Join(base, "speed"))
	info.Carrier = readInt(filepath.Join(base, "carrier")) == 1
	info.Duplex = readFirstLine(filepath.Join(base, "duplex"))
	info.MAC = readFirstLine(filepath.Join(base, "address"))

	if iface, err := net.InterfaceByName(name); err == nil {
		addrs, _ := iface.Addrs()
		for _, addr := range addrs {
			if !strings.HasPrefix(addr.String(), "fe80") {
				info.Addresses = append(info.Addresses, addr.String())
			}
		}
	}
	return info
}

// listInterfaces returns all interfaces except lo.
func listInterfaces() []InterfaceInfo {
	var result []InterfaceInfo
	entries, err := os.ReadDir("/sys/class/net")
	if err != nil {
		return result
	}
	for _, entry := range entries {
		if entry.Name() == "lo" {
			continue
		}
		result = append(result, collectInterface(entry.Name()))
	}
	return result
}

// formatText renders human-readable interface table.
func formatText(interfaces []InterfaceInfo) string {
	var b strings.Builder
	b.WriteString("NAME       STATE   MTU    SPEED     ADDRESSES\n")
	for _, iface := range interfaces {
		state := "down"
		if iface.Up {
			state = "up"
		}
		addrs := strings.Join(iface.Addresses, ", ")
		if addrs == "" {
			addrs = "-"
		}
		speed := "-"
		if iface.SpeedMbps > 0 {
			speed = fmt.Sprintf("%dMb/s", iface.SpeedMbps)
		}
		b.WriteString(fmt.Sprintf("%-10s %-6s %-6d %-9s %s\n",
			iface.Name, state, iface.MTU, speed, addrs))
	}
	return b.String()
}

func main() {
	jsonFlag := flag.Bool("json", false, "output JSON")
	flag.Parse()

	args := flag.Args()
	if len(args) == 0 {
		// Default: show all
		if *jsonFlag {
			output, _ := json.MarshalIndent(listInterfaces(), "", "  ")
			fmt.Println(string(output))
		} else {
			fmt.Print(formatText(listInterfaces()))
		}
		return
	}

	command := args[0]
	switch command {
	case "show":
		if len(args) == 1 {
			if *jsonFlag {
				output, _ := json.MarshalIndent(listInterfaces(), "", "  ")
				fmt.Println(string(output))
			} else {
				fmt.Print(formatText(listInterfaces()))
			}
			return
		}
		if len(args) != 2 {
			fmt.Fprintln(os.Stderr, "usage: xtos-net show [interface]")
			os.Exit(2)
		}
		if err := validateName(args[1]); err != nil {
			fmt.Fprintln(os.Stderr, "xtos-net: "+err.Error())
			os.Exit(1)
		}
		info := collectInterface(args[1])
		if *jsonFlag {
			output, _ := json.MarshalIndent(info, "", "  ")
			fmt.Println(string(output))
		} else {
			fmt.Print(formatText([]InterfaceInfo{info}))
		}

	case "up":
		if len(args) != 2 {
			fmt.Fprintln(os.Stderr, "usage: xtos-net up <interface>")
			os.Exit(2)
		}
		if err := validateName(args[1]); err != nil {
			fmt.Fprintln(os.Stderr, "xtos-net: "+err.Error())
			os.Exit(1)
		}
		if err := interfaceUp(args[1]); err != nil {
			fmt.Fprintln(os.Stderr, "xtos-net: "+err.Error())
			os.Exit(1)
		}
		fmt.Printf("XTOS_NET_UP interface=%s\n", args[1])

	case "down":
		if len(args) != 2 {
			fmt.Fprintln(os.Stderr, "usage: xtos-net down <interface>")
			os.Exit(2)
		}
		if err := validateName(args[1]); err != nil {
			fmt.Fprintln(os.Stderr, "xtos-net: "+err.Error())
			os.Exit(1)
		}
		if err := interfaceDown(args[1]); err != nil {
			fmt.Fprintln(os.Stderr, "xtos-net: "+err.Error())
			os.Exit(1)
		}
		fmt.Printf("XTOS_NET_DOWN interface=%s\n", args[1])

	case "addr":
		if len(args) < 2 {
			fmt.Fprintln(os.Stderr, "usage: xtos-net addr add|flush <interface> [cidr]")
			os.Exit(2)
		}
		switch args[1] {
		case "add":
			if len(args) != 4 {
				fmt.Fprintln(os.Stderr, "usage: xtos-net addr add <interface> <cidr>")
				os.Exit(2)
			}
			if err := validateName(args[2]); err != nil {
				fmt.Fprintln(os.Stderr, "xtos-net: "+err.Error())
				os.Exit(1)
			}
			if err := addAddress(args[2], args[3]); err != nil {
				fmt.Fprintln(os.Stderr, "xtos-net: "+err.Error())
				os.Exit(1)
			}
			fmt.Printf("XTOS_NET_ADDR_ADD interface=%s address=%s\n", args[2], args[3])
		case "flush":
			if len(args) != 3 {
				fmt.Fprintln(os.Stderr, "usage: xtos-net addr flush <interface>")
				os.Exit(2)
			}
			if err := validateName(args[2]); err != nil {
				fmt.Fprintln(os.Stderr, "xtos-net: "+err.Error())
				os.Exit(1)
			}
			if err := flushAddresses(args[2]); err != nil {
				fmt.Fprintln(os.Stderr, "xtos-net: "+err.Error())
				os.Exit(1)
			}
			fmt.Printf("XTOS_NET_ADDR_FLUSH interface=%s\n", args[2])
		default:
			fmt.Fprintln(os.Stderr, "xtos-net: unknown addr subcommand: "+args[1])
			os.Exit(2)
		}

	default:
		fmt.Fprintln(os.Stderr, "xtos-net: unknown command: "+command)
		fmt.Fprintln(os.Stderr, "commands: show [iface], up, down, addr add/flush")
		os.Exit(2)
	}
}
