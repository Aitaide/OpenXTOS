"""Source/compile checks only; real ioctl/VM behavior requires the Linux guest."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class FrameGuestSources(unittest.TestCase):
    def test_guest_sources_are_separate_from_legacy_fixture(self):
        for name in ("frame_fixture.c", "frame_client.c"):
            self.assertTrue((ROOT / "tests/bci_kernel" / name).is_file(), name)

    def test_real_sdk_client_compiles_without_kernel_internal_headers(self):
        scratch = ROOT / ".build/xtos-roadmap-v1/p2-frame-guest-source"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="client-", dir=scratch) as temp:
            binary = Path(temp) / "client"
            compiler = shlex.split(os.environ.get("CC", "cc"))
            include = ROOT / "sdk/libxtos-bci/include"
            objects = []
            for name, source in (
                ("client", ROOT / "tests/bci_kernel/frame_client.c"),
                ("session", ROOT / "sdk/libxtos-bci/src/session.c"),
            ):
                deps = Path(temp) / (name + ".d")
                obj = Path(temp) / (name + ".o")
                command = compiler + [
                    "-std=c11", "-Wall", "-Wextra", "-Werror", "-pthread",
                    "-MMD", "-MF", str(deps), "-I" + str(include),
                    "-c", str(source), "-o", str(obj),
                ]
                result = subprocess.run(command, text=True, capture_output=True,
                                        timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                dependencies = shlex.split(deps.read_text().replace("\\\n", " "))
                headers = [Path(p) for p in dependencies if p.endswith(".h")]
                self.assertTrue(headers)
                for header in headers:
                    self.assertTrue(header.resolve().is_relative_to(include.resolve()),
                                    str(header))
                objects.append(str(obj))
            result = subprocess.run(compiler + ["-pthread", *objects, "-o", str(binary)],
                                    text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            # Does not open a device or print guest PASS markers in this mode.
            result = subprocess.run([str(binary), "--selftest"], text=True,
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout, "BCI_FRAME_CLIENT_SELFTEST=PASS\n")


if __name__ == "__main__":
    unittest.main()
