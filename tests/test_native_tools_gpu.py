#!/usr/bin/env python3
"""Read-only hw gpu regressions; fixtures never write host /sys or /dev."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tools/xtos-native/hw/hw.c"
COMMON = ROOT / "tools/xtos-native/common"

IOCTL_SHIM = r'''
#include <stdarg.h>
#include <stddef.h>
#include <string.h>
#include <dirent.h>
#include <errno.h>
#include <unistd.h>
#include "xtos_gpu.h"

static int fault_enabled(const char *marker) {
    int saved_errno = errno;
    int enabled = access(marker, F_OK) == 0;
    errno = saved_errno;
    return enabled;
}

static DIR *drm_dir;
static size_t drm_nodes_seen;
static DIR *msi_dir;
static size_t msi_nodes_seen;
static int irq_collection;

DIR *__real_opendir(const char *path);
DIR *__wrap_opendir(const char *path) {
    if (strstr(path, "/msi_irqs") != NULL) irq_collection = 1;
    if (strcmp(path, "sys/class/drm") == 0 && fault_enabled("fault-opendir")) {
        errno = EIO;
        return NULL;
    }
    DIR *dir = __real_opendir(path);
    if (strcmp(path, "sys/class/drm") == 0) drm_dir = dir;
    if (strstr(path, "/msi_irqs") != NULL) msi_dir = dir;
    return dir;
}

struct dirent *__real_readdir(DIR *dir);
struct dirent *__wrap_readdir(DIR *dir) {
    if (dir == msi_dir && (fault_enabled("fault-msi-readdir")
            || (msi_nodes_seen != 0 && fault_enabled("fault-msi-readdir-later")))) {
        errno = EIO;
        return NULL;
    }
    if (dir == drm_dir && (fault_enabled("fault-readdir")
            || (drm_nodes_seen != 0 && fault_enabled("fault-readdir-later")))) {
        errno = EIO;
        return NULL;
    }
    struct dirent *entry = __real_readdir(dir);
    if (dir == msi_dir && entry != NULL && entry->d_name[0] != '.') ++msi_nodes_seen;
    if (dir == drm_dir && entry != NULL
            && (strncmp(entry->d_name, "card", 4) == 0
                || strncmp(entry->d_name, "renderD", 7) == 0)) {
        ++drm_nodes_seen;
    }
    return entry;
}

void *__real_realloc(void *ptr, size_t size);
void *__wrap_realloc(void *ptr, size_t size) {
    static size_t calls;
    static size_t irq_calls;
    ++calls;
    if (irq_collection) ++irq_calls;
    if (fault_enabled("fault-realloc")
            || (calls == 2 && fault_enabled("fault-realloc-later"))
            || (irq_collection && fault_enabled("fault-irq-realloc"))
            || (irq_calls == 2 && fault_enabled("fault-irq-realloc-later"))) {
        errno = ENOMEM;
        return NULL;
    }
    return __real_realloc(ptr, size);
}

char *__real_strdup(const char *text);
char *__wrap_strdup(const char *text) {
    static size_t calls;
    ++calls;
    if (fault_enabled("fault-strdup")
            || (calls == 2 && fault_enabled("fault-strdup-later"))) {
        errno = ENOMEM;
        return NULL;
    }
    return __real_strdup(text);
}

static const char fake_name[] = "driver-"
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    "-quote-\"-slash-\\-end";

int __real_ioctl(int fd, unsigned long request, ...);
int __wrap_ioctl(int fd, unsigned long request, ...) {
    va_list ap;
    va_start(ap, request);
    void *arg = va_arg(ap, void *);
    va_end(ap);
    if (request == DRM_IOCTL_VERSION) {
        struct xtos_drm_version *version = arg;
        size_t actual = sizeof(fake_name) - 1;
        if (version->name != NULL && version->name_len != 0) {
            size_t n = version->name_len < actual ? version->name_len : actual;
            memcpy(version->name, fake_name, n);
        }
        version->name_len = actual;
        version->version_major = 1;
        version->version_minor = 2;
        version->version_patchlevel = 3;
        return 0;
    }
    return __real_ioctl(fd, request, arg);
}
'''


class GpuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="xtos-gpu-test-")
        cls.root = Path(cls.temp.name)
        cls.root.chmod(0o755)
        cls.binary = cls.root / "hw"
        shim = cls.root / "ioctl_shim.c"
        shim.write_text(IOCTL_SHIM)
        subprocess.run([
            "gcc", "-O2", "-Wall", "-Wextra", "-Werror", "-static",
            "-I", str(COMMON),
            "-I", str(ROOT / "tools/xtos-native/hw"),
            '-DXTOS_SYS_CLASS_DRM="sys/class/drm"',
            '-DXTOS_DEV_DRI="dev/dri"',
            '-DXTOS_PROC_IRQ="proc/irq"',
            "-Wl,--wrap=ioctl,--wrap=opendir,--wrap=readdir,--wrap=realloc,--wrap=strdup",
            "-o", str(cls.binary), str(SOURCE), str(shim),
        ], check=True, capture_output=True, text=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        for marker in self.root.glob("fault-*"):
            marker.unlink()
        for dirname in ("sys", "dev", "proc"):
            path = self.root / dirname
            if path.exists():
                shutil.rmtree(path)
        self.drm = self.root / "sys/class/drm"
        self.dri = self.root / "dev/dri"
        self.irq = self.root / "proc/irq"
        for path in (self.drm, self.dri, self.irq):
            path.mkdir(parents=True)

    def add_node(self, name, character=False):
        (self.drm / name).mkdir()
        if character:
            (self.dri / name).symlink_to("/dev/null")
        else:
            (self.dri / name).write_text("fixture")

    def run_hw(self, *args, drop=False):
        preexec = None
        if drop and os.geteuid() == 0:
            def drop_privileges():
                os.setgroups([])
                os.setgid(65534)
                os.setuid(65534)
            preexec = drop_privileges
        return subprocess.run(["./hw", *args], cwd=self.root,
                              capture_output=True, text=True, timeout=20,
                              preexec_fn=preexec)

    def test_no_device_is_successful_empty_json_and_not_present(self):
        result = self.run_hw("--json", "gpu")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [])
        result = self.run_hw("gpu")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not present", result.stdout.lower())
        self.assertNotIn("\x1b[", result.stdout)

    def test_only_card_and_render_nodes_are_enumerated(self):
        self.add_node("card0")
        self.add_node("renderD128")
        for name in ("card0-HDMI-A-1", "card0-DP-1", "controlD64", "renderD128-DP-1"):
            (self.drm / name).mkdir()
        result = self.run_hw("--json", "gpu")
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(result.stdout)
        self.assertEqual({row["node"] for row in rows},
                         {"/dev/dri/card0", "/dev/dri/renderD128"})

    def test_missing_drm_directory_is_successful_absence(self):
        self.drm.rmdir()
        result = self.run_hw("--json", "gpu")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [])
        result = self.run_hw("gpu")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not present", result.stdout.lower())

    def assert_gpu_failure(self, result, error):
        self.assertEqual(result.returncode, 1, (result.stdout, result.stderr))
        self.assertIn(f"errno={error}", result.stderr)
        self.assertIn(os.strerror(error), result.stderr)
        self.assertNotIn("not present", result.stdout.lower())
        self.assertNotIn("diagnostics complete", result.stdout.lower())

    def test_drm_opendir_errors_are_not_reported_as_absence(self):
        for fault, error in (("permission", 13), ("io", 5)):
            with self.subTest(fault=fault):
                if fault == "permission":
                    self.drm.chmod(0)
                else:
                    (self.root / "fault-opendir").touch()
                try:
                    for args in (("--json", "gpu"), ("gpu",)):
                        result = self.run_hw(*args, drop=True)
                        self.assert_gpu_failure(result, error)
                        self.assertEqual(result.stdout, "")
                finally:
                    self.drm.chmod(0o755)

    def test_regular_file_is_not_character_device_and_reports_eacces(self):
        self.add_node("card7")
        (self.dri / "card7").chmod(0)
        result = self.run_hw("--json", "gpu", drop=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        row = json.loads(result.stdout)[0]
        self.assertEqual(row["node"], "/dev/dri/card7")
        self.assertFalse(row["character_device"])
        self.assertFalse(row["openable"])
        self.assertEqual(row["open_errno"], 13)
        self.assertEqual(row["drm_driver"], "unknown")

    def test_node_name_allocation_failure_aborts_enumeration(self):
        self.add_node("card0")
        self.add_node("card1")
        for fault in ("realloc", "realloc-later", "strdup", "strdup-later"):
            with self.subTest(fault=fault):
                marker = self.root / f"fault-{fault}"
                marker.touch()
                try:
                    for args in (("--json", "gpu"), ("gpu",)):
                        result = self.run_hw(*args, drop=True)
                        self.assert_gpu_failure(result, 12)
                        self.assertEqual(result.stdout, "")
                finally:
                    marker.unlink()

    def test_readdir_failure_aborts_instead_of_reporting_partial_nodes(self):
        self.add_node("card0")
        self.add_node("card1")
        for fault in ("readdir", "readdir-later"):
            with self.subTest(fault=fault):
                marker = self.root / f"fault-{fault}"
                marker.touch()
                try:
                    for args in (("--json", "gpu"), ("gpu",)):
                        result = self.run_hw(*args, drop=True)
                        self.assert_gpu_failure(result, 5)
                        self.assertEqual(result.stdout, "")
                finally:
                    marker.unlink()

    def test_irq_allocation_failure_cannot_report_diagnostics_complete(self):
        self.add_node("card0")
        device = self.drm / "card0/device"
        device.mkdir()
        (device / "irq").write_text("43\n")
        msi = device / "msi_irqs"
        msi.mkdir()
        (msi / "41").touch()
        (msi / "42").touch()
        for source, fault in (("msi-first", "irq-realloc"),
                              ("msi-later", "irq-realloc-later"),
                              ("legacy", "irq-realloc")):
            with self.subTest(source=source):
                if source == "legacy":
                    shutil.rmtree(msi)
                marker = self.root / f"fault-{fault}"
                marker.touch()
                try:
                    for args in (("--json", "gpu"), ("gpu",)):
                        result = self.run_hw(*args, drop=True)
                        self.assert_gpu_failure(result, 12)
                        self.assertIn("IRQ", result.stderr)
                finally:
                    marker.unlink()

    def test_msi_directory_read_error_cannot_report_partial_irq_success(self):
        self.add_node("card0")
        msi=self.drm/"card0/device/msi_irqs"
        msi.mkdir(parents=True)
        (msi/"41").touch()
        (msi/"42").touch()
        for fault in ("msi-readdir","msi-readdir-later"):
            marker=self.root/f"fault-{fault}"
            marker.touch()
            try:
                for args in (("--json","gpu"),("gpu",)):
                    with self.subTest(fault=fault,args=args):
                        self.assert_gpu_failure(self.run_hw(*args,drop=True),5)
            finally:
                marker.unlink()

    def test_drm_driver_name_is_read_via_ioctl_and_json_escaped(self):
        self.add_node("renderD128", character=True)
        result = self.run_hw("--json", "gpu", drop=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        row = json.loads(result.stdout)[0]
        expected = (
            "driver-abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
            "-quote-\"-slash-\\-end"
        )
        self.assertTrue(row["character_device"])
        self.assertTrue(row["openable"])
        self.assertIsNone(row["open_errno"])
        self.assertEqual(row["drm_driver"], expected)
        self.assertGreater(len(row["drm_driver"]), 256)

    def test_pci_metadata_and_requested_effective_irq_affinity(self):
        self.add_node("card0", character=True)
        pci = self.root / "sys/devices/pci0000:00/0000:00:02.0"
        pci.mkdir(parents=True)
        (pci / "vendor").write_text('0x"8086\\gpu\n')
        (pci / "device").write_text("0x1234\n")
        (pci / "irq").write_text("41\n")
        (pci / "msi_irqs").mkdir()
        (pci / "msi_irqs/42").touch()
        (self.drm / "card0/device").symlink_to(pci)
        irq41 = self.irq / "41"
        irq41.mkdir()
        (irq41 / "smp_affinity_list").write_text("0-3\n")
        (irq41 / "effective_affinity_list").write_text("2\n")
        irq42 = self.irq / "42"
        irq42.mkdir()
        (irq42 / "smp_affinity_list").write_text("1\n")
        result = self.run_hw("--json", "gpu")
        self.assertEqual(result.returncode, 0, result.stderr)
        row = json.loads(result.stdout)[0]
        self.assertEqual(row["pci"], {
            "address": "0000:00:02.0",
            "vendor": '0x"8086\\gpu',
            "device": "0x1234",
        })
        self.assertEqual(row["irqs"], [
            {"irq": 41, "requested_affinity": "0-3", "effective_affinity": "2"},
            {"irq": 42, "requested_affinity": "1", "effective_affinity": "unknown"},
        ])

    def test_hexadecimal_irq_fallback_is_not_presented_as_a_cpu_list(self):
        self.add_node("card0")
        device=self.drm/"card0/device"
        device.mkdir()
        (device/"irq").write_text("41\n")
        irq=self.irq/"41"
        irq.mkdir()
        (irq/"smp_affinity").write_text("00000001\n")
        (irq/"effective_affinity").write_text("1\n")
        result=self.run_hw("--json","gpu")
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)[0]["irqs"],[{
            "irq":41,"requested_affinity":"mask:00000001","effective_affinity":"mask:1"}])


if __name__ == "__main__":
    if sys.platform != "linux" or shutil.which("gcc") is None:
        print("skip: Linux and gcc required")
        sys.exit(0)
    unittest.main(verbosity=2)
