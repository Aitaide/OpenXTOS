"""Run the real cross scripts in disposable trees; never cross-build or boot QEMU."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CrossScripts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cross-config-", dir=os.environ.get("TMPDIR", ROOT / ".build"))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.build = self.root / "out"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.root / "scripts").mkdir()
        (self.root / "kernel/linux/scripts/kconfig").mkdir(parents=True)
        (self.root / "system/rootfs").mkdir(parents=True)
        for name in ("build_system_arm64.sh", "build_system_riscv64.sh", "verify_system_arm64.sh", "verify_system_riscv64.sh", "verify_guest_arm64.sh", "verify_guest_riscv64.sh", "enable_rt_config.sh"):
            shutil.copy2(ROOT / "scripts" / name, self.root / "scripts" / name)
        for name in ("scripts/config", "scripts/kconfig/merge_config.sh"):
            shutil.copy2(ROOT / "kernel/linux" / name, self.root / "kernel/linux" / name)
        shutil.copy2(ROOT / "kernel/xtos_defconfig", self.root / "kernel/xtos_defconfig")
        shutil.copy2(ROOT / "system/rootfs/xtos-guest-check", self.root / "system/rootfs/xtos-guest-check")
        self.exe(self.root / "scripts/prepare_linux.sh", 'printf "prepare\\n" >> "$TRACE"\nprintf "%s\\n" "$KERNEL_DIR" > "$PREPARED_KERNEL"\nexit "${PREPARE_RC:-0}"\n')
        self.exe(self.bin / "make", '''printf 'make %s\\n' "$*" >> "$TRACE"
for arg in "$@"; do case "$arg" in O=*) out="${arg#O=}";; esac; done
case " $* " in
 *' defconfig '*) mkdir -p "$out"; printf '# CONFIG_PREEMPT_RT is not set\\nCONFIG_DRM=y\\n' > "$out/.config";;
 *' olddefconfig '*) if [ -n "$DROP" ]; then sed -i "/^${DROP}=/d" "$out/.config"; fi;;
 *' Image '*) exit 97;;
esac
''')
        for cc in ("aarch64-linux-gnu-gcc", "riscv64-linux-gnu-gcc"):
            self.exe(self.bin / cc, 'printf "fixture compiler\\n"\n')
        self.exe(self.bin / "sleep", "exit 0\n")
        self.exe(self.bin / "cpio", "cat >/dev/null\nprintf archive\n")
        for qemu in ("qemu-system-aarch64", "qemu-system-riscv64"):
            self.exe(self.bin / qemu, 'printf "qemu %s\\n" "$*" >> "$TRACE"\ncat "$QEMU_LOG"\nexit "${QEMU_RC:-0}"\n')
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"], BUILD_ROOT=str(self.build), TRACE=str(self.root / "trace"), PREPARED_KERNEL=str(self.root / "prepared-kernel"), DROP="", JOBS="1")

    def exe(self, path, body):
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    def run_script(self, name, **env):
        command = ["bash", str(self.root / "scripts" / name)]
        result = subprocess.run(command, env=dict(self.env, **env), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15)
        evidence = os.environ.get("CROSS_SCRIPT_EVIDENCE")
        if evidence:
            parent = Path(evidence) / self.id().split(".")[-1]
            parent.mkdir(parents=True, exist_ok=True)
            dest = parent / str(len(list(parent.iterdir())) + 1)
            shutil.copytree(self.root, dest)
            (dest / "process.json").write_text(json.dumps(dict(command=command, overrides=env, returncode=result.returncode), indent=2))
            (dest / "process.log").write_text(result.stdout)
        return result

    def cached_config(self, arch):
        path = self.build / ("kernel-build-" + arch) / ".config"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# CONFIG_PREEMPT_RT is not set\nCONFIG_DRM=y\n")
        return path

    def test_cached_config_is_reconciled_before_image(self):
        for arch in ("arm64", "riscv64"):
            with self.subTest(arch=arch):
                config = self.cached_config(arch)
                result = self.run_script("build_system_" + arch + ".sh")
                self.assertEqual(result.returncode, 97, result.stdout)
                trace = (self.root / "trace").read_text().splitlines()
                self.assertEqual(trace[0], "prepare", trace)
                self.assertIn("CONFIG_PREEMPT_RT=y", config.read_text())
                self.assertIn("CONFIG_SERIAL_DEV_CTRL_TTYPORT=y", config.read_text())
                self.assertIn("CONFIG_DRM=y", config.read_text())
                self.assertFalse(any(" defconfig" in line for line in trace), trace)
                self.assertTrue(any("olddefconfig" in line for line in trace), trace)
                (self.root / "trace").unlink()

    def test_cached_config_required_settings_fail_closed(self):
        for arch in ("arm64", "riscv64"):
            for symbol in ("CONFIG_PREEMPT_RT", "CONFIG_SERIAL_DEV_BUS", "CONFIG_SERIAL_DEV_CTRL_TTYPORT", "CONFIG_IKCONFIG", "CONFIG_IKCONFIG_PROC", "CONFIG_XTOS_SCHED", "CONFIG_XTOS_BCI", "CONFIG_XTOS_BCI_OPENBCI"):
                with self.subTest(arch=arch, symbol=symbol):
                    self.cached_config(arch)
                    result = self.run_script("build_system_" + arch + ".sh", DROP=symbol)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn("Required kernel setting missing after olddefconfig: " + symbol + "=", result.stdout)
                    self.assertNotIn("Building " + ("ARM64" if arch == "arm64" else "RISC-V 64") + " kernel image", result.stdout)

    def test_source_gate_validates_actual_fixed_kernel_root(self):
        for arch in ("arm64", "riscv64"):
            with self.subTest(arch=arch):
                self.cached_config(arch)
                result = self.run_script("build_system_" + arch + ".sh", KERNEL_DIR=str(self.root / "unrelated-kernel"))
                self.assertEqual(result.returncode, 97, result.stdout)
                self.assertEqual((self.root / "prepared-kernel").read_text().strip(), str(self.root / "kernel/linux"))

    def test_source_gate_refusal_precedes_make(self):
        for arch in ("arm64", "riscv64"):
            with self.subTest(arch=arch):
                self.cached_config(arch)
                result = self.run_script("build_system_" + arch + ".sh", PREPARE_RC="73")
                self.assertEqual(result.returncode, 73, result.stdout)
                self.assertEqual((self.root / "trace").read_text(), "prepare\n")
                (self.root / "trace").unlink()

    def test_fresh_config_defconfig_precedes_reconciliation(self):
        for arch in ("arm64", "riscv64"):
            with self.subTest(arch=arch):
                result = self.run_script("build_system_" + arch + ".sh")
                self.assertEqual(result.returncode, 97, result.stdout)
                trace = (self.root / "trace").read_text().splitlines()
                self.assertEqual(trace[0], "prepare")
                self.assertIn("defconfig", trace[1])
                self.assertIn("olddefconfig", trace[2])
                self.assertIn("Image dtbs modules", trace[3])
                (self.root / "trace").unlink()

    def test_qemu_failure_cannot_pass_with_success_markers(self):
        for arch in ("arm64", "riscv64"):
            for kind in ("guest", "system"):
                with self.subTest(arch=arch, kind=kind):
                    result = self.verify_log(kind, arch, self.guest_log(), QEMU_RC="42")
                    self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_mainline_without_rt_suffix_passes_both_verifiers(self):
        for arch in ("arm64", "riscv64"):
            for kind in ("guest", "system"):
                with self.subTest(arch=arch, kind=kind):
                    result = self.verify_log(kind, arch, self.guest_log().replace(" PREEMPT_RT", ""))
                    self.assertEqual(result.returncode, 0, result.stdout)

    def test_expected_release_missing_refuses_before_qemu(self):
        for arch in ("arm64", "riscv64"):
            with self.subTest(arch=arch):
                self.guest_artifacts(arch)
                (self.build / ("kernel-build-" + arch) / "include/config/kernel.release").unlink()
                result = self.run_script("verify_guest_" + arch + ".sh")
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("Expected kernel release missing", result.stdout)
                self.assertFalse((self.root / "trace").exists())

    def guest_artifacts(self, arch, release="7.3.1"):
        image = self.build / ("kernel-install-" + arch) / "Image"
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b"fixture image, not bootable")
        (self.build / ("rootfs-" + arch)).mkdir(exist_ok=True)
        (self.build / ("initramfs-" + arch + ".cpio.gz")).write_bytes(b"fixture archive")
        expected = self.build / ("kernel-build-" + arch) / "include/config/kernel.release"
        expected.parent.mkdir(parents=True, exist_ok=True)
        expected.write_text(release + "\n")

    def guest_log(self, **changes):
        fields = dict(kernel="7.3.1", realtime="1", preempt_rt="y", xtos_sched="y", xtos_modules="pass", module_xtos_bci_core="ok", module_xtos_bci_buffer="ok", module_openbci_cyton="ok", guest_test_rt="pass", guest_test_spsc="pass", guest_test_signal="pass", guest_userspace="pass")
        fields.update(changes)
        log = "Linux version 7.3.1 PREEMPT_RT\nBooting Linux on physical CPU 0\nOpenSBI\nRun /init as init process\nXTOS_GUEST_CHECK=START\n"
        log += "".join(key + "=" + value + "\n" for key, value in fields.items())
        return log + "XTOS_GUEST_CHECK=DONE\n"

    def verify_log(self, kind, arch, log, **env):
        self.guest_artifacts(arch)
        path = self.root / "serial.log"
        path.write_text(log)
        return self.run_script("verify_" + kind + "_" + arch + ".sh", QEMU_LOG=str(path), **env)

    def test_guest_release_and_rt_ground_truth(self):
        for arch in ("arm64", "riscv64"):
            for changes, accepted in (({}, True), ({"kernel": "7.3.1-wrong"}, False), ({"realtime": "0"}, False), ({"preempt_rt": "n"}, False), ({"realtime": "10"}, False)):
                with self.subTest(arch=arch, changes=changes):
                    result = self.verify_log("guest", arch, self.guest_log(**changes))
                    self.assertEqual(result.returncode == 0, accepted, result.stdout)

    def test_conflicting_review_probe_rejected(self):
        for arch in ('arm64', 'riscv64'):
            for kind in ('guest', 'system'):
                with self.subTest(arch=arch, kind=kind):
                    log = self.guest_log() + 'realtime=0\npreempt_rt=n\nguest_userspace=fail\n'
                    result = self.verify_log(kind, arch, log)
                    self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_guest_fields_require_unique_exact_values(self):
        valid = self.guest_log()
        fields = [line for line in valid.splitlines()
                  if '=' in line and not line.startswith('XTOS_GUEST_CHECK=')]
        for arch in ('arm64', 'riscv64'):
            for field in fields:
                key = field.split('=', 1)[0]
                for mutation, log in (
                        ('missing', valid.replace(field + '\n', '')),
                        ('duplicate', valid + field + '\n'),
                        ('conflict', valid + key + '=unexpected\n')):
                    with self.subTest(arch=arch, field=key, mutation=mutation):
                        result = self.verify_log('guest', arch, log)
                        self.assertNotEqual(result.returncode, 0, result.stdout)
            for marker in ('START', 'DONE'):
                field = 'XTOS_GUEST_CHECK=' + marker + '\n'
                for mutation, log in (('missing', valid.replace(field, '')),
                                      ('duplicate', valid + field)):
                    with self.subTest(arch=arch, marker=marker, mutation=mutation):
                        result = self.verify_log('guest', arch, log)
                        self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_guest_failure_diagnostics_rejected(self):
        for arch in ('arm64', 'riscv64'):
            for kind in ('guest', 'system'):
                for failure in ('module_extra=fail', 'guest_test_extra=fail',
                                'guest_test_extra=missing', 'Kernel panic - fixture',
                                '[ 1.0] BUG: fixture', '[ 1.0] WARNING: fixture',
                                'XTOS_GUEST_CHECK=unexpected'):
                    with self.subTest(arch=arch, kind=kind, failure=failure):
                        result = self.verify_log(kind, arch, self.guest_log() + failure + '\n')
                        self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_guest_crlf_positive_controls(self):
        for arch in ('arm64', 'riscv64'):
            for kind in ('guest', 'system'):
                with self.subTest(arch=arch, kind=kind):
                    result = self.verify_log(kind, arch, self.guest_log().replace('\n', '\r\n'))
                    self.assertEqual(result.returncode, 0, result.stdout)

    def test_basic_critical_failures_are_nonzero(self):
        for arch in ("arm64", "riscv64"):
            for log in (self.guest_log().replace("Run /init as init process\n", ""), self.guest_log(realtime="0"), self.guest_log() + "Kernel panic\n", self.guest_log(kernel="7.3.1-wrong")):
                with self.subTest(arch=arch, log=log):
                    result = self.verify_log("system", arch, log)
                    self.assertNotEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
