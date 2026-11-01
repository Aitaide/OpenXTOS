"""Run the read-only status command through a real POSIX shell."""
import gzip
import os
import shlex
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "system/rootfs/usr/bin/xtos-rt-status"
RT_VERSION = "#1 SMP PREEMPT_RT Sun Nov 1 2026"


class KernelRTStatusTests(unittest.TestCase):
    def run_status(self, node=None, config=None, version=RT_VERSION, extra_env=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            if node is not None:
                path = root / "sys/kernel/realtime"
                path.parent.mkdir(parents=True)
                path.write_bytes(node)
            if config is not None:
                path = root / "proc/config.gz"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(config if isinstance(config, bytes) else gzip.compress(config.encode()))
            before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            result = subprocess.run(
                ["/bin/sh", str(HELPER), "--fixture-root", str(root),
                 "--uname-version", version], text=True, capture_output=True,
                env={**os.environ, **(extra_env or {})}, timeout=5)
            after = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            self.assertEqual(before, after, "status must not write fixture files")
            return result

    def check_status(self, expected, status, **kwargs):
        result = self.run_status(**kwargs)
        self.assertEqual(result.stdout, expected + "\n", result.stderr)
        self.assertEqual(result.returncode, status, result.stderr)
        self.assertIn("fixture", result.stderr)

    def test_present_node_is_authoritative(self):
        for node, expected, status in [(b"1\n", "1", 0), (b"0\n", "0", 1)]:
            with self.subTest(node=node):
                self.check_status(expected, status, node=node, config="CONFIG_PREEMPT_RT=y\n")

    def test_mainline_without_node_requires_config_and_version(self):
        self.check_status("1", 0, config="CONFIG_PREEMPT_RT=y\n")

    def test_mainline_missing_or_negative_evidence_is_not_success(self):
        for config, version in [
            (None, RT_VERSION),
            ("CONFIG_PREEMPT_RT=y\n", "#1 SMP PREEMPT_DYNAMIC"),
            ("# CONFIG_PREEMPT_RT is not set\n", RT_VERSION),
            ("CONFIG_PREEMPT_RT=n\n", RT_VERSION),
            ("CONFIG_PREEMPT_RT=yes\n", RT_VERSION),
            ("CONFIG_PREEMPT_RT=y extra\n", RT_VERSION),
            ("CONFIG_PREEMPT_RT=y\n", "#1 SMP NOT_PREEMPT_RT"),
            ("CONFIG_PREEMPT_RT=y\n", "#1 SMP PREEMPT_RT_SUFFIX"),
            (None, "#1 SMP PREEMPT"),
        ]:
            with self.subTest(config=config, version=version):
                self.check_status("unknown", 2, config=config, version=version)

    @unittest.skipUnless(os.geteuid() == 0 and (REPO / ".build/rootfs/bin/busybox").is_file(), "requires native built rootfs BusyBox and chroot")
    def test_built_busybox_executes_real_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bin").mkdir()
            shutil.copy2(REPO / ".build/rootfs/bin/busybox", root / "bin/busybox")
            applets = subprocess.check_output([str(root / "bin/busybox"), "--list"], text=True).splitlines()
            for applet in applets:
                (root / "bin" / applet).symlink_to("busybox")
            (root / "usr/bin").mkdir(parents=True)
            shutil.copy2(HELPER, root / "usr/bin/xtos-rt-status")
            (root / "fixture/proc").mkdir(parents=True)
            (root / "fixture/sys/kernel").mkdir(parents=True)
            (root / "dev").mkdir()
            (root / "dev/null").write_bytes(b"")
            (root / "fixture/proc/config.gz").write_bytes(gzip.compress(b"CONFIG_PREEMPT_RT=y\n"))
            node = root / "fixture/sys/kernel/realtime"
            for value, output, status in [(b"1\n", "1\n", 0), (b"0\n", "0\n", 1),
                                           (b"1\n\n", "0\n", 1), (b"1\x00\n", "0\n", 1),
                                           (None, "1\n", 0)]:
                if value is None:
                    node.unlink()
                else:
                    node.write_bytes(value)
                result = subprocess.run(["chroot", str(root), "/bin/sh", "/usr/bin/xtos-rt-status",
                    "--fixture-root", "/fixture", "--uname-version", RT_VERSION], text=True, capture_output=True, timeout=5)
                self.assertEqual((result.stdout, result.returncode), (output, status), result.stderr)

    def test_corrupt_config_does_not_use_partial_decompression(self):
        config = gzip.compress(b"CONFIG_PREEMPT_RT=y\n")[:-5]
        self.check_status("unknown", 2, config=config)

    def test_default_live_ignores_fixture_environment(self):
        baseline = subprocess.run(["/bin/sh", str(HELPER)], capture_output=True, text=True, timeout=5)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "sys/kernel/realtime"
            path.parent.mkdir(parents=True)
            path.write_text("1\n" if baseline.stdout != "1\n" else "0\n")
            result = subprocess.run(["/bin/sh", str(HELPER)], capture_output=True, text=True,
                env={**os.environ, "XTOS_RT_ROOT": directory, "XTOS_RT_UNAME_VERSION": RT_VERSION,
                     "XTOS_FIXTURE_ROOT": directory}, timeout=5)
        self.assertEqual((result.stdout, result.returncode), (baseline.stdout, baseline.returncode))
        self.assertNotIn("fixture", result.stderr)

    def test_fixture_parameters_must_be_explicit_and_paired(self):
        for arguments in [["--uname-version", RT_VERSION], ["--fixture-root", "/"], ["--bad"]]:
            with self.subTest(arguments=arguments):
                result = subprocess.run(["/bin/sh", str(HELPER), *arguments], capture_output=True, text=True, timeout=5)
                self.assertEqual((result.stdout, result.returncode), ("unknown\n", 2))

    def test_startup_and_selftest_use_common_status(self):
        paths = [
            "system/rootfs/usr/bin/xtos-selftest", "system/rootfs/init",
            "system/rootfs/xtos-guest-check", "system/live/xtos-start",
            "system/baremetal/xtos-start",
        ]
        for name in paths:
            with self.subTest(path=name):
                source = (REPO / name).read_text()
                self.assertIn("/usr/bin/xtos-rt-status", source)
                self.assertNotIn("/sys/kernel/realtime", source)
                lines = source.splitlines()
                line = next(i for i, text in enumerate(lines) if "/usr/bin/xtos-rt-status" in text)
                # Execute only the actual maintained reporting/check slice: no mounts,
                # module loads, networking or shutdown in a host test.
                end = line + (1 if name.endswith("xtos-selftest") else 2)
                snippet = "\n".join(lines[line:end]) + "\n"
                for node, config, expected in [
                    (b"1\n", None, "1"), (b"0\n", None, "0"),
                    (None, "CONFIG_PREEMPT_RT=y\n", "1"), (None, None, "unknown"),
                ]:
                    with tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        if node is not None:
                            path = root / "sys/kernel/realtime"
                            path.parent.mkdir(parents=True)
                            path.write_bytes(node)
                        if config is not None:
                            path = root / "proc/config.gz"
                            path.parent.mkdir(parents=True)
                            path.write_bytes(gzip.compress(config.encode()))
                        invocation = "/bin/sh " + shlex.quote(str(HELPER)) + " --fixture-root " + shlex.quote(str(root)) + " --uname-version " + shlex.quote(RT_VERSION)
                        command = snippet.replace("/usr/bin/xtos-rt-status", invocation)
                        result = subprocess.run(["/bin/sh", "-ec", command], text=True, capture_output=True, timeout=5)
                        if name.endswith("xtos-selftest"):
                            self.assertEqual(result.returncode, {"1": 0, "0": 1, "unknown": 2}[expected], result.stderr)
                        else:
                            self.assertEqual(result.returncode, 0, result.stderr)
                            self.assertIn("realtime=" + expected + "\n", result.stdout)
                missing = snippet.replace("/usr/bin/xtos-rt-status", "/nonexistent/xtos-rt-status")
                result = subprocess.run(["/bin/sh", "-ec", missing], text=True, capture_output=True, timeout=5)
                if name.endswith("xtos-selftest"):
                    self.assertNotEqual(result.returncode, 0)
                else:
                    self.assertIn("realtime=unknown\n", result.stdout)
                    self.assertNotIn("realtime=1\n", result.stdout)
                if name.endswith("xtos-guest-check"):
                    self.assertIn("preempt_rt=y", source)
                    self.assertIn("preempt_rt=n", source)

    def test_malformed_present_node_does_not_fallback(self):
        for node in [b"", b"yes\n", b"01\n", b" 1\n", b"1\n1\n", b"1\n\n", b"1\x00\n"]:
            with self.subTest(node=node):
                self.check_status("0", 1, node=node, config="CONFIG_PREEMPT_RT=y\n")


if __name__ == "__main__":
    unittest.main()
