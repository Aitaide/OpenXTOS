#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Regression tests for source-only release packaging contracts."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parent


class SourcePackagingTests(unittest.TestCase):
    def test_arm_offer_identifies_static_libc_source_asset(self):
        sys.path.insert(0, str(SCRIPTS))
        from install_project_notices import install
        with tempfile.TemporaryDirectory(prefix="openxtos-arm-offer-test-") as temp:
            doc = install(SCRIPTS.parent, Path(temp), "arm64 bundle")
            offer = (doc / "CORRESPONDING-SOURCE.txt").read_text()
            self.assertIn("GNU C Library", offer)
            self.assertIn("arm64-corresponding-source.tar.gz", offer)
            self.assertIn("SOURCE_PACKAGES.tsv", offer)

    def test_records_real_static_runtime_files(self):
        import shutil
        if not shutil.which("gcc"):
            self.skipTest("gcc not installed")
        with tempfile.TemporaryDirectory(prefix="openxtos-runtime-test-") as temp:
            output = Path(temp) / "runtime.txt"
            result = subprocess.run([sys.executable, str(SCRIPTS / "record_static_runtime.py"), "gcc", str(output)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            paths = [Path(line) for line in output.read_text().splitlines()]
            self.assertEqual({path.name for path in paths}, {"libc.a", "libgcc.a"})
            self.assertTrue(all(path.is_absolute() and path.is_file() for path in paths))

    def test_existing_enabled_source_repository_is_success(self):
        with tempfile.TemporaryDirectory(prefix="openxtos-source-test-") as temp:
            source = Path(temp) / "ubuntu.sources"
            source.write_text("Types: deb deb-src\nURIs: http://archive.ubuntu.com/ubuntu\nSuites: noble\nComponents: main\n")
            result = subprocess.run([sys.executable, str(SCRIPTS / "enable_deb_src.py"), str(source)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("already enabled", result.stdout)

    def test_populated_source_output_is_rejected_before_network(self):
        with tempfile.TemporaryDirectory(prefix="openxtos-source-test-") as temp:
            root = Path(temp)
            output = root / "archives"
            output.mkdir()
            (output / "cached.dsc").write_text("existing archive")
            manifest = root / "sources.tsv"
            manifest.write_text("source_package\tsource_version\tbinary_packages\nexample\t1.0\texample\n")
            result = subprocess.run([sys.executable, str(SCRIPTS / "fetch_distribution_sources.py"), str(manifest), str(output)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("output directory must be empty", result.stderr)
            self.assertNotIn("fetch source package", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
