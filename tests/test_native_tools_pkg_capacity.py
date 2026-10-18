#!/usr/bin/env python3
"""Read-only pkg manifest fixtures; no chroot or system-directory writes."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "tools" / "xtos-native"
SRC = NATIVE / "pkg" / "pkg.c"


@unittest.skipUnless(shutil.which("gcc"), "gcc unavailable")
class PackageCapacityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="xtos-pkg-capacity-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.directory = Path(cls.temp.name)
        cls.manifest = cls.directory / "manifest.json"
        cls.binary = cls.directory / "pkg"
        cls.payload = cls.directory / "tail-file"
        cls.payload.write_bytes(b"pkg capacity fixture\n")
        mon = NATIVE / "vendor" / "monocypher"
        result = subprocess.run([
            "gcc", "-O2", "-Wall", "-Wextra", "-Werror", "-static",
            "-I", str(NATIVE / "common"), "-I", str(mon),
            f"-DMANIFEST_PATH={json.dumps(str(cls.manifest))}",
            "-o", str(cls.binary), str(SRC), str(mon / "monocypher.c"),
            str(mon / "monocypher-ed25519.c"),
        ], capture_output=True, text=True)
        if result.returncode:
            raise AssertionError(result.stderr)

    def write_manifest(self, packages):
        manifest = {
            "architecture": "x86_64",
            "lock": {"packages": [p["package"] for p in packages]},
            "lock_sha256": "a" * 64,
            "packages": packages,
            "schema_version": 1,
        }
        with self.manifest.open("w") as stream:
            json.dump(manifest, stream, indent=2, sort_keys=True)
            stream.write("\n")

    def write_file_pool_manifest(self, file_count):
        """Stream a canonical indent-2 manifest around the compiled file cap."""
        package = self.package(0)["package"]
        data = self.payload.read_bytes()
        with self.manifest.open("w") as stream:
            stream.write(
                '{\n'
                '  "architecture": "x86_64",\n'
                '  "lock": {\n'
                '    "packages": []\n'
                '  },\n'
                f'  "lock_sha256": "{"a" * 64}",\n'
                '  "packages": [\n'
                '    {\n'
                '      "files": [\n'
            )
            for index in range(file_count):
                metadata = {"path": f"/fixture/{index:06d}", "size": 0, "type": "directory"}
                if index + 1 == file_count:
                    metadata = {"path": str(self.payload), "size": len(data), "type": "file",
                                "sha256": hashlib.sha256(data).hexdigest()}
                entry = json.dumps(metadata, indent=2, sort_keys=True)
                stream.write("        " + entry.replace("\n", "\n        "))
                stream.write(",\n" if index + 1 < file_count else "\n")
            stream.write('      ],\n      "package": ')
            package_json = json.dumps(package, indent=2, sort_keys=True)
            stream.write(package_json.replace("\n", "\n      "))
            stream.write('\n    }\n  ],\n  "schema_version": 1\n}\n')

    def package(self, number, files=None):
        return {
            "files": [] if files is None else files,
            "package": {
                "architecture": "amd64", "name": f"fixture-{number:04d}",
                "sha256": hashlib.sha256(str(number).encode()).hexdigest(),
                "size": 1000 + number,
                "source_url": f"https://example.invalid/fixture-{number}.deb",
                "version": f"1.{number}",
            },
        }

    def run_pkg(self, *args):
        return subprocess.run([str(self.binary), *args], capture_output=True,
                              text=True, timeout=60, env={**os.environ, "TERM": "dumb"})

    def assert_success(self, *args):
        result = self.run_pkg(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_large_inventory_preserves_every_package(self):
        data = self.payload.read_bytes()
        tail_file = {"path": str(self.payload), "sha256": hashlib.sha256(data).hexdigest(),
                     "size": len(data), "type": "file"}
        for count in (530, 600):
            packages = [self.package(i, [{"path": f"/fixture/{i}", "type": "directory"}])
                        for i in range(count)]
            packages[-2]["package"]["name"] = "vulkan-tools"
            packages[-1]["package"]["name"] = "zz-tail"
            packages[-1]["files"] = [tail_file]
            self.write_manifest(packages)
            with self.subTest(count=count, command="list"):
                rows = json.loads(self.assert_success("--json", "list"))
                self.assertEqual(len(rows), count)
                self.assertEqual(rows, [
                    {"name": p["package"]["name"], "version": p["package"]["version"],
                     "arch": "amd64", "files": 1, "deb_bytes": p["package"]["size"]}
                    for p in packages
                ])
            with self.subTest(count=count, command="summary"):
                self.assertIn(f"Packages:      {count}\n", self.assert_success("summary"))
            for index in (511, 512, count - 2, count - 1):
                meta = packages[index]["package"]
                with self.subTest(count=count, command="show", name=meta["name"]):
                    row = json.loads(self.assert_success("--json", "show", meta["name"]))
                    self.assertEqual(row, {
                        "name": meta["name"], "version": meta["version"],
                        "architecture": meta["architecture"], "deb_sha256": meta["sha256"],
                        "deb_bytes": meta["size"], "source_url": meta["source_url"],
                        "installed_files": int(index == count - 1),
                        "installed_bytes": len(data) if index == count - 1 else 0,
                    })
            with self.subTest(count=count, command="verify", name="vulkan-tools"):
                self.assertIn("PKG_VERIFY checked=0 ok=0 missing=0 corrupt=0 overridden=0",
                              self.assert_success("verify", "vulkan-tools"))
            with self.subTest(count=count, command="verify", name="zz-tail"):
                self.assertEqual(json.loads(self.assert_success("--json", "verify", "zz-tail")),
                                 {"checked": 1, "ok": 1, "missing": 0, "corrupt": 0, "overridden": 0})
                self.assertIn(str(self.payload), self.assert_success("files", "zz-tail"))

    def test_file_pool_overflow_refuses_partial_inventory(self):
        self.write_file_pool_manifest(262145)
        for args in (("summary",), ("--json", "list"), ("verify", "--all"),
                     ("verify", "fixture-0000")):
            with self.subTest(command=args):
                result = self.run_pkg(*args)
                self.assertNotEqual(result.returncode, 0, "partial manifest accepted")
                self.assertEqual(result.stdout, "")
                self.assertIn("manifest file capacity exceeded", result.stderr)

    def test_file_pool_limit_is_accepted(self):
        self.write_file_pool_manifest(262144)
        rows = json.loads(self.assert_success("--json", "list"))
        self.assertEqual(rows[0]["files"], 262144)
        self.assertEqual(json.loads(self.assert_success("--json", "verify", "fixture-0000")),
                         {"checked": 1, "ok": 1, "missing": 0, "corrupt": 0, "overridden": 0})

    def test_compile_time_manifest_fixture(self):
        self.write_manifest([self.package(0)])
        self.assertIn("Packages:      1\n", self.assert_success("summary"))
        self.assertEqual(json.loads(self.assert_success("--json", "show", "fixture-0000"))["version"], "1.0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
