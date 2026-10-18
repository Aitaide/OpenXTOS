#!/usr/bin/env python3
"""Fixture-only tests for rt tune; never open the host's proc/sys paths for writing."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "tools/xtos-native/rt/rt.c"
COMMON = ROOT / "tools/xtos-native/common"

# Redirection exists only in this test translation unit. The production binary
# has no environment-controlled writable root. Failed/short/dropped writes model
# procfs behavior which ordinary temporary files cannot otherwise reproduce.
HARNESS = r'''
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <sys/types.h>
#include <sys/stat.h>

static int fault_fd = -1;
static int fault_mode;

static int fixture_path(const char *path, char *mapped, size_t size) {
    int n = snprintf(mapped, size, "%s%s", FIXTURE_ROOT, path);
    if (path[0] != '/' || n < 0 || (size_t)n >= size) {
        errno = ENAMETOOLONG;
        return -1;
    }
    return 0;
}

static FILE *fixture_fopen(const char *path, const char *mode) {
    char mapped[PATH_MAX], marker[PATH_MAX + 32];
    if (fixture_path(path, mapped, sizeof(mapped)) < 0) return NULL;
    snprintf(marker, sizeof(marker), "%s.unreadable", mapped);
    if (access(marker, F_OK) == 0) { errno = EACCES; return NULL; }
    return fopen(mapped, mode);
}

static DIR *fixture_opendir(const char *path) {
    char mapped[PATH_MAX];
    if (fixture_path(path, mapped, sizeof(mapped)) < 0) return NULL;
    return opendir(mapped);
}

static int fixture_open(const char *path, int flags, ...) {
    char mapped[PATH_MAX], marker[PATH_MAX + 32];
    if (fixture_path(path, mapped, sizeof(mapped)) < 0) return -1;
    fault_mode = 0;
    if (flags & (O_WRONLY | O_RDWR)) {
        char audit[PATH_MAX];
        if (fixture_path("/writes.log", audit, sizeof(audit)) < 0) return -1;
        FILE *log = fopen(audit, "a");
        if (!log) return -1;
        fprintf(log, "%s\n", path);
        fclose(log);
        snprintf(marker, sizeof(marker), "%s.deny", mapped);
        if (access(marker, F_OK) == 0) { errno = EIO; return -1; }
        snprintf(marker, sizeof(marker), "%s.short", mapped);
        if (access(marker, F_OK) == 0) fault_mode = 1;
        snprintf(marker, sizeof(marker), "%s.drop", mapped);
        if (access(marker, F_OK) == 0) fault_mode = 2;
        /* procfs replaces the value; regular files need truncation to match. */
        if (!fault_mode) flags |= O_TRUNC;
    }
    fault_fd = open(mapped, flags);
    return fault_fd;
}

static ssize_t fixture_write(int fd, const void *buf, size_t count) {
    if (fd == fault_fd && fault_mode == 1) return count ? (ssize_t)count - 1 : 0;
    if (fd == fault_fd && fault_mode == 2) return (ssize_t)count;
    return write(fd, buf, count);
}

/* Root and PREEMPT_RT are fixture inputs, never claims about the test host. */
static uid_t fixture_geteuid(void) __attribute__((unused));
static uid_t fixture_geteuid(void) {
    char mapped[PATH_MAX];
    if (fixture_path("/unprivileged", mapped, sizeof(mapped)) < 0) return 1000;
    return access(mapped, F_OK) == 0 ? 1000 : 0;
}

#define fopen fixture_fopen
#define opendir fixture_opendir
#define open fixture_open
#define write fixture_write
#define geteuid fixture_geteuid
#define main rt_tool_main
#include RT_SOURCE
#undef main
#undef geteuid
#undef write
#undef open
#undef opendir
#undef fopen

int main(int argc, char **argv) {
    if (argc == 2 && strcmp(argv[1], "--exercise-short-write") == 0) {
        return write_str("/short-target", "-1") == 0 ? 0 : 1;
    }
    return rt_tool_main(argc, argv);
}
'''


@unittest.skipUnless(sys.platform == "linux", "linux only")
class RtAffinityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="xtos-rt-affinity-")
        cls.work = Path(cls.temp.name)
        cls.fixture = cls.work / "root"
        cls.binary = cls.work / "rt-fixture"
        harness = cls.work / "fixture.c"
        harness.write_text(HARNESS, encoding="utf-8")
        try:
            subprocess.run([
                "gcc", "-O2", "-Wall", "-Wextra", "-Werror", "-static",
                "-I", str(COMMON),
                f"-DFIXTURE_ROOT={json.dumps(str(cls.fixture))}",
                f"-DRT_SOURCE={json.dumps(str(SRC))}",
                "-o", str(cls.binary), str(harness),
            ], check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            cls.temp.cleanup()
            raise AssertionError(f"fixture compilation failed:\n{exc.stderr}") from exc

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        if self.fixture.exists():
            shutil.rmtree(self.fixture)
        self.fixture.mkdir()
        self.put("/sys/kernel/realtime", "1\n")
        self.put("/sys/devices/system/cpu/online", "0-3\n")
        self.put("/sys/devices/system/cpu/isolated", "\n")
        self.put("/proc/sys/kernel/sched_rt_runtime_us", "-1\n")
        self.put("/proc/sys/kernel/sched_rt_period_us", "1000000\n")
        (self.fixture / "proc/irq").mkdir(parents=True)

    def put(self, path, content):
        target = self.fixture / path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def irq(self, number, requested="0", effective="0"):
        requested_path = self.put(f"/proc/irq/{number}/smp_affinity_list", requested + "\n")
        self.put(f"/proc/irq/{number}/effective_affinity_list", effective + "\n")
        self.put(f"/sys/kernel/irq/{number}/actions", "fixture-device\n")
        return requested_path

    def run_rt(self, *args):
        return subprocess.run([str(self.binary), *args], capture_output=True,
                              text=True, timeout=10)

    def writes(self):
        audit = self.fixture / "writes.log"
        return audit.read_text(encoding="utf-8").splitlines() if audit.exists() else []

    def test_invalid_housekeeping_rejects_before_any_write(self):
        for cpulist in ("", "1,,2", "2-1", "0-3,2", "0,4", "1-", "+1", "1, 2"):
            with self.subTest(cpulist=cpulist):
                self.setUp()
                self.irq(40, requested="3", effective="3")
                result = self.run_rt("tune", "--housekeeping", cpulist)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.writes(), [], f"wrote before rejecting {cpulist!r}")

    def test_offline_and_boot_isolated_cpus_reject_before_writes(self):
        self.put("/sys/devices/system/cpu/online", "0,2-3\n")
        self.put("/sys/devices/system/cpu/isolated", "3\n")
        for cpulist in ("1", "2-3"):
            with self.subTest(cpulist=cpulist):
                self.setUp()
                self.put("/sys/devices/system/cpu/online", "0,2-3\n")
                self.put("/sys/devices/system/cpu/isolated", "3\n")
                self.irq(40, requested="0", effective="0")
                result = self.run_rt("tune", "--housekeeping", cpulist)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.writes(), [])

    def test_valid_sparse_online_cpu_list_is_accepted(self):
        self.put("/sys/devices/system/cpu/online", "0,2-3\n")
        self.irq(40, requested="0", effective="2-3")
        result = self.run_rt("tune", "--housekeeping", "2-3")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('requested="2-3" effective="2-3"', result.stdout)

    def test_online_range_with_gap_is_rejected_before_any_write(self):
        self.put("/sys/devices/system/cpu/online", "0-1,3\n")
        self.irq(40, requested="0", effective="0")
        result = self.run_rt("tune", "--housekeeping", "0-3")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.writes(), [])

    def test_adjacent_online_ranges_cover_requested_range(self):
        self.put("/sys/devices/system/cpu/online", "0-1,2-3\n")
        self.irq(40, requested="0", effective="0-3")
        result = self.run_rt("tune", "--housekeeping", "0-3")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_success_reports_literal_requested_and_effective_readbacks(self):
        self.irq(40, requested="0", effective="1,2")
        result = self.run_rt("tune", "--housekeeping", "1-2")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('requested="1-2" effective="1,2"', result.stdout)

    def test_effective_subset_of_housekeeping_is_success(self):
        self.irq(40, requested="0", effective="2")
        result = self.run_rt("tune", "--housekeeping", "1-2")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('requested="1-2" effective="2"', result.stdout)

    def test_unavailable_effective_affinity_is_explicitly_skipped(self):
        self.irq(40, requested="0", effective="0")
        (self.fixture / "proc/irq/40/effective_affinity_list").unlink()
        result = self.run_rt("tune", "--housekeeping", "1")
        self.assertIn("irq 40 skipped", result.stdout)
        self.assertIn("skipped=1", result.stdout)
        self.assertEqual(self.writes(), [])

    def test_explicit_housekeeping_range_replaces_cpu0_default(self):
        self.irq(40, requested="0", effective="1-2")
        result = self.run_rt("tune", "--housekeeping", "1-2")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        requested = self.fixture / "proc/irq/40/smp_affinity_list"
        self.assertEqual(requested.read_text(encoding="utf-8").strip(), "1-2")
        self.assertIn('requested="1-2" effective="1-2"', result.stdout)

    def test_no_housekeeping_option_keeps_cpu0_compatibility(self):
        self.irq(40, requested="3", effective="0")
        result = self.run_rt("tune")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        requested = self.fixture / "proc/irq/40/smp_affinity_list"
        self.assertEqual(requested.read_text(encoding="utf-8").strip(), "0")
        self.assertIn('housekeeping_cpus=0', result.stdout)

    def test_requested_and_effective_readback_mismatch_fails(self):
        self.irq(40, requested="0", effective="0")
        result = self.run_rt("tune", "--housekeeping", "1-2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("failed", result.stdout.lower())
        self.assertIn('requested="1-2" effective="0"', result.stdout)

    def test_unmovable_irq_write_is_reported_and_fails(self):
        irq = self.irq(40, requested="0", effective="0")
        (irq.with_name("smp_affinity_list.deny")).touch()
        result = self.run_rt("tune", "--housekeeping", "1")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue("failed" in result.stdout.lower() or "skipped" in result.stdout.lower())
        self.assertIn("failed=1", result.stdout)

    def test_rt_budget_write_failure_is_nonzero(self):
        runtime = self.put("/proc/sys/kernel/sched_rt_runtime_us", "500000\n")
        runtime.with_name(runtime.name + ".deny").touch()
        result = self.run_rt("tune", "--housekeeping", "1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rt_runtime", result.stderr)
        self.assertEqual(len(self.writes()), 1)

    def test_rt_budget_successful_but_dropped_write_fails(self):
        runtime = self.put("/proc/sys/kernel/sched_rt_runtime_us", "500000\n")
        runtime.with_name(runtime.name + ".drop").touch()
        result = self.run_rt("tune", "--housekeeping", "1")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(runtime.read_text(encoding="utf-8"), "500000\n")
        self.assertNotIn("throttling disabled", result.stdout)
        self.assertNotIn("already -1", result.stdout)
        self.assertIn("changes=0", result.stdout)
        self.assertIn("failed=1", result.stdout)
        self.assertIn("rt_runtime", result.stderr)

    def test_rt_budget_read_errors_never_claim_disabled_or_write_budget(self):
        for content in (None, "unreadable", "", "invalid\n", "-1junk\n", "-2\n",
                        "9999999999999999999999999999999999\n", "-1\n0\n"):
            with self.subTest(content=content):
                self.setUp()
                runtime = self.fixture / "proc/sys/kernel/sched_rt_runtime_us"
                if content is None:
                    runtime.unlink()
                elif content == "unreadable":
                    runtime.with_name(runtime.name + ".unreadable").touch()
                else:
                    runtime.write_text(content, encoding="utf-8")
                result = self.run_rt("tune", "--housekeeping", "1")
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("throttling disabled", result.stdout)
                self.assertNotIn("already -1", result.stdout)
                self.assertIn("failed=1", result.stdout)
                self.assertIn("rt_runtime", result.stderr)
                self.assertEqual(self.writes(), [])

    def test_rt_budget_short_write_is_nonzero(self):
        runtime = self.put("/proc/sys/kernel/sched_rt_runtime_us", "500000\n")
        runtime.with_name(runtime.name + ".short").touch()
        result = self.run_rt("tune", "--housekeeping", "1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rt_runtime", result.stderr)

    def test_irq_short_write_is_nonzero_and_readback_is_reported(self):
        irq = self.irq(40, requested="0", effective="0")
        irq.with_name(irq.name + ".short").touch()
        result = self.run_rt("tune", "--housekeeping", "1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('requested="0" effective="0"', result.stdout)

    def test_unprivileged_user_is_rejected_before_any_write(self):
        (self.fixture / "unprivileged").touch()
        result = self.run_rt("tune", "--housekeeping", "1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.writes(), [])

    def test_non_rt_kernel_refuses_before_any_write(self):
        self.put("/sys/kernel/realtime", "0\n")
        self.irq(40, requested="0", effective="0")
        result = self.run_rt("tune", "--housekeeping", "1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing", result.stderr)
        self.assertEqual(self.writes(), [])

    def test_short_write_is_not_success(self):
        target = self.put("/short-target", "old")
        target.with_name(target.name + ".short").touch()
        result = self.run_rt("--exercise-short-write")
        self.assertNotEqual(result.returncode, 0)

    def test_unknown_tune_argument_is_rejected_before_writes(self):
        result = self.run_rt("tune", "--bogus")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.writes(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
