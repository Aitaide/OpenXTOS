#!/usr/bin/env python3
"""Run the real release CLI with isolated, controlled build producers.

These fixtures exercise packaging only; they do not establish a kernel build
or boot result. Every external producer is a fixed fixture, never a mutable
cross-build script from the shared checkout.
"""
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import runpy
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = Path(os.environ.get("TMPDIR", "/root/.hermes/cache/scratch"))
BUILD_STUB = '''#!/usr/bin/env python3
import json, os, gzip
from pathlib import Path
root = Path(__file__).resolve().parents[1]
build = Path(os.environ["BUILD_ROOT"])
arch = {"build_system.sh": "x86_64", "build_system_arm64.sh": "arm64",
        "build_system_riscv64.sh": "riscv64"}[Path(__file__).name]
with (root / "calls").open("a") as f:
    f.write(arch + "\\n")
fault = json.loads(os.environ.get("RELEASE_FIXTURE_FAULT", "{}"))
if fault.get("build_fail") == arch:
    raise SystemExit(23)
kdir = build / ("kernel-build" if arch == "x86_64" else "kernel-build-" + arch)
release = fault.get("release", {}).get(arch, "7.3.1")
config = 'CONFIG_PREEMPT_RT=y\\nCONFIG_LOCALVERSION=""\\n'
if release == "7.3.1-xtos":
    config = 'CONFIG_PREEMPT_RT=y\\nCONFIG_LOCALVERSION="-xtos"\\n'
files = {kdir / "include/config/kernel.release": release + "\\n", kdir / ".config": config}
if arch == "x86_64":
    files.update({build / "images/xtos-bzImage": "fresh-x86_64",
                  build / "images/xtos-rootfs.cpio.gz": "fresh-rootfs-x86_64",
                  build / "images/kernel.config": config})
else:
    files.update({build / ("kernel-install-" + arch) / "Image": "fresh-" + arch,
                  build / ("initramfs-" + arch + ".cpio.gz"): "fresh-rootfs-" + arch})
for path, data in files.items():
    if str(path.relative_to(build)) == fault.get("missing"):
        path.unlink(missing_ok=True)
        continue
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)
image_path = (build / "images/xtos-bzImage" if arch == "x86_64" else
              build / ("kernel-install-" + arch) / "Image")
if str(image_path.relative_to(build)) != fault.get("missing"):
    data = bytearray(4096)
    if arch == "x86_64":
        data[0x1fe:0x200] = bytes.fromhex("55aa")
        data[0x202:0x206] = b"HdrS"
        data[0x206:0x208] = (0x20f).to_bytes(2, "little")
        data[0x20e:0x210] = (0x100).to_bytes(2, "little")
        version = (release + chr(0)).encode()
        data[0x300:0x300+len(version)] = version
    else:
        data[56:60] = b"ARMd" if arch == "arm64" else bytes.fromhex("52534305")
        banner = ("Linux version " + release + " fixture" + chr(0)).encode()
        data[128:128+len(banner)] = banner
    data += b"IKCFG_ST" + gzip.compress(config.encode(), mtime=0) + b"IKCFG_ED"
    image_path.write_bytes(data)
'''
SBOM_STUB = '''import argparse, json
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument("--out", required=True)
a = p.parse_args()
root = Path(__file__).resolve().parents[2]
pin = json.loads((root / "kernel/source-pin.json").read_text())
Path(a.out).write_text(json.dumps({"bomFormat": "CycloneDX", "components": [
    {"name": "linux", "version": pin["version"]}]}))
'''


class ReleasePackagingTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="release-packaging-", dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.build = self.root / ".build"
        for name in ("scripts", "tools/xtos-sbom", "docs", "kernel", "kernel/linux/scripts"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        for name in ("make_release.sh", "reproducible_env.sh"):
            source = (Path(os.environ.get("RELEASE_FIXTURE_SCRIPT", ROOT / "scripts/make_release.sh"))
                      if name == "make_release.sh" else ROOT / "scripts" / name)
            shutil.copy2(source, self.root / "scripts" / name)
        shutil.copy2(ROOT / "kernel/source-pin.json", self.root / "kernel/source-pin.json")
        shutil.copy2(ROOT/'kernel/linux/scripts/extract-ikconfig',
                     self.root/'kernel/linux/scripts/extract-ikconfig')
        shutil.copy2(ROOT / "VERSION", self.root / "VERSION")
        for name in ("sbom.md", "reproducible_build.md", "abi_versioning.md"):
            (self.root / "docs" / name).write_text("fixture " + name + "\n")
        (self.root / "tools/xtos-sbom/gen_sbom.py").write_text(SBOM_STUB)
        for name in ("build_system.sh", "build_system_arm64.sh", "build_system_riscv64.sh"):
            script = self.root / "scripts" / name
            script.write_text(BUILD_STUB)
            script.chmod(0o755)
        # Exactly the old release script's reuse predicate, with legacy identity.
        for arch in ("arm64", "riscv64"):
            kdir = self.build / ("kernel-build-" + arch)
            (kdir / "include/config").mkdir(parents=True)
            (kdir / "include/config/kernel.release").write_text("6.6.0-rt15\n")
            (kdir / ".config").write_text('CONFIG_LOCALVERSION=""\n')
            image = self.build / ("kernel-install-" + arch) / "Image"
            image.parent.mkdir(parents=True)
            image.write_text("legacy-" + arch)
            (self.build / ("initramfs-" + arch + ".cpio.gz")).write_text("legacy-rootfs")
        version = next(line.split("=", 1)[1] for line in (self.root / "VERSION").read_text().splitlines()
                       if line.startswith("XTOS_VERSION="))
        self.release = self.build / f"release/xtos-{version}"

    def run_release(self, fault=None, x86_only=False):
        env = dict(os.environ, BUILD_ROOT=str(self.build), JOBS="1", SOURCE_DATE_EPOCH="1",
                   RELEASE_FIXTURE_FAULT=json.dumps(fault or {}), PYTHONDONTWRITEBYTECODE="1")
        command = ["bash", str(self.root / "scripts/make_release.sh")]
        if x86_only:
            command.append("--x86-only")
        result = subprocess.run(command, env=env, cwd=self.root, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
        artifacts = {str(p.relative_to(self.release)): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in sorted(self.release.rglob("*")) if p.is_file()}
        print("RELEASE_CLI_EVIDENCE=" + json.dumps({"test": self.id(), "argv": command,
              "fault": fault, "raw_exit": result.returncode, "output": result.stdout,
              "artifacts_sha256": artifacts}), flush=True)
        return result

    def test_legacy_cross_cache_is_rebuilt(self):
        result = self.run_release()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((self.root / "calls").read_text().splitlines(),
                         ["x86_64", "arm64", "riscv64"])
        for arch in ("arm64", "riscv64"):
            self.assertEqual((self.release / arch / "Image").read_bytes(),
                             (self.build/('kernel-install-'+arch)/'Image').read_bytes())

    def test_cross_configs_are_packaged(self):
        result = self.run_release()
        self.assertEqual(result.returncode, 0, result.stdout)
        for arch in ("arm64", "riscv64"):
            self.assertEqual((self.release / arch / "kernel.config").read_bytes(),
                             (self.build / ("kernel-build-" + arch) / ".config").read_bytes())

    def test_missing_cross_artifacts_are_rejected(self):
        for arch in ("arm64", "riscv64"):
            for missing in (f"kernel-install-{arch}/Image", f"initramfs-{arch}.cpio.gz",
                            f"kernel-build-{arch}/.config",
                            f"kernel-build-{arch}/include/config/kernel.release"):
                with self.subTest(missing=missing):
                    result = self.run_release({"missing": missing})
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn("kernel artifact missing or empty", result.stdout)

    def test_wrong_release_is_rejected_for_every_architecture(self):
        for arch in ("x86_64", "arm64", "riscv64"):
            with self.subTest(arch=arch):
                result = self.run_release({"release": {arch: "6.6.0-rt15"}})
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("kernel.release mismatch", result.stdout)

    def test_readme_uses_actual_release_with_explicit_localversion(self):
        result = self.run_release({"release": {"arm64": "7.3.1-xtos"}})
        self.assertEqual(result.returncode, 0, result.stdout)
        readme = (self.release / "README.md").read_text()
        self.assertIn("7.3.1-xtos", readme)
        self.assertNotIn("6.6.0-rt15", readme)

    def test_unconfigured_release_suffix_is_rejected(self):
        result = self.run_release({"release": {"riscv64": "7.3.1-other"}})
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("kernel.release mismatch", result.stdout)

    def test_x86_only_keeps_original_cli_option(self):
        result = self.run_release(x86_only=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((self.root / "calls").read_text().splitlines(), ["x86_64"])
        self.assertFalse((self.release / "arm64").exists())
        self.assertFalse((self.release / "riscv64").exists())
        self.assertIn("Linux 7.3.1", (self.release / "README.md").read_text())

    def test_release_notes_do_not_promote_historical_validation(self):
        result = self.run_release(x86_only=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        notes = (self.release/'RELEASE_NOTES.md').read_text()
        self.assertIn('Validation status: NOT_VERIFIED_BY_PACKAGING', notes)
        self.assertNotIn('## Validated in this release', notes)
        self.assertNotIn('in-guest validation 14/14', notes)
        self.assertNotIn('boot 5/5', notes)

    def test_failed_build_is_propagated(self):
        result = self.run_release({"build_fail": "arm64"})
        self.assertEqual(result.returncode, 23, result.stdout)

    def test_packaged_x86_config_must_equal_resolved_build_config(self):
        producer = self.root/'scripts/build_system.sh'
        producer.write_text(producer.read_text() + "\n(build/'images/kernel.config').write_text('# CONFIG_PREEMPT_RT is not set\\n')\n")
        result = self.run_release(x86_only=True)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('packaged config differs', result.stdout)

    def test_actual_x86_image_rejects_old_version_with_new_sidecar(self):
        image = bytearray(4096)
        image[0x1fe:0x200] = b'\x55\xaa'
        image[0x202:0x206] = b'HdrS'
        image[0x206:0x208] = (0x20f).to_bytes(2, 'little')
        image[0x20e:0x210] = (0x100).to_bytes(2, 'little')
        image[0x300:0x30c] = b'6.6.0-rt15\0'
        (self.root/'old-bzImage').write_bytes(image)
        producer = self.root/'scripts/build_system.sh'
        producer.write_text(producer.read_text() + "\n(build/'images/xtos-bzImage').write_bytes((root/'old-bzImage').read_bytes())\n")
        result = self.run_release(x86_only=True)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('actual kernel image', result.stdout)

    def test_failed_cross_install_is_not_swallowed(self):
        bindir = self.root / "bin"
        bindir.mkdir()
        wrapper = bindir / "install"
        install_command = shutil.which("install") or "/usr/bin/install"
        wrapper.write_text("#!/bin/bash\nfor arg in \"$@\"; do\n"
                           "  [[ \"$arg\" == */kernel-install-arm64/Image ]] && exit 29\n"
                           "done\nexec " + install_command + ' "$@"\n')
        wrapper.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(bindir) + os.pathsep + os.environ["PATH"]}):
            result = self.run_release()
        self.assertEqual(result.returncode, 29, result.stdout)

    def test_real_sbom_uses_new_pin_dependencies(self):
        generator = self.root / "tools/xtos-sbom/gen_sbom.py"
        shutil.copy2(ROOT / "tools/xtos-sbom/gen_sbom.py", generator)
        namespace = runpy.run_path(str(generator))
        deps = {"kernel/source-pin.json", "VERSION"}
        for entry in namespace["COMPONENTS"]:
            base = ROOT / entry["path"]
            if base.is_file():
                deps.add(entry["path"])
            for relative in entry.get("hash_globs", []):
                candidate = base / relative
                if candidate.is_file():
                    deps.add(str(candidate.relative_to(ROOT)))
        # Include direct and loop-driven kernel hash dependencies of the real producer.
        for node in ast.walk(ast.parse(generator.read_text())):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and len(node.value) < 200 and "\n" not in node.value):
                for candidate in (ROOT / node.value, ROOT / "kernel/linux" / node.value):
                    if candidate.is_file():
                        deps.add(str(candidate.relative_to(ROOT)))
        for name in sorted(deps):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
        self.sbom_dependency_hashes = {
            name: hashlib.sha256((self.root / name).read_bytes()).hexdigest() for name in sorted(deps)
        }
        result = self.run_release()
        self.assertEqual(result.returncode, 0, result.stdout)
        sbom = json.loads((self.release / "docs/sbom.json").read_text())
        linux = next(c for c in sbom["components"] if c["name"] == "linux")
        self.assertEqual(linux["version"], "7.3.1")
        integration = next(c for c in sbom["components"] if c["name"] == "xtos-linux-integration")
        self.assertIn(self.sbom_dependency_hashes["kernel/patches/xtos-linux-7.3.1.patch"],
                      {item["content"] for item in integration.get("hashes", [])})
        print("SBOM_DEPENDENCY_HASHES=" + json.dumps(self.sbom_dependency_hashes, sort_keys=True), flush=True)


if __name__ == "__main__":
    unittest.main()
