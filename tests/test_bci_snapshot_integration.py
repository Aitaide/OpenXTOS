"""Actual core + immutable helper + Cyton integration (host kernel services)."""
from pathlib import Path
import subprocess
import unittest
import test_bci_lifetime as lifetime

CASES = ("acquire", "acquire_validation", "copy_failure", "mapping_args", "consume",
         "overwrite", "release_replay", "read_busy", "blocked_acquire",
         "vma_close_unbind", "vma_release_clone", "copy_from_failure",
         "allocation_failure", "per_fd", "read_after_op")


class BCISnapshotIntegrationTest(unittest.TestCase):
    SOURCE = Path(__file__).resolve().parent / "bci_snapshot_integration_host/integration_test.c"
    setUpClass = classmethod(lifetime.BCILifetimeTest.setUpClass.__func__)
    tearDownClass = classmethod(lifetime.BCILifetimeTest.tearDownClass.__func__)


def _case(name):
    def test(self):
        result = subprocess.run([str(self.binary), name], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), name)
    return test


for _name in CASES:
    setattr(BCISnapshotIntegrationTest, "test_" + _name, _case(_name))

if __name__ == "__main__":
    unittest.main()
