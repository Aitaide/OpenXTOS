#!/usr/bin/env python3
"""Static native tools with fixed, read-only OS-boundary fixtures."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "tools/xtos-native/common"
HARNESS = r"""
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <fcntl.h>
#include <dirent.h>
#include <sys/stat.h>
#include <sys/utsname.h>
#include <ifaddrs.h>
static void audit(const char *kind) {
    char p[4096];
    snprintf(p, sizeof(p), "%s/audit", FIXTURE_ROOT);
    FILE *f = fopen(p, "a");
    if (f) { fprintf(f, "%s\n", kind); fclose(f); }
}
static FILE *fixture_fopen(const char *path, const char *mode) {
    char p[4096];
    if (strcmp(mode, "r") != 0) { audit("write"); errno = EACCES; return NULL; }
    snprintf(p, sizeof(p), "%s%s", FIXTURE_ROOT, path);
    if (strcmp(path, "/sys/kernel/realtime") == 0) {
        char marker[4096];
        snprintf(marker, sizeof(marker), "%s/unreadable", FIXTURE_ROOT);
        FILE *f = fopen(marker, "r");
        if (f) { fclose(f); errno = EACCES; return NULL; }
    }
    return fopen(p, mode);
}
/* Explicit test fixture: production always calls the real uname syscall. */
static int fixture_uname(struct utsname *u) __attribute__((unused));
static int fixture_uname(struct utsname *u) {
    memset(u, 0, sizeof(*u));
    strcpy(u->sysname, "Linux");
    strcpy(u->machine, "x86_64");
    strcpy(u->release, "7.3.1-test-PREEMPT_RT");
    FILE *f = fixture_fopen("/uname-version", "r");
    if (!f) { errno = EIO; return -1; }
    if (!fgets(u->version, sizeof(u->version), f)) { fclose(f); return -1; }
    u->version[strcspn(u->version, "\n")] = 0;
    fclose(f);
    return 0;
}
static DIR *fixture_opendir(const char *path) {
    char p[4096];
    snprintf(p, sizeof(p), "%s%s", FIXTURE_ROOT, path);
    return opendir(p);
}
static int fixture_open(const char *path, int flags, ...) __attribute__((unused));
static int fixture_open(const char *path, int flags, ...) {
    (void)path; (void)flags; audit("open"); errno = EACCES; return -1;
}
static int fixture_stat(const char *path, struct stat *s) __attribute__((unused));
static int fixture_stat(const char *path, struct stat *s) {
    char p[4096];
    snprintf(p, sizeof(p), "%s%s", FIXTURE_ROOT, path);
    return stat(p, s);
}
static int fixture_getifaddrs(struct ifaddrs **p) __attribute__((unused));
static int fixture_getifaddrs(struct ifaddrs **p) { *p = NULL; return -1; }
static int fixture_system(const char *s) __attribute__((unused));
static int fixture_system(const char *s) { (void)s; audit("system"); return -1; }
#define fopen fixture_fopen
#define uname fixture_uname
#define opendir fixture_opendir
#define open fixture_open
#define stat(path, buf) fixture_stat(path, buf)
#define getifaddrs fixture_getifaddrs
#define system fixture_system
#include TOOL_SOURCE
"""


class RtKernelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="xtos-rt-kernel-")
        cls.work = Path(os.environ.get("XTOS_NATIVE_RT_ARTIFACTS", cls.temp.name))
        cls.work.mkdir(parents=True, exist_ok=True)
        cls.fixture = cls.work / "root"
        harness = cls.work / "fixture.c"
        harness.write_text(HARNESS, encoding="utf-8")
        cls.commands = []
        for tool in ("info", "rt"):
            cmd = ["gcc", "-O2", "-Wall", "-Wextra", "-Werror", "-static",
                   "-I", str(COMMON), "-MMD", "-MF", str(cls.work / (tool + ".d")),
                   "-DFIXTURE_ROOT=" + json.dumps(str(cls.fixture)),
                   "-DTOOL_SOURCE=" + json.dumps(str(ROOT / f"tools/xtos-native/{tool}/{tool}.c")),
                   "-o", str(cls.work / tool), str(harness)]
            cls.commands.append(cmd)
            r = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, "LC_ALL": "C"})
            (cls.work / (tool + ".compile.log")).write_text(r.stdout + r.stderr)
            if r.returncode:
                raise AssertionError(f"compilation failed: {cmd!r}\n{r.stderr}")
        (cls.work / "commands.json").write_text(json.dumps(cls.commands, indent=2))
        cls.executions = []

    @classmethod
    def tearDownClass(cls):
        (cls.work / "executions.json").write_text(json.dumps(cls.executions, indent=2))
        cls.temp.cleanup()

    def setUp(self):
        if self.fixture.exists():
            shutil.rmtree(self.fixture)
        self.fixture.mkdir()
        self.put("/uname-version", "#1 SMP PREEMPT_RT Sun Nov 1 00:00:00 UTC 2026")
        self.put("/proc/sys/kernel/sched_rt_runtime_us", "950000\n")
        self.put("/proc/sys/kernel/sched_rt_period_us", "1000000\n")

    def put(self, path, content):
        p = self.fixture / path.lstrip("/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode())

    def run_tool(self, tool, *args):
        r = subprocess.run([str(self.work / tool), *args], capture_output=True, text=True, timeout=10)
        self.executions.append({"test": self.id(), "tool": tool, "args": args,
                                "exit": r.returncode, "stdout": r.stdout, "stderr": r.stderr})
        return r

    def check_state(self, active):
        for tool, args, key in (("info", ["--json"], "realtime_active"),
                                ("rt", ["--json", "status"], "realtime_kernel")):
            r = self.run_tool(tool, *args)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIs(json.loads(r.stdout)[key], active)
        for tool, args in (("info", []), ("rt", ["status"])):
            r = self.run_tool(tool, *args)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual("PREEMPT_RT active" in r.stdout, active)
        # Invalid CPUs force a read-only stop immediately after the RT gate.
        r = self.run_tool("rt", "tune", "--housekeeping", "invalid")
        self.assertEqual(r.returncode, 2 if active else 1, r.stdout + r.stderr)
        self.assertEqual("refusing to tune a non-RT kernel" in r.stderr, not active)
        self.assertFalse((self.fixture / "audit").exists(), "write/helper attempted")

    def test_missing_node_uses_current_uname_rt_token(self):
        self.check_state(True)

    def test_sysfs_zero_overrides_rt_banner(self):
        self.put("/sys/kernel/realtime", "0\n")
        self.check_state(False)

    def test_sysfs_one_overrides_non_rt_banner(self):
        self.put("/sys/kernel/realtime", "1\n")
        self.put("/uname-version", "#1 SMP PREEMPT_DYNAMIC timestamp")
        self.check_state(True)

    def test_malformed_sysfs_never_becomes_positive(self):
        for value in (b"", b"1junk\n", b"2\n", b"01\n", b"+1\n", b"1\n0\n",
                      b"1\x00junk", b"1" + b" " * 512):
            with self.subTest(value=value):
                self.setUp()
                self.put("/sys/kernel/realtime", value)
                self.check_state(False)

    def test_unreadable_sysfs_never_falls_back(self):
        self.put("/sys/kernel/realtime", "1\n")
        self.put("/unreadable", "")
        self.check_state(False)

    def test_only_complete_version_token_is_accepted(self):
        for version in ("#1 SMP PREEMPT_DYNAMIC", "#1 SMP PREEMPT", "#1 SMP",
                        "#1 XPREEMPT_RT", "#1 PREEMPT_RT_FAKE", "#1 PREEMPT_RT-debug", ""):
            with self.subTest(version=version):
                self.setUp()
                self.put("/uname-version", version + "\n")
                self.check_state(False)

    def test_uname_failure_is_safe(self):
        (self.fixture / "uname-version").unlink()
        self.check_state(False)

    def test_legacy_exact_digits_without_newline(self):
        for value in ("0", "1"):
            with self.subTest(value=value):
                self.setUp()
                self.put("/sys/kernel/realtime", value)
                self.check_state(value == "1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
