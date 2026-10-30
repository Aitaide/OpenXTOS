"""Adversarial execution of the production BCI core, ring and Cyton driver."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CASES = ("legacy_mmap", "retained_fd", "open_unregister", "blocked_unbind", "blocked_stop",
         "two_devices_ids", "concurrent_overwrite", "efault", "reopen_empty", "stats_writer",
         "stale_inode_reuse", "cancel_open_reply")
HEADERS = ("module.h", "kernel.h", "init.h", "fs.h", "cdev.h", "device.h",
           "slab.h", "uaccess.h", "poll.h", "dma-mapping.h", "types.h",
           "list.h", "spinlock.h", "wait.h", "mutex.h", "kref.h", "mm.h",
           "refcount.h", "vmalloc.h", "err.h", "string.h", "compat.h", "limits.h", "ktime.h", "sched.h")


class BCILifetimeTest(unittest.TestCase):
    SOURCE = ROOT / "tests/bci_lifetime_host/lifetime_test.c"
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="bci-lifetime-", dir=os.environ.get("TMPDIR"))
        root = Path(cls.directory.name)
        linux = root / "linux"
        linux.mkdir()
        for name in HEADERS:
            (linux / name).write_text('#include "kernel_shim.h"\n')
        (linux / "xtos_bci.h").write_text('#include "' + str(ROOT / "kernel/linux/include/linux/xtos_bci.h") + '"\n')
        (linux / "xtos_bci_snapshot.h").write_text('#include "' + str(ROOT / "kernel/linux/include/uapi/linux/xtos_bci_snapshot.h") + '"\n')
        (linux / "xtos_bci_config.h").write_text('#include "' + str(ROOT / "kernel/linux/include/uapi/linux/xtos_bci_config.h") + '"\n')
        (linux / "xtos_bci_frame.h").write_text('#include "' + str(ROOT / "kernel/linux/include/uapi/linux/xtos_bci_frame.h") + '"\n')
        cls.binary = root / "lifetime_test"
        command = [os.environ.get("CC", "cc"), "-std=gnu11", "-Wall", "-Wextra", "-Werror",
                   "-Wno-unused-function", "-Wno-unused-variable", "-Wno-unused-parameter",
                   "-pthread", "-I" + str(root), "-I" + str(ROOT / "tests/bci_lifetime_host"),
                   "-I" + str(ROOT / "kernel/linux/include"),
                   str(cls.SOURCE), "-o", str(cls.binary)]
        sanitizer = os.environ.get("BCI_SANITIZE", "")
        if sanitizer:
            command[1:1] = ["-fsanitize=" + sanitizer, "-fno-omit-frame-pointer", "-g", "-no-pie"]
        output = os.environ.get("BCI_BINARY_OUT")
        if output:
            cls.binary = Path(output)
            command[-1] = str(cls.binary)
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()


def _case(name):
    def test(self):
        result = subprocess.run([str(self.binary), name], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), name)
    return test


for _name in CASES:
    setattr(BCILifetimeTest, "test_" + _name, _case(_name))

if __name__ == "__main__":
    unittest.main()
