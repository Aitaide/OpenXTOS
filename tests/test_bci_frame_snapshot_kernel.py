"""Compile the production frame ring and snapshot against host services."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class FrameSnapshotHostTests(unittest.TestCase):
    def test_production_snapshot(self):
        mode = os.environ.get("XTOS_SNAPSHOT_SANITIZER", "normal")
        flags = {"normal": [], "asan": ["-fsanitize=address,undefined", "-fno-omit-frame-pointer"], "tsan": ["-fsanitize=thread"]}[mode]
        scratch = ROOT / ".build/xtos-roadmap-v1/p2-frame-snapshot"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="host-", dir=scratch) as tmp:
            binary = Path(tmp) / "snapshot_test"
            subprocess.run([os.environ.get("CC", "cc"), "-std=c11", "-Wall", "-Wextra", "-Werror", "-g", "-O1", "-pthread", *flags, str(ROOT / "tests/bci_frame_snapshot_host/snapshot_test.c"), "-o", str(binary)], check=True, timeout=60)
            result = subprocess.run([str(binary)], text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout, "SNAPSHOT_CASES=8\n")

    def test_independent_export_dependencies(self):
        paths = [
            "tests/bci_frame_snapshot_host/snapshot_test.c",
            "tests/bci_frame_snapshot_host/snapshot_shim.h",
            "tests/bci_frame_host/frame_shim.h",
            "kernel/linux/drivers/xtos/bci/xtos_bci_frame.c",
            "kernel/linux/drivers/xtos/bci/xtos_bci_frame.h",
            "kernel/linux/drivers/xtos/bci/xtos_bci_frame_snapshot.c",
            "kernel/linux/drivers/xtos/bci/xtos_bci_frame_snapshot.h",
            "kernel/linux/include/uapi/linux/xtos_bci_frame.h",
            "kernel/linux/include/uapi/linux/xtos_bci_config.h",
        ]
        scratch = ROOT / ".build/xtos-roadmap-v1/p2-frame-snapshot"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="export-", dir=scratch) as tmp:
            export = Path(tmp)
            for name in paths:
                dest = export / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / name, dest)
            depfile = export / "dependencies.d"
            binary = export / "snapshot_test"
            subprocess.run([os.environ.get("CC", "cc"), "-std=c11", "-Wall", "-Wextra", "-Werror", "-O1", "-pthread", "-MMD", "-MF", str(depfile), str(export / paths[0]), "-o", str(binary)], check=True, timeout=60)
            dependencies = depfile.read_text().replace("\\\n", " ").split(":", 1)[1].split()
            for name in dependencies:
                self.assertTrue(Path(name).resolve().is_relative_to(export), name)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout, "SNAPSHOT_CASES=8\n")

if __name__ == "__main__":
    unittest.main()
