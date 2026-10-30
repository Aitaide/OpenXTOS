"""Exercise P2 frame ioctls in the actual combined core/ring/Cyton C source."""
import importlib.util
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("frame_core_host_services", ROOT / "tests/test_bci_lifetime.py")
BASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BASE)


class FrameCoreTests(unittest.TestCase):
    SOURCE = ROOT / "tests/bci_lifetime_host/frame_core_test.c"
    setUpClass = classmethod(BASE.BCILifetimeTest.setUpClass.__func__)
    tearDownClass = classmethod(BASE.BCILifetimeTest.tearDownClass.__func__)

    def test_frame_snapshot_kbuild_member(self):
        makefile = (ROOT / "kernel/linux/drivers/xtos/bci/Makefile").read_text()
        members = next(line.split(":=", 1)[1].split() for line in makefile.splitlines()
                       if line.startswith("xtos_bci_core-y :="))
        self.assertIn("xtos_bci_frame_snapshot.o", members)

    def test_read_frames(self):
        result = subprocess.run([str(self.binary), "read_frames"],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "read_frames")

    def test_frame_poll_independent(self):
        result = subprocess.run([str(self.binary), "frame_poll_independent"],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "frame_poll_independent")

    def test_compat_pointer_end(self):
        result = subprocess.run([str(self.binary), "compat_pointer_end"],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "compat_pointer_end")


def _case(name):
    def method(self):
        result = subprocess.run([str(self.binary), name], text=True,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), name)
    return method


for _name in ["frame_errno_no_owner", "frame_homogeneous_config", "frame_pointer_boundaries",
              "frame_acquire_shape_empty", "frame_acquire_copy_allocation", "frame_token_exhaustion",
              "frame_consume_overwrite", "frame_mmap_reopen", "frame_only_poll", "generic_control_wakes_frames",
              "snapshot_release_wakes", "snapshot_wakes_legacy", "snapshot_wakes_frames", "frame_family_tokens",
              "frame_release_detached", "frame_consume", "frame_acquire_mmap", "frame_empty_and_fault", "frame_shape_rejection", "frame_copy_failure",
              "frame_wait_stop", "frame_wait_unbind", "frame_overwrite_during_copy"]:
    setattr(FrameCoreTests, "test_" + _name, _case(_name))


if __name__ == "__main__":
    unittest.main()
