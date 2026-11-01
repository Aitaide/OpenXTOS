"""Kernel migration gates must execute through maintained entrypoints."""
from pathlib import Path
import re
import unittest

ROOT=Path(__file__).resolve().parents[1]
SUITES=('test_kernel_migration.py','test_kernel_source_pin.py',
        'test_kernel_scheduler_baseline.py','test_kernel_cross_config.py',
        'test_kernel_rt_status.py','test_kernel_release_packaging.py',
        'test_kernel_sched_rt.py','test_native_tools_rt_kernel.py',
        'test_kernel_maintenance_wiring.py')

class KernelMaintenanceWiringTests(unittest.TestCase):
    def test_kernel_migration_suites_execute_once_in_shared_loop(self):
        text=(ROOT/'scripts/test.sh').read_text()
        match=re.search(r'for suite in test_kernel_migration\.py\b.*?\ndone',text,re.S)
        self.assertIsNotNone(match)
        block=match.group()
        self.assertEqual(re.findall(r'test_[a-z_]+\.py',block),list(SUITES))
        self.assertIn('python3 -B -m unittest discover',block)
        self.assertIn('"$suite"',block)

    def test_cross_ci_runs_runtime_guest_validation(self):
        text=(ROOT/'.github/workflows/ci.yml').read_text()
        self.assertIn('./scripts/verify_guest_arm64.sh',text)
        self.assertIn('./scripts/verify_guest_riscv64.sh',text)
        self.assertIn('QEMU_TIMEOUT=300 ./scripts/verify_guest_arm64.sh',text)
        self.assertIn('QEMU_TIMEOUT=360 ./scripts/verify_guest_riscv64.sh',text)

if __name__=='__main__':unittest.main()
