"""Compile the actual SDK session implementation at its OS boundary."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SDK = ROOT / "sdk/libxtos-bci"
WRAPS = ["open", "close", "ioctl", "mmap", "munmap"]


class SnapshotSDKTests(unittest.TestCase):
    def test_real_session_snapshot_contract(self):
        scratch = Path(os.environ.get("TMPDIR", "/root/.hermes/cache/scratch"))
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="bci-sdk-", dir=scratch) as tmp:
            binary = Path(tmp) / "test_snapshot"
            command = shlex.split(os.environ.get("CC", "cc")) + [
                "-std=c11", "-Wall", "-Wextra", "-Werror", "-g",
                "-I" + str(SDK / "include"),
            ]
            if os.environ.get("BCI_SDK_SANITIZE") == "1":
                command += ["-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                            "-fno-omit-frame-pointer", "-no-pie"]
            command += [str(SDK / "tests/test_snapshot.c"), str(SDK / "src/session.c")]
            command += ["-Wl,--wrap=" + name for name in WRAPS]
            command += ["-o", str(binary)]
            result = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([str(binary)], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertRegex(result.stdout, r"snapshot_sdk_cases=\d+ pass")
            print(result.stdout.strip())


    def test_makefile_tests_and_installed_headers(self):
        scratch = Path(os.environ.get("TMPDIR", "/root/.hermes/cache/scratch"))
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="bci-sdk-build-", dir=scratch) as tmp:
            tree = Path(tmp) / "sdk"
            shutil.copytree(SDK, tree, ignore=shutil.ignore_patterns(
                "*.o", "*.a", "*.so*", "test_samples", "test_snapshot", "test_snapshot_uapi", "test_config"))
            # Existing sample test hardcodes /tmp; keep this isolated fixture
            # under the configured scratch directory without editing source.
            fixture = tree / "tests/test_samples.c"
            fixture.write_text(fixture.read_text().replace(
                "/tmp/xtos-bci-samples-XXXXXX", str(Path(tmp) / "samples-XXXXXX")))
            result = subprocess.run(["make", "test"], cwd=tree, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("snapshot_sdk_cases=", result.stdout)
            self.assertIn("snapshot_uapi=pass", result.stdout)
            self.assertIn("BCI sample conversion: PASS", result.stdout)
            self.assertIn("config_sdk_cases=25 pass", result.stdout)
            dest = Path(tmp) / "installed"
            result = subprocess.run(["make", "install", "DESTDIR=" + str(dest)],
                                    cwd=tree, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for name in ["bci.h", "bci_uapi.h", "bci_snapshot_uapi.h", "bci_config_uapi.h", "bci_version.h"]:
                installed = dest / "usr/include/xtos" / name
                self.assertTrue(installed.is_file(), str(installed))
                self.assertEqual(installed.read_bytes(), (SDK / "include/xtos" / name).read_bytes())
            client = Path(tmp) / "client.c"
            client.write_text('#include <xtos/bci.h>\n#include <xtos/bci_version.h>\n#include <errno.h>\n'
                              'int main(void) { struct xtos_bci_config_v1 cfg = {0}; '
                              'if (xtos_bci_get_config(0, &cfg) != -1 || errno != EINVAL) return 1; '
                              'return xtos_bci_snapshot_release(0) != -1; }\n')
            command = shlex.split(os.environ.get("CC", "cc")) + [
                "-Werror", "-I" + str(dest / "usr/include"), str(client),
                "-L" + str(dest / "usr/lib"), "-lxtos-bci",
                "-Wl,-rpath," + str(dest / "usr/lib"), "-o", str(Path(tmp) / "client")]
            result = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([str(Path(tmp) / "client")], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            print("snapshot_make_install=pass sample_conversion=pass layout=pass client=pass")


if __name__ == "__main__":
    unittest.main()
