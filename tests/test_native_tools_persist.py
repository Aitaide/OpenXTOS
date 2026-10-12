#!/usr/bin/env python3
"""Static production build plus isolated persist fixtures; never mount/format host devices.

The C harness includes the real tool, substitutes block metadata for regular
fixture files, and intercepts privileged syscalls. A fixed executable stub
records mkfs argv and writes only a fixture superblock. All proc/sys checks and
marker I/O use the production implementation. There are no loop devices/skips.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "tools" / "xtos-native" / "persist" / "persist.c"
FLAGS = ["-static", "-O2", "-Wall", "-Wextra", "-Werror"]

HARNESS = r'''
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/fs.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <sys/wait.h>
#include <unistd.h>
#include <limits.h>
static int fault(const char *name) {
    const char *s = getenv("PERSIST_FAULT");
    return s && strcmp(s, name) == 0;
}
static void trace(const char *message) {
    FILE *f = fopen(FIXTURE_ROOT "/trace", "a");
    if (!f) abort();
    fprintf(f, "%s\n", message);
    if (fclose(f)) abort();
}
static int fdpath(int fd, char *path) {
    char proc[80];
    snprintf(proc, sizeof(proc), "/proc/self/fd/%d", fd);
    ssize_t n = readlink(proc, path, PATH_MAX - 1);
    if (n < 0) return -1;
    path[n] = '\0';
    return 0;
}
static void block_stat(const char *path, struct stat *st) {
    const char prefix[] = XTOS_DEV_ROOT "/";
    if (strncmp(path, prefix, sizeof(prefix) - 1) != 0 || fault("regular")) return;
    const char *name = path + sizeof(prefix) - 1;
    char devfile[PATH_MAX];
    int n = snprintf(devfile, sizeof(devfile), XTOS_SYS_ROOT "/class/block/%s/dev", name);
    if (n < 0 || n >= PATH_MAX) abort();
    FILE *f = fopen(devfile, "r");
    unsigned int ma, mi;
    if (!f) return;
    int rc = fscanf(f, "%u:%u", &ma, &mi);
    fclose(f);
    if (rc != 2) return;
    st->st_mode = (st->st_mode & 07777) | S_IFBLK;
    st->st_rdev = makedev(ma, mi);
}
static int mock_stat(const char *path, struct stat *st) {
    int rc = stat(path, st);
    if (!rc) {
        char resolved[PATH_MAX];
        if (realpath(path, resolved)) block_stat(resolved, st);
        if (fault("swapfile") && strstr(path, "/swapfile")) st->st_dev = makedev(8, 1);
        if (fault("identity-changed")) st->st_rdev = makedev(99, 99);
    }
    return rc;
}
static int mock_fstat(int fd, struct stat *st) {
    int rc = fstat(fd, st);
    char path[PATH_MAX];
    if (!rc && fdpath(fd, path) == 0) block_stat(path, st);
    return rc;
}
static int mock_ioctl(int fd, unsigned long request, ...) {
    (void)fd;
    va_list ap;
    va_start(ap, request);
    void *arg = va_arg(ap, void *);
    va_end(ap);
    if (fault("ioctl")) { errno = EIO; return -1; }
    if (request == BLKGETSIZE64) *(uint64_t *)arg = fault("empty-device") ? 0 : 64 * 1024 * 1024;
    else if (request == BLKROGET) *(int *)arg = fault("readonly");
    else { errno = EINVAL; return -1; }
    return 0;
}
static uid_t mock_geteuid(void) { return fault("nonroot") ? 1000 : 0; }
static int mock_flock(int fd, int operation) {
    (void)fd;
    (void)operation;
    if (fault("locked")) { errno = EWOULDBLOCK; return -1; }
    return 0;
}
static int mock_open(const char *path, int flags, ...) {
    if ((flags & O_EXCL) && fault("busy")) { errno = EBUSY; return -1; }
    /* Production open() uses no O_CREAT here. */
    return open(path, flags);
}
static pid_t mock_fork(void) {
    trace("fork");
    if (fault("fork")) { errno = EAGAIN; return -1; }
    return fork();
}
static int mock_execve(const char *path, char *const args[], char *const env[]) {
    if (fault("exec")) { errno = ENOENT; return -1; }
    return execve(path, args, env);
}
static pid_t mock_waitpid(pid_t pid, int *status, int options) {
    static int interrupted;
    if (fault("wait-eintr") && !interrupted++) { errno = EINTR; return -1; }
    pid_t result = waitpid(pid, status, options);
    if (fault("wait")) { errno = ECHILD; return -1; }
    if (fault("mkfs-failed")) *status = 7 << 8;
    if (fault("mkfs-signal")) *status = SIGTERM;
    if (fault("mounted-after-mkfs")) {
        FILE *f = fopen(XTOS_PROC_ROOT "/self/mountinfo", "a");
        if (!f) abort();
        fputs("9 1 8:1 / /race rw - ext4 /dev/alias rw\n", f);
        fclose(f);
    }
    return result;
}
static int mock_mount(const char *source, const char *target, const char *type,
                      unsigned long flags, const void *data) {
    (void)data;
    struct stat st;
    if (stat(target, &st) || !S_ISDIR(st.st_mode) || (st.st_mode & 0777) != 0700 ||
        strcmp(type, "ext4") || flags != (MS_NODEV | MS_NOSUID | MS_NOEXEC)) abort();
    char message[PATH_MAX + 80];
    snprintf(message, sizeof(message), "mount ext4 private=0700 device=%s", source);
    trace(message);
    if (fault("mount")) { errno = EPERM; return -1; }
    char empty[PATH_MAX];
    int n = snprintf(empty, sizeof(empty), "%s.empty", target);
    if (n < 0 || n >= PATH_MAX) abort();
    if (rename(target, empty) || rename(FIXTURE_ROOT "/volume", target)) abort();
    return 0;
}
static int mock_umount2(const char *target, int flags) {
    if (flags) abort();
    trace("umount");
    if (fault("umount")) { errno = EBUSY; return -1; }
    static int interrupted;
    if (fault("umount-eintr") && !interrupted++) { errno = EINTR; return -1; }
    char empty[PATH_MAX];
    int n = snprintf(empty, sizeof(empty), "%s.empty", target);
    if (n < 0 || n >= PATH_MAX) abort();
    if (rename(target, FIXTURE_ROOT "/volume") || rename(empty, target)) abort();
    return 0;
}
static int mock_fsync(int fd) {
    char path[PATH_MAX];
    if (fdpath(fd, path)) abort();
    int marker = strstr(path, "/.XTOS_PERSIST_ID.tmp.") != NULL;
    trace(marker ? "fsync-marker" : "fsync-dir");
    if ((marker && fault("fsync-marker")) || (!marker && fault("fsync-dir"))) {
        errno = EIO;
        return -1;
    }
    return fsync(fd);
}
static int mock_syncfs(int fd) {
    trace("syncfs");
    if (fault("syncfs")) { errno = EIO; return -1; }
    /* No real syncfs: don't flush any host filesystem. */
    (void)fd;
    return 0;
}
static ssize_t mock_write(int fd, const void *buffer, size_t length) {
    static int interrupted;
    if (fault("write-eintr") && !interrupted++) { errno = EINTR; return -1; }
    if (fault("write")) { errno = ENOSPC; return -1; }
    if (fault("write-short") && length > 1) length = 1;
    return write(fd, buffer, length);
}
static int mock_close(int fd) {
    char path[PATH_MAX];
    int marker = fdpath(fd, path) == 0 && strstr(path, "/.XTOS_PERSIST_ID.tmp.") != NULL;
    int rc = close(fd);
    if (marker && fault("close-marker")) { errno = EIO; return -1; }
    return rc;
}
#define stat(path, st) mock_stat(path, st)
#define fstat mock_fstat
#define ioctl mock_ioctl
#define geteuid mock_geteuid
#define flock mock_flock
#define open mock_open
#define fork mock_fork
#define execve mock_execve
#define waitpid mock_waitpid
#define mount mock_mount
#define umount2 mock_umount2
#define fsync mock_fsync
#define syncfs mock_syncfs
#define write mock_write
#define close mock_close
#include SOURCE_PATH
'''


def mount_line(dev="0:1", root="/", point="/", source="overlay", filesystem="overlay", number=1):
    return f"{number} 0 {dev} {root} {point} rw - {filesystem} {source} rw\n"


def superblock(label="XTOS-PERSIST", ext4=True):
    data = bytearray(4096)
    data[1024 + 0x38:1024 + 0x3A] = b"\x53\xef"
    if ext4:
        data[1024 + 0x60] = 0x40
    data[1024 + 0x78:1024 + 0x88] = label.encode().ljust(16, b"\0")
    return data


class PersistTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="xtos-persist-tests-")
        cls.base = Path(cls.temp.name)
        cls.production = cls.base / "persist-production"
        subprocess.run(["gcc", *FLAGS, "-o", str(cls.production), str(SRC)], check=True)
        cls.fixture = cls.base / "fixture"
        cls.fixture.mkdir()
        cls.binary = cls.base / "persist-fixture"
        cls.mkfs = cls.base / "mkfs-stub"
        cls.mkfs.write_text(f"#!{sys.executable}\n" + "import json, sys\n" +
                            f"from pathlib import Path\nr = Path({str(cls.fixture)!r})\n" +
                            "with (r / 'trace').open('a') as f: f.write('mkfs ' + json.dumps(sys.argv[1:]) + '\\n')\n" +
                            "assert sys.argv[1:4] == ['-F', '-L', 'XTOS-PERSIST']\n" +
                            "target = Path(sys.argv[4])\nassert target.parent == r / 'dev'\n" +
                            f"target.write_bytes({bytes(superblock())!r})\n")
        cls.mkfs.chmod(0o700)
        defines = {
            "FIXTURE_ROOT": cls.fixture,
            "XTOS_PROC_ROOT": cls.fixture / "proc",
            "XTOS_SYS_ROOT": cls.fixture / "sys",
            "XTOS_DEV_ROOT": cls.fixture / "dev",
            "XTOS_TMP_ROOT": cls.fixture / "run",
            "XTOS_MKFS_PATH": cls.mkfs,
            "SOURCE_PATH": SRC,
        }
        harness = cls.base / "harness.c"
        harness.write_text("\n".join(f"#define {k} {json.dumps(str(v))}" for k, v in defines.items()) + "\n" + HARNESS)
        subprocess.run(["gcc", *FLAGS, "-o", str(cls.binary), str(harness)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        shutil.rmtree(self.fixture)
        for name in ("dev", "sys/class/block", "sys/dev/block", "proc/self", "run", "volume"):
            (self.fixture / name).mkdir(parents=True, exist_ok=True)
        self.nodes = {}
        self.block("disk", "8:0", "devices/pci/block/disk")
        self.block("disk1", "8:1", "devices/pci/block/disk/disk1", partition=True)
        self.block("disk2", "8:2", "devices/pci/block/disk/disk2", partition=True)
        self.block("diskchild", "8:3", "devices/pci/block/disk/disk1/diskchild", partition=True)
        self.device = self.fixture / "dev/disk1"
        self.volume = self.fixture / "volume"
        self.mounts = self.fixture / "proc/self/mountinfo"
        self.mounts.write_text(mount_line())
        (self.fixture / "proc/swaps").write_text("Filename\t\tType\tSize\tUsed\tPriority\n")

    def block(self, name, dev, node, partition=False, label="XTOS-PERSIST", ext4=True):
        location = self.fixture / "sys" / node
        location.mkdir(parents=True, exist_ok=True)
        (location / "dev").write_text(dev + "\n")
        (location / "holders").mkdir()
        if partition:
            (location / "partition").write_text("1\n")
        (self.fixture / "sys/class/block" / name).symlink_to(location)
        (self.fixture / "sys/dev/block" / dev).symlink_to(location)
        (self.fixture / "dev" / name).write_bytes(superblock(label, ext4))
        self.nodes[name] = location

    def run_tool(self, *args, fault=None, production=False):
        env = dict(os.environ)
        env.pop("PERSIST_FAULT", None)
        if fault:
            env["PERSIST_FAULT"] = fault
        return subprocess.run([str(self.production if production else self.binary), *map(str, args)],
                              capture_output=True, text=True, timeout=15, env=env)

    def trace(self):
        path = self.fixture / "trace"
        return path.read_text().splitlines() if path.exists() else []

    def refuse(self, *args, message, fault=None, code=1):
        before = self.device.read_bytes()
        r = self.run_tool(*args, fault=fault)
        self.assertEqual(r.returncode, code, r.stdout + r.stderr)
        self.assertIn(message, r.stderr)
        self.assertNotIn("PERSIST_SETUP", r.stdout)
        self.assertEqual(self.device.read_bytes(), before)
        self.assertFalse(any(s.startswith(("fork", "mkfs", "mount")) for s in self.trace()), self.trace())
        return r

    def test_production_non_destructive_checks(self):
        for args in (("setup", "/etc/passwd", "--confirm"),
                     ("marker", "/dev/../etc/passwd", "--confirm"),
                     ("setup", "/dev/null", "--confirm")):
            with self.subTest(args=args):
                r = self.run_tool(*args, production=True)
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertNotIn("PERSIST_FORMAT", r.stdout)
        r = self.run_tool("status", "--json", production=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["scope"], ["/home", "/root"])

    def test_exact_cli_and_confirmation(self):
        for cmd in ("setup", "marker"):
            self.refuse(cmd, self.device, message="--confirm", code=2)
        bad_args = [(), ("explode",), ("status", "--bad"), ("status", "--json", "extra"),
                    ("--json", "status"), ("setup",), ("marker",),
                    ("setup", self.device, "extra", "--confirm"),
                    ("marker", self.device, "--confirm", "--confirm"),
                    ("setup", "--confirm", self.device), ("marker", self.device, "--bad")]
        for args in bad_args:
            with self.subTest(args=args):
                self.refuse(*args, message="usage:", code=2)

    def test_root_and_block_identity(self):
        cases = [("nonroot", "require root"), ("regular", "not a block device"),
                 ("ioctl", "cannot determine"), ("empty-device", "cannot determine"),
                 ("readonly", "read-only"), ("locked", "locked"),
                 ("identity-changed", "identity changed"), ("busy", "busy")]
        for cmd in ("setup", "marker"):
            for fault, message in cases:
                with self.subTest(cmd=cmd, fault=fault):
                    self.refuse(cmd, self.device, "--confirm", message=message, fault=fault)

    def test_whole_disk_paths_and_sysfs_mismatch(self):
        self.refuse("setup", self.fixture / "dev/disk", "--confirm", message="dedicated partition")
        self.refuse("marker", self.fixture / "dev/disk", "--confirm", message="dedicated partition")
        self.refuse("setup", str(self.fixture / "dev") + "/../dev/disk1", "--confirm", message="without traversal")
        self.refuse("setup", "/etc/passwd", "--confirm", message="under /dev/")
        alias = self.fixture / "dev/outside"
        alias.symlink_to(self.volume)
        self.refuse("marker", alias, "--confirm", message="under /dev/")
        (self.fixture / "sys/dev/block/8:1").unlink()
        (self.fixture / "sys/dev/block/8:1").symlink_to(self.nodes["disk2"])
        self.refuse("setup", self.device, "--confirm", message="sysfs device identity")

    def test_mounted_device_ancestors_and_descendants(self):
        for dev in ("8:1", "8:0", "8:3"):
            for cmd in ("setup", "marker"):
                with self.subTest(dev=dev, cmd=cmd):
                    self.mounts.write_text(mount_line() + mount_line(dev=dev, point="/mnt/a\\040b", source="/dev/by-id/alias", filesystem="ext4", number=2))
                    self.refuse(cmd, self.device, "--confirm", message="ancestor/descendant is mounted")

    def test_live_media_sibling_protected(self):
        for point in ("/run/live-media", "/run/live-media/iso", "/run/live-lower"):
            self.mounts.write_text(mount_line() + mount_line(dev="8:2", point=point, source="/dev/alias", filesystem="iso9660", number=2))
            self.refuse("setup", self.device, "--confirm", message="live boot medium")
            self.refuse("marker", self.device, "--confirm", message="live boot medium")

    def test_existing_persistence_is_refused(self):
        self.mounts.write_text(mount_line() + mount_line(dev="8:2", point="/run/persist", filesystem="ext4", number=2))
        self.refuse("setup", self.device, "--confirm", message="persistence volume is already mounted")

    def test_active_swap_ancestors_descendants_and_escaped_path(self):
        for name in ("disk", "disk1", "diskchild"):
            for cmd in ("setup", "marker"):
                (self.fixture / "proc/swaps").write_text("Filename Type Size Used Priority\n" +
                                                        f"{self.fixture / 'dev' / name} partition 4096 0 -2\n")
                self.refuse(cmd, self.device, "--confirm", message="backs active swap")
        alias = self.fixture / "dev/swap space"
        alias.symlink_to(self.device)
        (self.fixture / "proc/swaps").write_text("Filename Type Size Used Priority\n" +
                                                str(alias).replace(" ", "\\040") + " partition 4096 0 -2\n")
        self.refuse("setup", self.device, "--confirm", message="backs active swap")
        swapfile = self.fixture / "swapfile"
        swapfile.write_bytes(b"fixture swap")
        (self.fixture / "proc/swaps").write_text("Filename Type Size Used Priority\n" +
                                                str(swapfile) + " file 4096 0 -2\n")
        self.refuse("setup", self.device, "--confirm", message="backs active swap", fault="swapfile")

    def test_holders_on_target_and_parent(self):
        for name in ("disk1", "disk", "diskchild"):
            holder = self.nodes[name] / "holders/dm-0"
            holder.symlink_to(self.nodes["disk2"])
            self.refuse("setup", self.device, "--confirm", message="active holders")
            self.refuse("marker", self.device, "--confirm", message="active holders")
            holder.unlink()

    def test_missing_or_malformed_safety_inputs_fail_closed(self):
        for path, broken, message in [(self.mounts, "garbage\n", "malformed mountinfo"),
                                      (self.mounts, "", "empty mountinfo"),
                                      (self.fixture / "proc/swaps", "garbage\n", "malformed active swap"),
                                      (self.fixture / "proc/swaps", "Filename Type Size Used Priority\n/missing file 1 0 -1\n", "cannot identify active swap")]:
            original = path.read_text()
            path.write_text(broken)
            self.refuse("setup", self.device, "--confirm", message=message)
            path.write_text(original)
        self.mounts.unlink()
        self.refuse("setup", self.device, "--confirm", message="cannot read mountinfo")
        self.mounts.write_text(mount_line())
        (self.fixture / "proc/swaps").unlink()
        self.refuse("marker", self.device, "--confirm", message="cannot read active swap")
        (self.fixture / "proc/swaps").write_text("Filename Type Size Used Priority\n")
        (self.nodes["disk"] / "holders").rmdir()
        self.refuse("setup", self.device, "--confirm", message="cannot inspect")

    def test_setup_fixed_argv_alias_and_private_mount(self):
        aliasdir = self.fixture / "dev/by-id"
        aliasdir.mkdir(parents=True)
        alias = aliasdir / "dedicated"
        alias.symlink_to(self.device)
        self.device.write_bytes(b"unformatted fixture")
        r = self.run_tool("setup", alias, "--confirm")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"PERSIST_SETUP device={self.device}", r.stdout)
        mkfs = [s[5:] for s in self.trace() if s.startswith("mkfs ")]
        self.assertEqual([json.loads(s) for s in mkfs], [["-F", "-L", "XTOS-PERSIST", str(self.device)]])
        self.assertIn("fsync-marker", self.trace())
        self.assertIn("syncfs", self.trace())
        self.assertIn("umount", self.trace())
        self.assertEqual(list((self.fixture / "run").iterdir()), [])
        self.assertEqual((self.volume / "XTOS_PERSIST_ID").read_bytes(), b"XTOS-PERSIST-1\n")
        self.assertEqual(sorted(p.name for p in self.volume.iterdir()), ["XTOS_PERSIST_ID", "home", "root"])
        self.assertEqual((self.volume / "root").stat().st_mode & 0o777, 0o700)

    def test_marker_preserves_keys_data_and_never_formats(self):
        ssh = self.volume / "root/.ssh"
        ssh.mkdir(parents=True)
        key = ssh / "authorized_keys"
        key.write_text("ssh-ed25519 existing-user-key\n")
        home = self.volume / "home"
        home.mkdir()
        (home / "proof").write_bytes(b"user data")
        (self.volume / "XTOS_PERSIST_ID").write_text("old marker\n")
        before = self.device.read_bytes()
        r = self.run_tool("marker", self.device, "--confirm")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.device.read_bytes(), before)
        self.assertEqual(key.read_text(), "ssh-ed25519 existing-user-key\n")
        self.assertEqual((home / "proof").read_bytes(), b"user data")
        self.assertEqual((self.volume / "XTOS_PERSIST_ID").read_text(), "XTOS-PERSIST-1\n")
        self.assertFalse(any(s.startswith(("fork", "mkfs")) for s in self.trace()))

    def test_marker_requires_ext4_and_fixed_label(self):
        for label, ext4 in (("OTHER", True), ("XTOS-PERSIST", False)):
            self.device.write_bytes(superblock(label, ext4))
            self.refuse("marker", self.device, "--confirm", message="ext4 with label")
        self.device.write_bytes(b"not ext4")
        self.refuse("marker", self.device, "--confirm", message="ext4 with label")

    def test_formatter_and_mount_failures_no_success_marker(self):
        for fault, message in [("fork", "cannot fork"), ("exec", "not available"),
                               ("wait", "cannot wait"), ("mkfs-failed", "mkfs.ext4 failed"),
                               ("mkfs-signal", "terminated by signal"),
                               ("mounted-after-mkfs", "is mounted"), ("mount", "cannot mount")]:
            with self.subTest(fault=fault):
                r = self.run_tool("setup", self.device, "--confirm", fault=fault)
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn(message, r.stderr)
                self.assertNotIn("PERSIST_SETUP", r.stdout)
                self.assertFalse((self.volume / "XTOS_PERSIST_ID").exists())
                self.assertEqual(list((self.fixture / "run").iterdir()), [])
                self.mounts.write_text(mount_line())

    def test_marker_io_failures_and_unmount_cleanup(self):
        for fault, message in [("write", "cannot write"), ("fsync-marker", "cannot fsync marker"),
                               ("fsync-dir", "sync persistence directory"),
                               ("close-marker", "cannot close marker"), ("syncfs", "cannot sync")]:
            with self.subTest(fault=fault):
                r = self.run_tool("marker", self.device, "--confirm", fault=fault)
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn(message, r.stderr)
                self.assertNotIn("PERSIST_SETUP", r.stdout)
                self.assertEqual(self.trace()[-1], "umount")
                self.assertEqual(list((self.fixture / "run").iterdir()), [])
                self.assertFalse(list(self.volume.glob(".XTOS_PERSIST_ID.tmp.*")))
        r = self.run_tool("marker", self.device, "--confirm", fault="umount")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("volume remains mounted", r.stderr)
        self.assertNotIn("PERSIST_SETUP", r.stdout)
        self.assertFalse(self.volume.exists())
        probes = [p for p in (self.fixture / "run").iterdir() if not p.name.endswith(".empty")]
        self.assertEqual(len(probes), 1)
        self.assertTrue((probes[0] / "XTOS_PERSIST_ID").exists())

    def test_marker_symlink_hardlink_and_directory_traps(self):
        outside = self.fixture / "outside"
        outside.write_text("untouched")
        for name in ("XTOS_PERSIST_ID", "home", "root"):
            path = self.volume / name
            path.symlink_to(outside)
            r = self.run_tool("marker", self.device, "--confirm")
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertNotIn("PERSIST_SETUP", r.stdout)
            self.assertEqual(outside.read_text(), "untouched")
            self.assertEqual(self.trace()[-1], "umount")
            path.unlink()
        marker = self.volume / "XTOS_PERSIST_ID"
        marker.hardlink_to(outside)
        r = self.run_tool("marker", self.device, "--confirm")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("private regular file", r.stderr)
        self.assertEqual(outside.read_text(), "untouched")

    def test_interrupted_and_short_operations(self):
        for fault in ("wait-eintr", "umount-eintr", "write-eintr", "write-short"):
            r = self.run_tool("setup", self.device, "--confirm", fault=fault)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual((self.volume / "XTOS_PERSIST_ID").read_bytes(), b"XTOS-PERSIST-1\n")
            self.assertEqual(list((self.fixture / "run").iterdir()), [])

    def test_status_reports_mountpoints_and_binds(self):
        r = self.run_tool("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("RAM-only (persistence=0)", r.stdout)
        data = json.loads(self.run_tool("status", "--json").stdout)
        self.assertFalse(data["active"])
        self.assertEqual(len(data["candidates"]), 3)
        self.assertFalse(self.trace())
        # A source named /run/persist is not a mountpoint.
        self.mounts.write_text(mount_line() + mount_line(dev="8:1", point="/elsewhere", source="/run/persist", filesystem="ext4", number=2))
        self.assertFalse(json.loads(self.run_tool("status", "--json").stdout)["mounted"])
        active = mount_line() + mount_line(dev="8:1", point="/run/persist", filesystem="ext4", number=2)
        self.mounts.write_text(active)
        data = json.loads(self.run_tool("status", "--json").stdout)
        self.assertTrue(data["mounted"])
        self.assertFalse(data["active"])
        self.assertFalse(data["home_persistent"])
        self.mounts.write_text(active + mount_line(dev="8:1", root="/home", point="/home", filesystem="ext4", number=3) +
                              mount_line(dev="8:1", root="/root", point="/root", filesystem="ext4", number=4))
        r = self.run_tool("status", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertTrue(data["active"])
        self.assertEqual(data["persistence"], 1)
        self.assertTrue(data["home_persistent"])
        self.assertTrue(data["root_persistent"])
        self.assertEqual(data["scope"], ["/home", "/root"])
        self.assertIn("ACTIVE (persistence=1)", self.run_tool("status").stdout)
        self.mounts.write_text(self.mounts.read_text() + "9 0 0:4 net:[4026532805] /run/netns/test rw - nsfs nsfs rw\n")
        self.assertTrue(json.loads(self.run_tool("status", "--json").stdout)["active"])
        self.mounts.write_text(self.mounts.read_text() + mount_line(dev="8:2", root="/root", point="/root", filesystem="ext4", number=5))
        self.assertFalse(json.loads(self.run_tool("status", "--json").stdout)["active"])

    def test_status_candidates_exact_labels_ext4_json_and_no_shell(self):
        name = 'odd";touch SENTINEL;#'
        self.block(name, "8:4", f"devices/virtual/{name}", partition=True)
        self.block("wrong", "8:5", "devices/virtual/wrong", partition=True, label="OTHER")
        self.block("ext3", "8:6", "devices/virtual/ext3", partition=True, ext4=False)
        r = self.run_tool("status", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        paths = [entry["device"] for entry in data["candidates"]]
        self.assertEqual(len(paths), 4)
        self.assertIn(str(self.fixture / "dev" / name), paths)
        self.assertNotIn(str(self.fixture / "dev/wrong"), paths)
        self.assertNotIn(str(self.fixture / "dev/ext3"), paths)
        self.assertFalse(self.trace())
        self.assertFalse((ROOT / "SENTINEL").exists())
        # The same metacharacters are a single syscall argument on a write path.
        r = self.run_tool("marker", self.fixture / "dev" / name, "--confirm")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(any(s.startswith(("fork", "mkfs")) for s in self.trace()))


def main():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(PersistTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful():
        print(f"ALL PASS ({result.testsRun} tests; no host mounts, formatting, or loops)")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
