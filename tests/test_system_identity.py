#!/usr/bin/env python3
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gen_system_identity.py"
VERSION = ROOT / "VERSION"


class SystemIdentityTests(unittest.TestCase):
    def run_generator(self, output_dir, *extra):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--output-dir", str(output_dir), *extra],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

    def test_identity_outputs_match_exact_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            version = Path(tmp) / "VERSION"
            version.write_text(
                "XTOS_VERSION=1.2.3-test.1\n"
                "XTOS_RELEASE_NAME=Fixture Release\n"
                "XTOS_RELEASE_DATE=2026-10-02\n"
            )
            output = Path(tmp) / "rootfs" / "etc"
            result = self.run_generator(
                output,
                "--version-file",
                str(version),
                "--image-type",
                "developer",
                "--arch",
                "x86_64",
                "--kernel-release",
                "6.6.0-rt15",
                "--build-id",
                "build-123",
                "--source-state",
                "dirty",
                "--hardware-profile",
                "h3c-r4700-g3",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            expected = {
                "os-release": (
                    'NAME="XTOS"\nID=xtos\nVERSION_ID="1.2.3-test.1"\n'
                    'VERSION="1.2.3-test.1 (Fixture Release; XTOS Bate版本)"\n'
                    'PRETTY_NAME="XTOS Bate版本 1.2.3-test.1"\n'
                    'RELEASE_TYPE=development\nHOME_URL="https://github.com/Aitaide/XTOS"\n'
                ),
                "xtos-release": (
                    "XTOS_PRODUCT=XTOS\nXTOS_LABEL=XTOS Bate版本\nXTOS_VERSION=1.2.3-test.1\n"
                    "XTOS_RELEASE_NAME=Fixture Release\nXTOS_RELEASE_DATE=2026-10-02\n"
                    "XTOS_IMAGE_TYPE=developer\nXTOS_ARCHITECTURE=x86_64\n"
                    "XTOS_KERNEL_RELEASE=6.6.0-rt15\nXTOS_BUILD_ID=build-123\n"
                ),
                "issue": "XTOS Bate版本 1.2.3-test.1\n",
                "motd": "XTOS Bate版本 1.2.3-test.1\nimage=developer arch=x86_64 kernel=6.6.0-rt15\n",
            }
            for name, text in expected.items():
                self.assertEqual((output / name).read_text(), text, name)
            metadata = json.loads((output / "xtos-build.json").read_text())
            self.assertEqual(
                set(metadata),
                {
                    "architecture", "build_id", "hardware_profile", "image_type",
                    "kernel_release", "product_label", "product_name", "release_date",
                    "release_name", "source_state", "version",
                },
            )
            self.assertEqual(metadata["version"], "1.2.3-test.1")
            self.assertEqual(metadata["product_label"], "XTOS Bate版本")

    def test_repeat_is_byte_deterministic_and_default_build_id_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "one"
            second = Path(tmp) / "two"
            args = ["--image-type", "core", "--arch", "arm64", "--kernel-release", "6.6.0-rt15"]
            self.assertEqual(self.run_generator(first, *args).returncode, 0)
            self.assertEqual(self.run_generator(second, *args).returncode, 0)
            names = ["os-release", "xtos-release", "xtos-build.json", "issue", "motd"]
            for name in names:
                self.assertEqual((first / name).read_bytes(), (second / name).read_bytes(), name)
            self.assertEqual(json.loads((first / "xtos-build.json").read_text())["build_id"], "unknown")

    def test_malformed_version_file_is_rejected_without_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            version = Path(tmp) / "VERSION"
            version.write_text("XTOS_RELEASE_NAME=OnlyName\n")
            output = Path(tmp) / "out"
            result = self.run_generator(output, "--version-file", str(version), "--image-type", "core")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())

    def test_malformed_version_syntax_is_rejected_without_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            version = Path(tmp) / "VERSION"
            version.write_text(
                "XTOS_VERSION=not a version\n"
                "XTOS_RELEASE_NAME=Developer Preview 1\n"
                "XTOS_RELEASE_DATE=2026-10-02\n"
            )
            output = Path(tmp) / "out"
            result = self.run_generator(output, "--version-file", str(version), "--image-type", "core")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())

        with tempfile.TemporaryDirectory() as tmp:
            version = Path(tmp) / "VERSION"
            version.write_text("XTOS_VERSION=\"0.9.0\nXTOS_RELEASE_NAME=Developer Preview 1\nXTOS_RELEASE_DATE=2026-10-02\n")
            output = Path(tmp) / "out"
            result = self.run_generator(output, "--version-file", str(version), "--image-type", "core")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())

    def test_unsafe_image_type_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out"
            result = self.run_generator(output, "--image-type", "../escape")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())

    def test_source_version_is_not_modified(self):
        original = VERSION.read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_generator(Path(tmp) / "out", "--image-type", "ai")
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(VERSION.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
