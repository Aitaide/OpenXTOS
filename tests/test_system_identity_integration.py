#!/usr/bin/env python3
"""Exercise identity-only builder fragments in isolated fixture build roots."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IDENTITY_FILES = ("os-release", "xtos-release", "xtos-build.json", "issue", "motd")
BUILDERS = {
    "x86": ("build_system.sh", "rootfs", "kernel-build", "x86_64", "core", "release"),
    "arm64": ("build_system_arm64.sh", "rootfs-arm64", "kernel-build-arm64", "arm64", "core", "KERNEL_RELEASE"),
    "riscv64": ("build_system_riscv64.sh", "rootfs-riscv64", "kernel-build-riscv64", "riscv64", "core", "KERNEL_RELEASE"),
    "baremetal": ("build_baremetal.sh", "rootfs", "kernel-build", "x86_64", "developer", "release"),
}


class SystemIdentityIntegrationTests(unittest.TestCase):
    def identity_fragment(self, builder):
        filename, _, _, arch, image_type, release_var = BUILDERS[builder]
        text = (ROOT / "scripts" / filename).read_text()
        call = re.search(
            r'^python3 "\$ROOT/scripts/gen_system_identity\.py"[^\n]*$',
            text,
            re.MULTILINE,
        )
        if call is None:
            self.fail(f"{filename} must invoke the host identity generator")
        start = call.start()
        lines = text[start:].splitlines(keepends=True)
        count = 1
        while lines[count - 1].rstrip().endswith("\\"):
            count += 1
        invocation = "".join(lines[:count])
        output_dir = "$B/rootfs/etc" if builder == "baremetal" else "$ROOTFS/etc"
        for argument in (
            f'--output-dir "{output_dir}"',
            f'--image-type "${{XTOS_IMAGE_TYPE:-{image_type}}}"',
            f"--arch {arch}",
            f'--kernel-release "${release_var}"',
            '--build-id "${XTOS_BUILD_ID:-unknown}"',
            '--source-state "${XTOS_SOURCE_STATE:-unknown}"',
            '--hardware-profile "${XTOS_HARDWARE_PROFILE:-unknown}"',
        ):
            self.assertIn(argument, invocation, filename)
        assignments = re.findall(rf'^{release_var}=.*$', text[:start], re.MULTILINE)
        self.assertTrue(assignments, f"{filename} must resolve kernel release before generation")
        assignment = assignments[-1]
        if builder == "x86":
            self.assertIn('O="$KERNEL_OUTPUT" ARCH=x86 kernelrelease', assignment)
        else:
            kernel_path = "$B/kernel-build" if builder == "baremetal" else "$KERNEL_BUILD"
            self.assertIn(f'{kernel_path}/include/config/kernel.release', assignment)
        self.assertNotRegex(invocation, r"\beval\b")
        return text, start, assignment + "\n" + invocation

    def exercise_fixture(self, builder, fragment):
        _, rootfs_name, kernel_name, arch, image_type, _ = BUILDERS[builder]
        # Use Hermes scratch if configured; never write to the repository's .build.
        with tempfile.TemporaryDirectory(prefix="xtos identity ", dir=os.environ.get("TMPDIR")) as tmp:
            fixture = Path(tmp)
            source = fixture / "source root"
            (source / "scripts").mkdir(parents=True)
            shutil.copy2(ROOT / "scripts/gen_system_identity.py", source / "scripts")
            shutil.copy2(ROOT / "VERSION", source / "VERSION")
            kernel_source = source / "kernel/linux"
            kernel_source.mkdir(parents=True)
            # A real, tiny make target supplies x86's kernelrelease without a kernel build.
            (kernel_source / "Makefile").write_text(
                'kernelrelease:\n\t@cat "$(O)/include/config/kernel.release"\n'
            )
            build = fixture / "build root"
            kernel = build / kernel_name
            (kernel / "include/config").mkdir(parents=True)
            (kernel / "include/config/kernel.release").write_text("6.6.0-rt15\n")
            rootfs = build / rootfs_name
            etc = rootfs / "etc"
            etc.mkdir(parents=True)
            inittab = b"::respawn:/bin/sh\n"
            (etc / "inittab").write_bytes(inittab)
            shell = (
                'set -euo pipefail\nROOT="$1"\nBUILD_ROOT="$2"\nB="$2"\n'
                'ROOTFS="$3"\nKERNEL_DIR="$4"\nKERNEL_OUTPUT="$5"\nKERNEL_BUILD="$5"\n'
                + fragment
            )
            env = os.environ.copy()
            for key in ("XTOS_IMAGE_TYPE", "XTOS_BUILD_ID", "XTOS_SOURCE_STATE", "XTOS_HARDWARE_PROFILE"):
                env.pop(key, None)
            runs = (
                (False, {}, {"image_type": image_type, "build_id": "unknown", "source_state": "unknown", "hardware_profile": "unknown"}),
                (True, {
                    "XTOS_IMAGE_TYPE": "developer",
                    "XTOS_BUILD_ID": 'fixture id; $(touch injected) `touch injected` "quoted" $value \\path',
                    "XTOS_SOURCE_STATE": "dirty",
                    "XTOS_HARDWARE_PROFILE": "fixture hardware; $(touch injected)",
                }, None),
            )
            for reused, overrides, expected in runs:
                with self.subTest(builder=builder, reused=reused):
                    if reused:
                        (kernel / ".config").write_text("CONFIG_FIXTURE=y\n")
                        for name in IDENTITY_FILES:
                            (etc / name).write_text("stale identity\n")
                    current_env = dict(env, **overrides)
                    if expected is None:
                        expected = {
                            "image_type": overrides["XTOS_IMAGE_TYPE"],
                            "build_id": overrides["XTOS_BUILD_ID"],
                            "source_state": overrides["XTOS_SOURCE_STATE"],
                            "hardware_profile": overrides["XTOS_HARDWARE_PROFILE"],
                        }
                    command = ["bash", "-c", shell, "identity-fixture", str(source), str(build), str(rootfs), str(kernel_source), str(kernel)]
                    result = subprocess.run(command, cwd=fixture, env=current_env, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    metadata = json.loads((etc / "xtos-build.json").read_text())
                    version_text = (ROOT / "VERSION").read_text()
                    version = next(
                        line.split("=", 1)[1].strip()
                        for line in version_text.splitlines()
                        if line.startswith("XTOS_VERSION=")
                    )
                    for key, value in dict(
                        expected,
                        architecture=arch,
                        kernel_release="6.6.0-rt15",
                        version=version,
                        product_label="XTOS Bate版本",
                    ).items():
                        self.assertEqual(metadata[key], value, key)
                    self.assertIn(
                        f'PRETTY_NAME="XTOS Bate版本 {version}"',
                        (etc / "os-release").read_text(),
                    )
                    self.assertEqual((etc / "inittab").read_bytes(), inittab)
                    self.assertFalse((fixture / "injected").exists(), "metadata must remain literal shell arguments")
                    before = {name: (etc / name).read_bytes() for name in IDENTITY_FILES}
                    repeat = subprocess.run(command, cwd=fixture, env=current_env, capture_output=True, text=True)
                    self.assertEqual(repeat.returncode, 0, repeat.stderr)
                    self.assertEqual(before, {name: (etc / name).read_bytes() for name in IDENTITY_FILES})
                    for key, invalid in (("XTOS_IMAGE_TYPE", "../escape"), ("XTOS_SOURCE_STATE", "invented")):
                        rejected = subprocess.run(command, cwd=fixture, env=dict(current_env, **{key: invalid}), capture_output=True, text=True)
                        self.assertNotEqual(rejected.returncode, 0, rejected.stderr)
                        self.assertEqual(before, {name: (etc / name).read_bytes() for name in IDENTITY_FILES})

    def test_x86_stages_identity_before_rootfs_packing(self):
        text, start, fragment = self.identity_fragment("x86")
        self.assertLess(text.index('cp -a "$ROOT/system/rootfs/." "$ROOTFS/"'), start)
        self.assertLess(text.index('mkdir -p "$ROOTFS"/{bin,sbin,dev,etc,'), start)
        self.assertLess(start, text.index('cd "$ROOTFS"'))
        self.assertNotIn('cp -a "$ROOT/system/rootfs/."', text[start:])
        self.exercise_fixture("x86", fragment)

    def check_cross_identity(self, builder):
        text, start, fragment = self.identity_fragment(builder)
        _, rootfs_name, _, _, _, _ = BUILDERS[builder]
        self.assertIn(f'ROOTFS="$BUILD_ROOT/{rootfs_name}"', text)
        directories = text.index('mkdir -p "$ROOTFS"/{dev,proc,sys,tmp,root,etc,mnt}')
        inittab = text.index('# Create /etc/inittab')
        self.assertLess(directories, start)
        self.assertLess(start, inittab)
        # This top-level staging block runs whether kernel/BusyBox configs exist or not.
        self.assertNotRegex(text[directories:inittab], r"(?m)^\s*(?:if|elif|else|fi)\b")
        self.assertLess(start, text.index('INITRAMFS="$BUILD_ROOT/'))
        self.exercise_fixture(builder, fragment)

    def test_arm64_stages_identity_for_clean_and_reused_rootfs(self):
        self.check_cross_identity("arm64")

    def test_riscv64_stages_identity_for_clean_and_reused_rootfs(self):
        self.check_cross_identity("riscv64")

    def test_baremetal_preserves_identity_and_private_manifest_before_packing(self):
        text, start, fragment = self.identity_fragment("baremetal")
        self.assertIn('XTOS_IMAGE_TYPE="${XTOS_IMAGE_TYPE:-developer}" "$B/driver/build_system.sh"', text)
        self.assertLess(text.index("else: copy(src)"), start)
        self.assertLess(start, text.index("files=subprocess.check_output"))
        self.assertLess(start, text.index('pack "$B/rootfs" "$B/images/boot.cpio.gz"'))
        self.assertLess(start, text.index('cp -a "$B/rootfs/." "$B/installer/"'))
        self.assertLess(start, text.index('tar -C "$B/rootfs" -czf "$B/installer/payload/rootfs.tar.gz" .'))
        self.assertNotIn("(root/'etc/xtos-build.json').write_text", text)
        self.assertIn("(root/'etc/xtos-baremetal-build.json').write_text(json.dumps(manifest,indent=2)+'\\n')", text)
        self.assertIn("(b/'images/build-manifest.json').write_text(json.dumps(manifest,indent=2)+'\\n')", text)
        self.exercise_fixture("baremetal", fragment)


if __name__ == "__main__":
    unittest.main()
