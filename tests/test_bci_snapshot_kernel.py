"""Execute the actual isolated kernel helper using host kernel-service shims.

This validates C behavior and reference accounting, not Linux VM execution.
"""
import os
from pathlib import Path
import shlex
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".build/bci-software-completion/snapshot"
CASES = ("create", "bounds", "consume", "mapping", "map_args", "concurrent_refs")


class BCISnapshotKernelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = ROOT / "kernel/linux/drivers/xtos/bci/xtos_bci_snapshot.c"
        if not source.is_file():
            raise AssertionError("immutable snapshot helper has not been implemented")
        OUT.mkdir(parents=True, exist_ok=True)
        cls.binary = OUT / os.environ.get("SNAPSHOT_BINARY", "snapshot-host")
        command = [os.environ.get("CC", "cc"), "-std=gnu11", "-g", "-O1",
                   "-Wall", "-Wextra", "-Werror", "-pthread",
                   "-DXTOS_BCI_SNAPSHOT_HOST_TEST",
                   "-I" + str(ROOT / "tests/bci_snapshot_host")]
        command += shlex.split(os.environ.get("SNAPSHOT_CFLAGS", ""))
        command += [str(ROOT / "tests/bci_snapshot_host/snapshot_test.c"),
                    "-o", str(cls.binary)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)


def _case(name):
    def test(self):
        result = subprocess.run([str(self.binary), name], capture_output=True,
                                text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), name)
    return test


for _name in CASES:
    setattr(BCISnapshotKernelTest, "test_" + _name, _case(_name))

if __name__ == "__main__":
    unittest.main()
