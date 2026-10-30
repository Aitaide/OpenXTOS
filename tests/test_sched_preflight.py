"""Deterministic fixture tests for the read-only scheduler preflight collector."""
import gzip
import io
import json
import os
from unittest import mock
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from contextlib import redirect_stderr


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "tools/xtos-sched-bench/preflight.py"
PREFLIGHT = SimpleNamespace(**runpy.run_path(str(MODULE)))

EXPECTED_TOP_LEVEL_KEYS = [
    "schema",
    "collection_mode",
    "uname",
    "virtualization",
    "realtime",
    "cpu",
    "cmdline",
    "cgroup",
    "governor",
    "irq_affinity",
    "scheduler_config",
    "cap_sys_nice",
]


class SchedulerPreflightFixtureTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(
            os.environ.get("TMPDIR", "/root/.hermes/cache/scratch")
        )
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(
            prefix="sched-preflight-",
            dir=str(scratch),
        )
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def write(self, relative, contents, mode="text"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if mode == "bytes":
            path.write_bytes(contents)
        else:
            path.write_text(contents, encoding="utf-8")
        return path

    def collect(self, allowed=(0, 1, 2)):
        return PREFLIGHT.Collector(self.root, allowed_cpus=allowed).collect()

    def test_report_has_stable_schema_and_key_structure(self):
        report = self.collect()
        self.assertEqual(report["schema"], "xtos-sched-preflight-v1")
        self.assertEqual(list(report), EXPECTED_TOP_LEVEL_KEYS)
        self.assertEqual(
            list(report["cpu"]), ["online", "allowed", "intersection"]
        )
        self.assertEqual(
            list(report["scheduler_config"]),
            ["status", "proc_config_gz", "boot_config", "source", "error"],
        )
        self.assertEqual(
            list(report["virtualization"]), ["status", "value", "source", "error"]
        )

    def test_missing_facts_are_unknown_not_false(self):
        report = self.collect()
        self.assertEqual(report["realtime"]["status"], "unknown")
        self.assertEqual(report["cpu"]["online"]["status"], "unknown")
        self.assertEqual(report["cmdline"]["status"], "unknown")
        self.assertEqual(report["cgroup"]["v2"]["status"], "unknown")
        self.assertEqual(report["scheduler_config"]["proc_config_gz"]["status"], "unknown")
        self.assertEqual(report["scheduler_config"]["boot_config"]["status"], "unknown")
        self.assertEqual(report["virtualization"]["value"], "unknown")
        self.assertEqual(report["virtualization"]["status"], "unknown")
        self.assertNotEqual(report["realtime"]["status"], "available")

    def test_machine_model_does_not_prove_physical_hardware(self):
        self.write("sys/kernel/realtime", "1\n")
        self.write(
            "sys/devices/virtual/dmi/id/product_name", "PowerEdge fixture\n"
        )
        report = self.collect()
        self.assertEqual(report["realtime"]["status"], "available")
        self.assertEqual(report["realtime"]["value"], "1")
        self.assertEqual(report["virtualization"]["status"], "unknown")
        self.assertEqual(report["virtualization"]["value"], "unknown")

    def test_virtual_vendor_is_detected_even_with_physical_model_name(self):
        self.write("sys/devices/virtual/dmi/id/product_name", "PowerEdge fixture\n")
        self.write("sys/devices/virtual/dmi/id/sys_vendor", "QEMU\n")
        report = self.collect()
        self.assertEqual(report["virtualization"]["value"], "true")
        self.assertEqual(report["virtualization"]["source"],
                         "/sys/devices/virtual/dmi/id/sys_vendor")

    def test_fixture_mode_does_not_mix_live_uname_or_affinity(self):
        report = PREFLIGHT.Collector(self.root).collect()
        self.assertEqual(report["collection_mode"], "fixture_filesystem")
        self.assertEqual(report["uname"], {"release": "unknown", "machine": "unknown"})
        self.assertEqual(report["cpu"]["allowed"]["status"], "unknown")
        self.assertEqual(report["cpu"]["allowed"]["error"], "fixture_affinity_not_provided")

    def test_fifo_fact_is_rejected_without_blocking_cli(self):
        path = self.root / "sys/kernel/realtime"
        path.parent.mkdir(parents=True)
        os.mkfifo(path)
        try:
            result = subprocess.run([sys.executable, str(MODULE), "--root", str(self.root)],
                                    capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("FIFO fact blocked preflight instead of being rejected")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["realtime"]["error"], "not_regular")

    def test_read_rejects_path_replaced_by_symlink_after_resolution(self):
        target = self.write("sys/kernel/realtime", "1\n")
        collector = PREFLIGHT.Collector(self.root)
        original = collector._safe_path
        def replace_after_resolve(path):
            resolved = original(path)
            target.unlink()
            target.symlink_to("/etc/passwd")
            return resolved
        with mock.patch.object(collector, "_safe_path", side_effect=replace_after_resolve):
            data, error = collector._read_bytes("/sys/kernel/realtime")
        self.assertIsNone(data)
        self.assertIsNotNone(error)

    def test_root_ancestor_replacement_cannot_redirect_fact_reads(self):
        for use_symlink in (True, False):
            with self.subTest(use_symlink=use_symlink):
                base = self.root / str(use_symlink)
                anchor = base / "anchor"
                root = anchor / "root"
                root.mkdir(parents=True)
                (root / "fact").write_bytes(b"inside")
                alternate = base / "alternate"
                (alternate / "root").mkdir(parents=True)
                (alternate / "root/fact").write_bytes(b"outside")
                collector = PREFLIGHT.Collector(root)
                original = collector._safe_path
                def replace_ancestor(path):
                    resolved = original(path)
                    anchor.rename(base / "saved-anchor")
                    if use_symlink:
                        anchor.symlink_to(alternate, target_is_directory=True)
                    else:
                        alternate.rename(anchor)
                    return resolved
                before = len(list(Path('/proc/self/fd').iterdir()))
                with mock.patch.object(collector, "_safe_path", side_effect=replace_ancestor):
                    data, error = collector._read_bytes("/fact")
                self.assertNotEqual(data, b"outside", "replacement root redirected input")
                self.assertIsNotNone(error)
                self.assertEqual(len(list(Path('/proc/self/fd').iterdir())), before)

    def test_replaced_root_identity_is_not_accepted_on_later_reads(self):
        root = self.root / "selected-root"
        root.mkdir()
        (root / "fact").write_bytes(b"inside")
        collector = PREFLIGHT.Collector(root)
        root.rename(self.root / "saved-root")
        root.mkdir()
        (root / "fact").write_bytes(b"outside")
        data, error = collector._read_bytes("/fact")
        self.assertIsNone(data)
        self.assertIsNotNone(error)

    def test_cgroup_bind_mount_root_maps_membership_to_visible_cpuset(self):
        self.write("proc/self/mountinfo",
                   "29 23 0:26 /slice /sys/fs/cgroup rw - cgroup2 cgroup rw\n")
        self.write("proc/self/cgroup", "0::/slice/demo\n")
        self.write("sys/fs/cgroup/cgroup.controllers", "cpuset cpu\n")
        self.write("sys/fs/cgroup/demo/cpuset.cpus.effective", "2-3\n")
        self.assertEqual(self.collect()["cgroup"]["effective_cpuset"]["value"], [2, 3])

    def test_fixture_symlink_outside_root_is_not_read(self):
        proc_cmdline = self.root / "proc/cmdline"
        proc_cmdline.parent.mkdir(parents=True, exist_ok=True)
        proc_cmdline.symlink_to("/etc/passwd")
        report = self.collect()
        self.assertEqual(report["cmdline"]["status"], "unknown")
        self.assertEqual(report["cmdline"]["error"], "unsafe_path")
        self.assertEqual(report["cmdline"]["isolated"]["error"], "unsafe_path")

    def test_malformed_cpu_range_is_an_explicit_error(self):
        self.write("sys/devices/system/cpu/online", "0-2,bad\n")
        report = self.collect(allowed=(0, 1))
        self.assertEqual(report["cpu"]["online"]["status"], "error")
        self.assertEqual(report["cpu"]["online"]["error"], "malformed_cpu_range")
        self.assertEqual(report["cpu"]["intersection"]["status"], "error")

    def test_online_and_allowed_affinity_intersection_is_sorted(self):
        self.write("sys/devices/system/cpu/online", "3,0-2\n")
        report = self.collect(allowed=(8, 3, 1))
        self.assertEqual(report["cpu"]["online"]["value"], [0, 1, 2, 3])
        self.assertEqual(report["cpu"]["allowed"]["value"], [1, 3, 8])
        self.assertEqual(report["cpu"]["intersection"]["value"], [1, 3])

    def test_cmdline_collects_only_recognized_cpu_options(self):
        self.write(
            "proc/cmdline",
            "quiet secret=password isolcpus=managed_irq,1-2 "
            "nohz_full=3 housekeeping=0-1 foo=private\n",
        )
        report = self.collect()
        self.assertEqual(report["cmdline"]["status"], "available")
        self.assertEqual(report["cmdline"]["isolated"]["value"], [1, 2])
        self.assertEqual(report["cmdline"]["nohz_full"]["value"], [3])
        self.assertEqual(report["cmdline"]["housekeeping"]["value"], [0, 1])
        self.assertNotIn("secret", json.dumps(report, sort_keys=True))
        self.assertNotIn("private", json.dumps(report, sort_keys=True))

    def test_cgroup_and_scheduler_config_absence_is_reported(self):
        report = self.collect()
        self.assertEqual(report["cgroup"]["v2"]["status"], "unknown")
        self.assertEqual(report["cgroup"]["effective_cpuset"]["status"], "unknown")
        self.assertEqual(report["scheduler_config"]["status"], "unknown")

    def test_cgroup_v2_effective_cpuset_and_config_availability(self):
        self.write("sys/fs/cgroup/cgroup.controllers", "cpuset cpu\n")
        self.write("proc/self/cgroup", "0::/slice/demo\n")
        self.write(
            "sys/fs/cgroup/slice/demo/cpuset.cpus.effective", "1-2,4\n"
        )
        self.write("proc/config.gz", gzip.compress(b"CONFIG_PREEMPT_RT=y\n"), mode="bytes")
        report = self.collect(allowed=(1, 2, 4))
        self.assertEqual(report["cgroup"]["v2"]["value"], True)
        self.assertEqual(report["cgroup"]["effective_cpuset"]["value"], [1, 2, 4])
        self.assertEqual(report["scheduler_config"]["proc_config_gz"]["status"], "available")
        self.assertEqual(report["scheduler_config"]["status"], "available")

    def test_cgroup_mount_path_is_not_disclosed(self):
        self.write(
            "proc/self/mountinfo",
            "29 23 0:26 / /sys/fs/cgroup/private-slice-name rw - cgroup2 cgroup rw\n",
        )
        self.write("proc/self/cgroup", "0::/nested/private-workload\n")
        self.write(
            "sys/fs/cgroup/private-slice-name/nested/private-workload/cpuset.cpus.effective",
            "1-2\n",
        )
        report = self.collect()
        encoded = json.dumps(report, sort_keys=True)
        self.assertEqual(report["cgroup"]["v2"]["value"], True)
        self.assertEqual(report["cgroup"]["effective_cpuset"]["value"], [1, 2])
        self.assertNotIn("private-slice-name", encoded)
        self.assertNotIn("private-workload", encoded)

    def test_malformed_scheduler_config_is_an_explicit_error(self):
        self.write("proc/config.gz", b"not gzip data", mode="bytes")
        report = self.collect()
        self.assertEqual(report["scheduler_config"]["proc_config_gz"]["status"], "error")
        self.assertEqual(report["scheduler_config"]["status"], "error")

    def test_governor_is_reported_per_allowed_cpu_without_false_fallbacks(self):
        self.write(
            "sys/devices/system/cpu/cpu1/cpufreq/scaling_governor",
            "performance\n",
        )
        self.write(
            "sys/devices/system/cpu/cpu3/cpufreq/scaling_governor",
            "schedutil\n",
        )
        report = self.collect(allowed=(3, 1, 2))
        governors = report["governor"]["per_cpu"]
        self.assertEqual(governors["1"]["value"], "performance")
        self.assertEqual(governors["3"]["value"], "schedutil")
        self.assertEqual(governors["2"]["status"], "unknown")
        self.assertEqual(report["governor"]["status"], "partial")

    def test_irq_affinity_is_aggregated_not_emitted_per_irq(self):
        self.write("proc/irq/1/smp_affinity_list", "0-1\n")
        self.write("proc/irq/2/smp_affinity_list", "1\n")
        self.write("proc/irq/3/smp_affinity_list", "broken\n")
        report = self.collect(allowed=(1,))
        summary = report["irq_affinity"]
        self.assertEqual(summary["status"], "partial")
        self.assertEqual(summary["irqs_seen"], 3)
        self.assertEqual(summary["affinity_read"], 2)
        self.assertEqual(summary["malformed"], 1)
        self.assertEqual(summary["unreadable"], 0)
        self.assertEqual(summary["single_cpu"], 1)
        self.assertEqual(summary["multi_cpu"], 1)
        self.assertEqual(summary["cpu_usage"], {"0": 1, "1": 2})
        self.assertEqual(summary["outside_allowed"], 1)
        self.assertNotIn("1", summary)
        self.assertNotIn("2", summary)

    def test_irq_directory_scan_reports_truncation(self):
        for irq in range(4):
            self.write(f"proc/irq/{irq}/smp_affinity_list", "0\n")
        with mock.patch.dict(PREFLIGHT.Collector._collect_irq_affinity.__globals__,
                             MAX_IRQ_ENTRIES=2):
            summary = self.collect()["irq_affinity"]
        self.assertEqual(summary["irqs_seen"], 2)
        self.assertEqual(summary["status"], "partial")
        self.assertEqual(summary["error"], "truncated")

    def test_irq_directory_replacement_cannot_enumerate_outside_root(self):
        directory = self.root / "proc/irq"
        directory.mkdir(parents=True)
        outside = self.root / "outside-fixture"
        outside.mkdir()
        (outside / "71").mkdir()
        (outside / "93").mkdir()
        # A nested injected root makes outside-fixture a true sibling.
        collector = PREFLIGHT.Collector(self.root / "proc", allowed_cpus=(0,))
        nested_irq = self.root / "proc/proc/irq"
        nested_irq.mkdir(parents=True)
        original = collector._directory
        def replace_after_validation(path):
            result = original(path)
            nested_irq.rename(nested_irq.with_name("saved-irq"))
            nested_irq.symlink_to(outside, target_is_directory=True)
            return result
        with mock.patch.object(collector, "_directory", side_effect=replace_after_validation):
            summary = collector._collect_irq_affinity(
                {"status": "available", "value": [0]})
        self.assertEqual(summary["irqs_seen"], 0, "external directory names entered report")
        self.assertNotEqual(summary["status"], "available")

    def test_cap_sys_nice_is_parsed_when_proc_status_is_available(self):
        # CAP_SYS_NICE is bit 23.  The surrounding value is deliberately
        # otherwise uninteresting fixture data.
        self.write("proc/self/status", "Name:\tfixture\nCapEff:\t0000000000800000\n")
        report = self.collect()
        self.assertEqual(report["cap_sys_nice"]["status"], "available")
        self.assertTrue(report["cap_sys_nice"]["value"])

    def test_output_is_exclusive_and_pretty_json_is_valid(self):
        existing = self.root / "report.json"
        existing.write_text("sentinel\n", encoding="utf-8")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = PREFLIGHT.main(["--root", str(self.root), "--output", str(existing)])
        self.assertEqual(result, 2)
        self.assertEqual(existing.read_text(encoding="utf-8"), "sentinel\n")
        self.assertIn("exists", stderr.getvalue())

        output = self.root / "new-report.json"
        self.assertEqual(
            PREFLIGHT.main(
                ["--root", str(self.root), "--output", str(output), "--pretty"]
            ),
            0,
        )
        text = output.read_text(encoding="utf-8")
        self.assertIn("\n  \"schema\"", text)
        self.assertEqual(json.loads(text)["schema"], "xtos-sched-preflight-v1")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(
                PREFLIGHT.main(["--root", str(self.root), "--output", str(output)]),
                2,
            )


class SchedulerPreflightHostSchemaTests(unittest.TestCase):
    def test_real_host_collection_is_schema_only(self):
        report = PREFLIGHT.Collector(Path("/")).collect()
        self.assertEqual(report["schema"], "xtos-sched-preflight-v1")
        self.assertEqual(list(report), EXPECTED_TOP_LEVEL_KEYS)
        self.assertIn(report["virtualization"]["value"], {"true", "false", "unknown"})
        self.assertIn(
            report["cpu"]["allowed"]["status"], {"available", "unknown", "error"}
        )
        json.dumps(report, sort_keys=True)


if __name__ == "__main__":
    unittest.main()
