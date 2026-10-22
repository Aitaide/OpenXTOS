#!/usr/bin/env python3
"""Source prerequisites for the fail-closed native runner's target kernel.

These tests check maintained configuration requests, not a running guest. Real
resolved configuration and syscall behavior remain separate integration gates.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = (
    "CONFIG_NAMESPACES", "CONFIG_UTS_NS", "CONFIG_IPC_NS", "CONFIG_PID_NS",
    "CONFIG_NET_NS", "CONFIG_USER_NS", "CONFIG_SECCOMP", "CONFIG_SECCOMP_FILTER",
    "CONFIG_SECURITY", "CONFIG_SECURITYFS", "CONFIG_SECURITY_LANDLOCK",
)


def fragment(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines()
                if line.startswith("CONFIG_") and "=" in line)


class RunnerKernelConfigTests(unittest.TestCase):
    def test_shared_arch_fragment_requests_runner_security_prerequisites(self):
        requested = fragment(ROOT / "kernel/xtos_defconfig")
        for key in REQUIRED:
            with self.subTest(option=key):
                self.assertEqual(requested.get(key), "y")

    def test_live_fragment_does_not_disable_shared_security_requirements(self):
        combined = fragment(ROOT / "kernel/xtos_defconfig")
        combined.update(fragment(ROOT / "kernel/xtos_live_x86.config"))
        for key in REQUIRED:
            with self.subTest(option=key):
                self.assertEqual(combined.get(key), "y")
        self.assertIn("landlock", combined.get("CONFIG_LSM", "").strip('"').split(","))

    def test_test_runner_executes_privileged_fixture_or_reports_explicit_skip(self):
        runner = (ROOT / "scripts/test.sh").read_text()
        self.assertIn('python3 "$ROOT/tests/test_native_tools_runner.py"', runner)
        self.assertIn('runner_fixture_tests=skipped', runner)
        self.assertIn('python3 "$ROOT/tests/test_runner_kernel_config.py"', runner)

    def test_vendored_target_has_landlock_truncate_right(self):
        header = (ROOT / "kernel/linux/include/uapi/linux/landlock.h").read_text()
        self.assertIn("#define LANDLOCK_ACCESS_FS_TRUNCATE", header)
        kconfig = (ROOT / "kernel/linux/security/landlock/Kconfig").read_text()
        self.assertIn("config SECURITY_LANDLOCK", kconfig)


if __name__ == "__main__":
    unittest.main(verbosity=2)
