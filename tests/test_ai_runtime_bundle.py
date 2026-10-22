#!/usr/bin/env python3
"""Focused regression tests for the private, hash-bound CPU ORT runtime installer."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/provision_ai_runtime.py"
EVIDENCE = Path(os.environ.get("XTOS_AI_RUNTIME_EVIDENCE", str(ROOT / ".build/p1-ort-clean-evidence")))
BUNDLE = Path(os.environ.get("XTOS_AI_RUNTIME_BUNDLE", str(ROOT / ".build/p1-runtime-assembled/clean-runtime.tar.gz")))
LOCK = ROOT / "tools/xtos-onnx-baseline/requirements-ai.lock"
DESCRIPTOR = Path(os.environ.get("XTOS_AI_RUNTIME_DESCRIPTOR", str(ROOT / "system/packages/ai-runtime-x86_64.json")))
HOST_AVX = " avx " in (" " + Path("/proc/cpuinfo").read_text().replace("\n", " ") + " ")
QEMU = [] if HOST_AVX else ["--qemu-x86-64", "/usr/bin/qemu-x86_64"]


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def run_tool(*args: str, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=ROOT,
                          capture_output=True, text=True, timeout=timeout)


def make_tar(path: Path, entries: list[tuple[str, str, bytes | str]]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, kind, data in entries:
            info = tarfile.TarInfo(name)
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)
            elif kind == "file":
                blob = data if isinstance(data, bytes) else data.encode()
                info.size = len(blob)
                info.mode = 0o644
                archive.addfile(info, __import__("io").BytesIO(blob))
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = str(data)
                info.mode = 0o777
                archive.addfile(info)
            elif kind == "hardlink":
                info.type = tarfile.LNKTYPE
                info.linkname = str(data)
                archive.addfile(info)
            elif kind == "device":
                info.type = tarfile.CHRTYPE
                info.devmajor, info.devminor = 1, 3
                archive.addfile(info)
            else:
                raise AssertionError(kind)


class PrivateRuntimeBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not BUNDLE.is_file():
            raise unittest.SkipTest(f"verified input bundle absent: {BUNDLE}")
        cls.tmp = tempfile.TemporaryDirectory(prefix="xtos-ai-runtime-bundle-")
        cls.work = Path(cls.tmp.name)
        cls.descriptor = cls.work / "descriptor.json"
        if not DESCRIPTOR.is_file():
            raise AssertionError(f"explicit descriptor absent: {DESCRIPTOR}")
        cls.descriptor.write_bytes(DESCRIPTOR.read_bytes())
        cls.descriptor_doc = json.loads(cls.descriptor.read_text())

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def install(self, bundle: Path = BUNDLE, descriptor: Path | None = None,
                output: Path | None = None) -> subprocess.CompletedProcess[str]:
        return run_tool("install", "--bundle", str(bundle),
                        "--descriptor", str(descriptor or self.descriptor),
                        "--lock", str(LOCK), "--output-root", str(output or self.work / "runtime"),
                        *QEMU, timeout=300)

    def test_01_descriptor_is_complete_and_real_hashes(self) -> None:
        self.assertEqual(self.descriptor_doc["bundle"]["sha256"], sha(BUNDLE))
        self.assertEqual(self.descriptor_doc["bundle"]["bytes"], BUNDLE.stat().st_size)
        self.assertEqual(self.descriptor_doc["lock"]["sha256"], sha(LOCK))
        self.assertEqual(self.descriptor_doc["runtime"],
                         {"CPython": "3.11.15", "onnxruntime": "1.23.2", "numpy": "1.26.4"})
        self.assertGreater(len(self.descriptor_doc["members"]), 8000)
        self.assertTrue(all("sha256" in row for row in self.descriptor_doc["members"]
                            if row["type"] == "file"))
        fresh = self.descriptor_doc["provenance"]["fresh_wheel_install"]
        self.assertIs(type(fresh), bool)
        self.assertEqual(self.descriptor_doc["provenance"]["classification"],
                         "reused_interpreter_fresh_locked_wheels" if fresh else "reused_verified_artifact")
        self.assertEqual(DESCRIPTOR.read_bytes(), self.descriptor.read_bytes())

    def test_02_real_bundle_extracts_and_private_inference_runs(self) -> None:
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        output = self.work / "runtime"
        manifest = json.loads((output / "provision-manifest.json").read_text())
        self.assertEqual(manifest["status"], "installed_verified_artifact")
        self.assertEqual(manifest["runtime"]["CPython"], "3.11.15")
        self.assertEqual(manifest["private_python_probe"]["system_python_fallback"], False)
        self.assertEqual(manifest["execution_mode"], "qemu-x86_64 -cpu max functional emulation")
        self.assertEqual(manifest["livecopy_input"], str(output))
        self.assertEqual(manifest["elf_closure"]["status"], "pass")
        listed = {row["path"] for row in manifest["files"]}
        self.assertNotIn("provision-manifest.json", listed)
        self.assertEqual(manifest["files_scope"], "all extracted and generated runtime files except this self-referential manifest")
        for row in manifest["files"]:
            path = output / row["path"]
            if row["type"] == "file":
                self.assertTrue(path.is_file())
                self.assertEqual(path.stat().st_size, row["bytes"])
                self.assertEqual(sha(path), row["sha256"])
            elif row["type"] == "symlink":
                self.assertTrue(path.is_symlink())
                self.assertEqual(os.readlink(path), row["target"])
            else:
                self.assertTrue(path.is_dir())
        baseline = output / "clean/baseline.py"
        model = output / "clean/fixtures/fixed_classifier.onnx"
        env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": "/usr/lib/python3/dist-packages",
               "PYTHONHOME": "/deliberately-invalid-system-python", "PYTHONNOUSERSITE": "1",
               "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}
        command = (["/usr/bin/qemu-x86_64", "-cpu", "max", str(output / "clean/python/bin/python3.11"), "-I"]
                   if QEMU else [str(output / "bin/xtos-ai-python")])
        probe = subprocess.run(command + [str(baseline),
                                "--model-path", str(model), "--iterations", "20",
                                "--deadline-ms", "1000"], cwd=output, env=env,
                               capture_output=True, text=True, timeout=120)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(probe.stderr, "")
        document = json.loads(probe.stdout)
        self.assertEqual(document["status"], "ok")
        self.assertEqual(document["safe_action"], "action_allowed")
        self.assertEqual(document["execution_provider"], "CPUExecutionProvider")
        self.assertEqual(document["onnxruntime_version"], "1.23.2")
        self.assertEqual(document["model_sha256"], "6e6226cf772934a3f7a398ecb55ffd290503497d91803ff499a7aa28bf90a113")
        private = output / "clean/python/bin/python3.11"
        hidden = output / "clean/python/bin/python3.11.hidden"
        private.rename(hidden)
        try:
            no_fallback = subprocess.run([str(output / "bin/xtos-ai-python"), "-c", "print('must-not-run')"],
                                         cwd=output, env={"PATH": "/usr/bin:/bin"},
                                         capture_output=True, text=True, timeout=10)
            self.assertEqual(no_fallback.returncode, 127)
            self.assertIn("private XTOS Python missing", no_fallback.stderr)
        finally:
            hidden.rename(private)

    def test_03_hash_mismatch_rejected_before_extract(self) -> None:
        poisoned = self.work / "poisoned.tar.gz"
        poisoned.write_bytes(BUNDLE.read_bytes()[:-1] + bytes([BUNDLE.read_bytes()[-1] ^ 1]))
        output = self.work / "poison-output"
        result = self.install(poisoned, output=output)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())

    def test_04_descriptor_inventory_and_size_mismatch_rejected(self) -> None:
        bad = json.loads(self.descriptor.read_text())
        bad["members"] = bad["members"][:-1]
        path = self.work / "missing-member.json"
        path.write_text(json.dumps(bad, indent=2, sort_keys=True) + "\n")
        result = self.install(descriptor=path, output=self.work / "inventory-output")
        self.assertNotEqual(result.returncode, 0)
        bad = json.loads(self.descriptor.read_text())
        bad["bundle"]["bytes"] += 1
        path.write_text(json.dumps(bad, indent=2, sort_keys=True) + "\n")
        result = self.install(descriptor=path, output=self.work / "size-output")
        self.assertNotEqual(result.returncode, 0)
        bad = json.loads(self.descriptor.read_text())
        bad["lock"]["sha256"] = "0" * 64
        path.write_text(json.dumps(bad, indent=2, sort_keys=True) + "\n")
        result = self.install(descriptor=path, output=self.work / "lock-output")
        self.assertNotEqual(result.returncode, 0)

    def test_05_fresh_output_only_and_alias_refused(self) -> None:
        output = self.work / "existing"
        output.mkdir()
        (output / "sentinel").write_text("do not touch")
        result = self.install(output=output)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((output / "sentinel").read_text(), "do not touch")
        alias = self.work / "alias"
        alias.symlink_to(output, target_is_directory=True)
        result = self.install(output=alias)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((output / "sentinel").read_text(), "do not touch")

    def assert_bad_archive(self, entries: list[tuple[str, str, bytes | str]], label: str) -> None:
        archive = self.work / f"{label}.tar.gz"
        make_tar(archive, entries)
        descriptor = self.work / f"{label}.json"
        # Use the real descriptor fields but point the bundle digest at this fixture;
        # archive safety must then fail on metadata/inventory, not on an unhelpful hash.
        doc = json.loads(self.descriptor.read_text())
        doc["bundle"] = {"file": archive.name, "bytes": archive.stat().st_size, "sha256": sha(archive)}
        doc["members"] = []
        descriptor.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
        result = self.install(archive, descriptor, self.work / f"{label}-out")
        self.assertNotEqual(result.returncode, 0, label)



class HostileTarFixtureTests(unittest.TestCase):
    """Run hostile archive cases independently of the optional real bundle."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="xtos-ai-tar-fixture-")
        self.work = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def inspect(self, entries: list[tuple[str, str, bytes | str]], label: str) -> list[dict]:
        # Static preflight only: never extract or execute a hostile fixture.
        archive = self.work / f"{label}.tar.gz"
        make_tar(archive, entries)
        spec = importlib.util.spec_from_file_location("provision_ai_runtime", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.inspect_archive(archive)

    def test_symlink_dotdot_is_checked_after_intermediate_expansion(self) -> None:
        common = [(name, "dir", b"") for name in (
            "clean", "clean/python", "clean/python/deep", "clean/python/deep/level",
            "clean/fixtures")]
        common += [("clean/python/foo", "file", b"fixture"),
                   ("clean/python/deep/level/a", "symlink", "../../../fixtures")]
        # SYMLINK-ESCAPE-001 reviewer boundary: lexical normalization hides
        # the ascent above clean/ after a expands to the shallower fixtures/.
        with self.assertRaisesRegex(ValueError, "symlink escapes bundle root"):
            self.inspect(common + [("clean/python/deep/level/b", "symlink",
                                    "a/../../../../python/foo")], "ordered-escape")

    def test_safe_relative_symlink_components_remain_compatible(self) -> None:
        common = [(name, "dir", b"") for name in (
            "clean", "clean/python", "clean/python/a", "clean/python/deep",
            "clean/python/deep/level", "clean/fixtures")]
        entries = common + [
            ("clean/python/a/target", "file", b"fixture"),
            ("clean/python/deep/relative", "symlink", "../a/target"),
            ("clean/python/deep/level/a", "symlink", "../../../fixtures"),
            ("clean/python/deep/level/b", "symlink", "a/../python/a/target"),
            # The same directory link may be visited twice without a cycle.
            ("clean/python/alias", "symlink", "a"),
            ("clean/python/repeated", "symlink", "alias/../alias/target"),
        ]
        rows = self.inspect(entries, "safe-components")
        self.assertEqual(len(rows), len(entries))
        self.assertEqual(sum(row["type"] == "symlink" for row in rows), 5)

    def test_invalid_symlink_prefixes_cycles_and_depth_are_rejected(self) -> None:
        common = [("clean", "dir", b""), ("clean/python", "dir", b""),
                  ("clean/python/file", "file", b"fixture")]
        cases = {
            "missing-prefix": [("clean/python/link", "symlink", "missing/../file")],
            "file-prefix": [("clean/python/link", "symlink", "file/../file")],
            "file-dot": [("clean/python/link", "symlink", "file/.")],
            "file-slash": [("clean/python/link", "symlink", "file/")],
            "cycle": [("clean/python/link", "symlink", "other"),
                      ("clean/python/other", "symlink", "link")],
            "depth": [(f"clean/python/link{i}", "symlink", f"link{i + 1}")
                      for i in range(65)] + [("clean/python/link65", "symlink", "file")],
            # Expansion itself has a component/.. sequence, not just the
            # original link. It must use the same ordered checks recursively.
            "nested-escape": [("clean/python/d", "dir", b""),
                              ("clean/python/d/a", "symlink", "../../python"),
                              ("clean/python/d/b", "symlink", "a/../../../python/file"),
                              ("clean/python/link", "symlink", "d/b")],
        }
        for label, entries in cases.items():
            with self.subTest(label=label), self.assertRaises(ValueError):
                self.inspect(common + entries, label)

    def rejects(self, entries: list[tuple[str, str, bytes | str]], label: str) -> None:
        archive = self.work / f"{label}.tar.gz"
        make_tar(archive, entries)
        descriptor = self.work / f"{label}.json"
        descriptor.write_text(json.dumps({
            "schema": "xtos-ai-runtime-bundle-v1",
            "architecture": "x86_64",
            "runtime": {"CPython": "3.11.15", "onnxruntime": "1.23.2", "numpy": "1.26.4"},
            "bundle": {"file": archive.name, "bytes": archive.stat().st_size, "sha256": sha(archive)},
            "lock": {"sha256": sha(LOCK)},
        }))
        output = self.work / f"{label}-out"
        result = run_tool("install", "--bundle", str(archive), "--descriptor", str(descriptor),
                          "--lock", str(LOCK), "--output-root", str(output))
        self.assertNotEqual(result.returncode, 0, label)
        self.assertFalse(output.exists(), label)

    def test_hostile_archive_fixtures_rejected_without_real_bundle(self) -> None:
        common = [("clean", "dir", b""), ("clean/python", "dir", b"")]
        self.rejects(common + [("clean/../escape", "file", b"x")], "traversal")
        self.rejects([("clean", "dir", b""), ("clean/python", "file", b"x"),
                      ("clean/python/x", "file", b"x")], "path-shadow")
        self.rejects(common + [("clean/python/link", "symlink", "../../outside")], "symlink-escape")
        self.rejects(common + [("clean/python/target", "dir", b""),
                               ("clean/python/link", "symlink", "target"),
                               ("clean/python/link/child", "file", b"x")], "symlink-parent")
        self.rejects(common + [("clean/python/link", "hardlink", "clean/python")], "hardlink")
        self.rejects(common + [("clean/python/device", "device", b"")], "device")
        self.rejects(common + [("clean/python/x", "file", b"one"),
                               ("clean/python/x", "file", b"two")], "duplicate")


if __name__ == "__main__":
    unittest.main(verbosity=2)
