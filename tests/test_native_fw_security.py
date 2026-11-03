#!/usr/bin/env python3
"""Real production-C firewall regression; every nft operation is netns-isolated."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
FW = ROOT / "tools/xtos-native/fw/fw.c"


def build_fw(directory):
    binary = Path(directory) / "fw"
    subprocess.run(["gcc", "-O2", "-Wall", "-Wextra", "-Werror", "-static",
                    "-I", str(ROOT / "tools/xtos-native/common"),
                    "-o", str(binary), str(FW)], check=True)
    return binary


def command(*args, ok=True):
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=15)
    if ok:
        assert result.returncode == 0, (args, result.returncode, result.stdout, result.stderr)
    return result


def inside(binary, outside):
    assert os.readlink("/proc/self/ns/net") != outside, "refusing host netns"
    command("ip", "link", "set", "lo", "up")
    # Child server is a second netns; the caller is an isolated client netns.
    server_code = '''import socket,time
sockets=[]
for family,host in ((socket.AF_INET,'0.0.0.0'),(socket.AF_INET6,'::')):
 for port in (22,8080,18080):
  s=socket.socket(family);s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
  if family==socket.AF_INET6:s.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1)
  s.bind((host,port));s.listen(64);sockets.append(s)
print('ready',flush=True)
time.sleep(300)
'''
    server = subprocess.Popen(["unshare", "-n", sys.executable, "-c", server_code],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert server.stdout is not None
        assert server.stdout.readline().strip() == "ready"
        prefix = ["nsenter", "-t", str(server.pid), "-n"]
        def fw(*args, ok=True):
            return command(*prefix, binary, *args, ok=ok)
        def nft(*args):
            return command(*prefix, "nft", *args).stdout
        command("ip", "link", "add", "client", "type", "veth", "peer", "name", "server")
        command("ip", "link", "set", "server", "netns", str(server.pid))
        command("ip", "addr", "add", "192.0.2.2/24", "dev", "client")
        command("ip", "addr", "add", "192.0.2.3/24", "dev", "client")
        command("ip", "link", "set", "client", "up")
        command(*prefix, "ip", "addr", "add", "192.0.2.1/24", "dev", "server")
        command(*prefix, "ip", "link", "set", "server", "up")
        command("ip", "-6", "addr", "add", "2001:db8::2/64", "dev", "client", "nodad")
        command("ip", "-6", "addr", "add", "2001:db8::3/64", "dev", "client", "nodad")
        command(*prefix, "ip", "-6", "addr", "add", "2001:db8::1/64", "dev", "server", "nodad")
        command(*prefix, "ip", "link", "set", "lo", "up")
        def connects6(port, source="2001:db8::2"):
            with socket.socket(socket.AF_INET6) as sock:
                sock.settimeout(0.4)
                sock.bind((source, 0))
                return sock.connect_ex(("2001:db8::1", port)) == 0
        def connects(port, source="192.0.2.2"):
            with socket.socket() as sock:
                sock.settimeout(0.4)
                sock.bind((source, 0))
                return sock.connect_ex(("192.0.2.1", port)) == 0
        assert connects(22), "positive control: server/route unavailable"
        assert connects(18080), "positive control: counterexample TCP server unavailable"
        assert connects6(22), "positive control: IPv6 server/route unavailable"
        print("PASS transport_positive_control", flush=True)
        fw("enable")
        assert not connects6(22), "default deny missing: unsolicited IPv6 SSH connected"
        assert not connects(22), "default deny missing: unsolicited SSH connected"
        assert not connects(8080), "default deny missing: unsolicited TCP connected"
        print("PASS default_drop_packets", flush=True)
        table = nft("list", "table", "inet", "xtos_fw")
        assert "type filter hook input priority filter; policy drop;" in table
        assert "type filter hook output priority filter; policy accept;" in table
        assert "enabled=true" in fw("status").stdout
        print("PASS base_hooks_status", flush=True)
        # QUALITY3-FW-001: the exact hooked input jump reaches this chain.
        # Prove the packet bypass before checking status (not a text-only fixture).
        assert connects(18080) is False
        nft("add", "rule", "inet", "xtos_fw", "xtos_user", "accept")
        assert connects(18080), "counterexample did not open fresh external TCP"
        degraded = fw("status", ok=False)
        print("COUNTEREXAMPLE " + json.dumps({"connected": True,
              "exit": degraded.returncode, "status": degraded.stdout}), flush=True)
        assert degraded.returncode != 0 and "enabled=true" not in degraded.stdout, \
            "QUALITY3-FW-001: reached unowned accept falsely healthy"
        print("PASS status_rejects_user_accept_packet_bypass", flush=True)
        fw("flush")
        assert not connects(18080)
        # All CLI schemas remain healthy: both actions, protocols and source kinds.
        for action in ("allow", "deny"):
            for proto in ("tcp", "udp"):
                for source in (None, "192.0.2.2/32", "192.0.2.99/24",
                               "2001:db8::2/128", "2001:db8::abcd/64",
                               "192.0.2.2/0", "2001:db8::2/0"):
                    args = [action, "--proto", proto, "--port", "8080"]
                    if source is not None:
                        args += ["--from", source]
                    fw(*args)
                    assert "enabled=true" in fw("status").stdout, (action, proto, source)
                    print(f"PASS user_schema_{action}_{proto}_{source or 'any'}", flush=True)
                    fw("flush")
        # A plausible owner comment is not sufficient: schema must match exactly.
        bad_rules = [
            ("unowned_drop", ["tcp", "dport", "8080", "drop"]),
            ("forged_bare_accept", ["accept", "comment", '"xtos:rule:tcp:8080:any"']),
            ("missing_owner", ["tcp", "dport", "8080", "accept"]),
            ("mismatched_owner", ["tcp", "dport", "8080", "accept", "comment", '"xtos:rule:tcp:8081:any"']),
            ("noncanonical_owner", ["tcp", "dport", "8080", "accept", "comment", '"xtos:rule:tcp:08080:any"']),
            ("unknown_condition", ["meta", "mark", "1", "tcp", "dport", "8080", "accept", "comment", '"xtos:rule:tcp:8080:any"']),
            ("unknown_counter", ["tcp", "dport", "8080", "counter", "accept", "comment", '"xtos:rule:tcp:8080:any"']),
            ("wrong_source", ["ip", "saddr", "192.0.2.3", "tcp", "dport", "8080", "accept", "comment", '"xtos:rule:tcp:8080:192.0.2.2/32"']),
            ("noncanonical_source_owner", ["ip", "saddr", "192.0.2.0/24", "tcp", "dport", "8080", "accept", "comment", '"xtos:rule:tcp:8080:192.0.2.2/24"']),
            ("unknown_verdict", ["tcp", "dport", "8080", "return", "comment", '"xtos:rule:tcp:8080:any"']),
        ]
        for name, rule in bad_rules:
            nft("add", "rule", "inet", "xtos_fw", "xtos_user", *rule)
            degraded = fw("status", ok=False)
            assert degraded.returncode != 0 and "enabled=true" not in degraded.stdout, name
            print("PASS user_rejects_" + name, flush=True)
            fw("flush")
            assert "enabled=true" in fw("status").stdout
        # The kernel itself forbids converting a reached regular chain to a
        # base hook; retain that boundary rather than assuming mutation worked.
        before_hook = nft("list", "table", "inet", "xtos_fw")
        hook = command(*prefix, "nft", "add", "chain", "inet", "xtos_fw", "xtos_user",
                       "{ type filter hook prerouting priority -10; policy accept; }", ok=False)
        assert hook.returncode != 0
        assert nft("list", "table", "inet", "xtos_fw") == before_hook
        assert "enabled=true" in fw("status").stdout
        print("PASS user_hook_policy_kernel_rejected", flush=True)
        unscoped = fw("allow", "--proto", "tcp", "--port", "22", ok=False)
        assert unscoped.returncode != 0, "unscoped SSH allow must require management CIDR"
        print("PASS ssh_requires_management_source", flush=True)
        fw("allow", "--proto", "tcp", "--port", "22", "--from", "192.0.2.2/32")
        assert connects(22), "explicit management CIDR not allowed"
        assert not connects(22, "192.0.2.3"), "management CIDR bypass"
        fw("allow", "--proto", "tcp", "--port", "22", "--from", "2001:db8::2/128")
        assert connects6(22), "explicit IPv6 management source not allowed"
        assert not connects6(22, "2001:db8::3"), "IPv6 management CIDR bypass"
        command(*prefix, sys.executable, "-c",
                "import socket;s=socket.create_connection(('127.0.0.1',22),1);s.close()")
        print("PASS loopback_packets", flush=True)
        print("PASS allow_source_scoped_packets", flush=True)
        # Parser/injection failures must never mutate the real ruleset.
        before = nft("list", "table", "inet", "xtos_fw")
        invalid = [
            ("--port", "22junk"), ("--port", "+22"), ("--port", "22;touch /tmp/fw-injected"),
            ("--port", "9999999999999999999999999"),
            ("--from", "192.0.2.2/33"), ("--from", "::1/129"),
            ("--from", "192.0.2.2;accept"), ("--from", "192.0.2.2/24 garbage"),
            ("--from", "$(id)"), ("--from", "fe80::1%server"),
            ("--from", "192.0.2.2/"), ("--unknown", "x"),
        ]
        for key, value in invalid:
            argv = ["allow", "--proto", "tcp", "--port", "8080"]
            if key == "--port":
                argv[-1] = value
            else:
                argv.extend([key, value])
            rejected = fw(*argv, ok=False)
            assert rejected.returncode != 0 and "FW_ALLOW" not in rejected.stdout, (key, value)
        assert before == nft("list", "table", "inet", "xtos_fw"), "invalid argv changed policy"
        print("PASS strict_parser_injection_no_mutation", flush=True)
        # Re-enabling a populated table is an atomic failure, not a policy reset.
        repeated = fw("enable", ok=False)
        assert repeated.returncode != 0 and "XTOS_FW_ENABLE" not in repeated.stdout
        assert before == nft("list", "table", "inet", "xtos_fw")
        print("PASS enable_failure_atomic", flush=True)
        fw("allow", "--proto", "tcp", "--port", "8080")
        assert connects(8080)
        fw("deny", "--proto", "tcp", "--port", "8080")
        assert not connects(8080), "deny must override an earlier allow for new flows"
        print("PASS deny_packets", flush=True)
        fw("delete", "--proto", "tcp", "--port", "8080")
        assert connects(8080), "delete should remove first matching owner rule (deny) only"
        print("PASS delete_owned_rule", flush=True)
        # Deletion is exact-scope, bounded, and cannot remove an unowned rule.
        nft("add", "rule", "inet", "xtos_fw", "xtos_user", "tcp", "dport", "9090", "drop")
        unowned = fw("delete", "--proto", "tcp", "--port", "9090", ok=False)
        assert unowned.returncode != 0
        assert "dport 9090 drop" in nft("list", "table", "inet", "xtos_fw")
        fw("allow", "--proto", "tcp", "--port", "8081", "--from", "192.0.2.2/32")
        fw("allow", "--proto", "tcp", "--port", "8081", "--from", "192.0.2.3/32")
        fw("delete", "--proto", "tcp", "--port", "8081", "--from", "192.0.2.2/32")
        scoped = nft("list", "table", "inet", "xtos_fw")
        assert 'xtos:rule:tcp:8081:192.0.2.2/32' not in scoped
        assert 'xtos:rule:tcp:8081:192.0.2.3/32' in scoped
        print("PASS delete_exact_scope_unowned_retained", flush=True)
        fw("flush")
        assert not connects(22) and not connects6(22) and not connects(8080), "flush opened ingress"
        table = nft("list", "table", "inet", "xtos_fw")
        assert 'iifname "lo" accept' in table and "ct state established,related accept" in table
        assert "policy drop" in table and "enabled=true" in fw("status").stdout
        print("PASS flush_retains_system_policy", flush=True)
        # A leading accept defeats default-drop even though policy is still drop.
        nft("insert", "rule", "inet", "xtos_fw", "xtos_input", "accept")
        degraded = fw("status", ok=False)
        assert degraded.returncode != 0 and "enabled=true" not in degraded.stdout, "status trusted policy despite unconditional accept"
        print("PASS status_rejects_accept_bypass", flush=True)
        # Table existence alone must never mean enabled.
        nft("flush", "chain", "inet", "xtos_fw", "xtos_input")
        degraded = fw("status", ok=False)
        assert degraded.returncode != 0 and "enabled=true" not in degraded.stdout
        print("PASS status_rejects_degraded_policy", flush=True)
        fw("disable", "--confirm")
        assert "enabled=false" in fw("status").stdout
        print("PASS disabled_status", flush=True)
    finally:
        server.terminate()
        server.communicate(timeout=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inside", nargs=2)
    args = parser.parse_args()
    if args.inside:
        inside(*args.inside)
        return 0
    required = ("gcc", "nft", "unshare", "nsenter", "ip")
    missing = [name for name in required if not shutil.which(name)]
    if missing:
        print("SKIP guest pending: missing " + ",".join(missing))
        return 77
    probe = command("unshare", "-Urn", "nft", "list", "ruleset", ok=False)
    if probe.returncode:
        print("SKIP guest pending: network namespace/CAP_NET_ADMIN unavailable: " + probe.stderr)
        return 77
    with tempfile.TemporaryDirectory(prefix="fw-security-", dir=ROOT / ".build") as directory:
        binary = build_fw(directory)
        result = subprocess.run(["unshare", "-Urn", sys.executable, __file__, "--inside",
                                 str(binary), os.readlink("/proc/self/ns/net")], timeout=60)
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
