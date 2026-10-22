#!/usr/bin/env python3
"""Focused acceptance tests for the native non-privileged runner.

The tests compile the runner and a tiny static probe in a temporary fixture.
They use a real child process and never modify host seccomp, cgroups, mounts,
or network configuration.
"""
from __future__ import annotations

import fcntl
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "tools" / "xtos-native" / "runner" / "runner.c"
CFLAGS = ["-std=c11", "-Wall", "-Wextra", "-Werror", "-O2", "-static"]
PROBE_SOURCE = r'''
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/futex.h>
#include <stdlib.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <unistd.h>

static void emit(const char *text) {
    (void)write(STDOUT_FILENO, text, strlen(text));
}

static const char *fd_state(int fd) {
    errno = 0;
    if (fcntl(fd, F_GETFD) < 0) {
        return errno == EBADF ? "EBADF" : "ERROR";
    }
    return "OPEN";
}

int main(int argc, char **argv) {
    if (argc == 6 && strcmp(argv[1], "--fd-check") == 0) {
        int socket_a = atoi(argv[2]);
        int socket_b = atoi(argv[3]);
        int file_fd = atoi(argv[4]);
        int high_file_fd = atoi(argv[5]);
        char stdin_byte = 0;
        char line[512];
        int stdin_open = fcntl(STDIN_FILENO, F_GETFD) >= 0;
        ssize_t stdin_read = read(STDIN_FILENO, &stdin_byte, 1);
        int stdout_usable = fcntl(STDOUT_FILENO, F_GETFD) >= 0 &&
                            write(STDOUT_FILENO, "fd-check-stdout\n", 16) == 16;
        int stderr_usable = fcntl(STDERR_FILENO, F_GETFD) >= 0 &&
                            write(STDERR_FILENO, "fd-check-stderr\n", 16) == 16;
        int used = snprintf(line, sizeof(line),
            "socket_a=%s socket_b=%s file=%s high_file=%s stdin=%s stdout=%s stderr=%s\n",
            fd_state(socket_a), fd_state(socket_b), fd_state(file_fd),
            fd_state(high_file_fd),
            stdin_open && stdin_read == 0 ? "usable" : "broken",
            stdout_usable ? "usable" : "broken",
            stderr_usable ? "usable" : "broken");
        if (used < 0 || (size_t)used >= sizeof(line)) {
            return 90;
        }
        emit(line);
        return 0;
    }
    if (argc == 3 && strcmp(argv[1], "--touch") == 0) {
        int marker_fd = open(argv[2], O_WRONLY | O_CREAT | O_EXCL, 0600);
        if (marker_fd < 0) {
            return 91;
        }
        (void)write(marker_fd, "executed\n", 9);
        (void)close(marker_fd);
        return 23;
    }
    if (argc == 4 && strcmp(argv[1], "--touch-fd") == 0) {
        int inherited_fd = atoi(argv[3]);
        errno = 0;
        if (fcntl(inherited_fd, F_GETFD) >= 0 || errno != EBADF) {
            return 92;
        }
        int marker_fd = open(argv[2], O_WRONLY | O_CREAT | O_EXCL, 0600);
        if (marker_fd < 0) {
            return 93;
        }
        (void)write(marker_fd, "executed\n", 9);
        (void)close(marker_fd);
        return 0;
    }
    if (argc == 2 && strcmp(argv[1], "--uid") == 0) {
        dprintf(STDOUT_FILENO, "%lu\n", (unsigned long)getuid());
        return 0;
    }
    if (argc == 2 && strcmp(argv[1], "--sleep") == 0) {
        sleep(1);
        return 0;
    }
    if (argc == 2 && strcmp(argv[1], "--write-check") == 0) {
        int allowed = open("/work/allowed", O_WRONLY | O_CREAT | O_EXCL, 0600);
        int denied = open("/not-allowed/denied", O_WRONLY | O_CREAT | O_EXCL, 0600);
        int existing = open("/not-allowed/existing", O_WRONLY | O_TRUNC);
        bool ok = allowed >= 0 && denied < 0 && existing < 0;
        if (allowed >= 0) { (void)write(allowed, "allowed\n", 8); (void)close(allowed); }
        if (denied >= 0) (void)close(denied);
        if (existing >= 0) (void)close(existing);
        dprintf(STDOUT_FILENO, "allowed=%s denied=%s truncate=%s\n",
                allowed >= 0 ? "OPEN" : "BLOCKED", denied < 0 ? "BLOCKED" : "OPEN",
                existing < 0 ? "BLOCKED" : "OPEN");
        return ok ? 0 : 96;
    }
    if (argc == 3 && strcmp(argv[1], "--truncate-ops") == 0) {
        char path[1024];
        (void)snprintf(path, sizeof(path), "%s/ftruncate", argv[2]);
        errno = 0;
        int fd = open(path, O_WRONLY);
        dprintf(STDOUT_FILENO, "write-open=%d errno=%d\n", fd, fd < 0 ? errno : 0);
        /* A denied write-open must not prevent the negative syscall itself
         * from reaching the kernel; a read-only fd cannot mutate the file. */
        if (fd < 0) fd = open(path, O_RDONLY);
        errno = 0;
        long rc = syscall(SYS_ftruncate, fd, 3);
        dprintf(STDOUT_FILENO, "ftruncate=%ld errno=%d\n", rc, rc < 0 ? errno : 0);
        if (fd >= 0) (void)close(fd);
#ifdef SYS_truncate
        (void)snprintf(path, sizeof(path), "%s/truncate", argv[2]);
        errno = 0;
        rc = syscall(SYS_truncate, path, 4);
        dprintf(STDOUT_FILENO, "truncate=%ld errno=%d\n", rc, rc < 0 ? errno : 0);
#endif
        return 0;
    }
    if (argc == 3 && strcmp(argv[1], "--directory-ops") == 0) {
        char path[1024];
#define DIR_OP(name, leaf, operation) do { \
        (void)snprintf(path, sizeof(path), "%s/%s", argv[2], leaf); \
        errno = 0; \
        long rc = (operation); \
        dprintf(STDOUT_FILENO, "%s=%ld errno=%d\n", name, rc, rc < 0 ? errno : 0); \
    } while (0)
        DIR_OP("mkdirat", "new-at", syscall(SYS_mkdirat, AT_FDCWD, path, 0700));
        DIR_OP("unlinkat", "delete-at", syscall(SYS_unlinkat, AT_FDCWD, path, 0));
        DIR_OP("unlinkat-dir", "remove-at", syscall(SYS_unlinkat, AT_FDCWD, path, AT_REMOVEDIR));
#ifdef SYS_mkdir
        DIR_OP("mkdir", "new-legacy", syscall(SYS_mkdir, path, 0700));
#endif
#ifdef SYS_unlink
        DIR_OP("unlink", "delete-legacy", syscall(SYS_unlink, path));
#endif
#ifdef SYS_rmdir
        DIR_OP("rmdir", "remove-legacy", syscall(SYS_rmdir, path));
#endif
        DIR_OP("symlinkat", "new-link", syscall(SYS_symlinkat, "delete-at", AT_FDCWD, path));
        DIR_OP("linkat", "hard-link", syscall(SYS_linkat, AT_FDCWD, "/not-allowed/delete-at", AT_FDCWD, path, 0));
        DIR_OP("mknodat", "new-fifo", syscall(SYS_mknodat, AT_FDCWD, path, 0010000 | 0600, 0));
        DIR_OP("renameat", "renamed", syscall(SYS_renameat, AT_FDCWD, "/not-allowed/delete-at", AT_FDCWD, path));
#undef DIR_OP
        return 0;
    }
    if (argc == 7 && strcmp(argv[1], "--root-check") == 0) {
        int inherited_fd = atoi(argv[5]);
        bool inherited_closed = strcmp(fd_state(inherited_fd), "EBADF") == 0;
        int absolute_fd = open(argv[2], O_RDONLY);
        int parent_fd = open(argv[3], O_RDONLY);
        int symlink_fd = open(argv[4], O_RDONLY);
        bool blocked = absolute_fd < 0 && parent_fd < 0 && symlink_fd < 0;
        if (absolute_fd >= 0) (void)close(absolute_fd);
        if (parent_fd >= 0) (void)close(parent_fd);
        if (symlink_fd >= 0) (void)close(symlink_fd);
        dprintf(STDOUT_FILENO, "absolute=%s parent=%s symlink=%s fd=%s\n",
                absolute_fd < 0 ? "BLOCKED" : "OPEN",
                parent_fd < 0 ? "BLOCKED" : "OPEN",
                symlink_fd < 0 ? "BLOCKED" : "OPEN",
                inherited_closed ? "EBADF" : "OPEN");
        if (!blocked || !inherited_closed) return 94;
        int marker_fd = open(argv[6], O_WRONLY | O_CREAT | O_EXCL, 0600);
        if (marker_fd < 0) return 95;
        emit("root-boundary-isolated\n");
        (void)write(marker_fd, "root-isolated\n", strlen("root-isolated\n"));
        (void)close(marker_fd);
        return 0;
    }
    char line[4096];
    char cwd[1024];
    int fd = dup(STDIN_FILENO);
    char byte = 0;
    ssize_t read_result = -2;
    if (fd >= 0) {
        read_result = read(fd, &byte, 1);
        (void)close(fd);
    }

    int word = 0;
    errno = 0;
    long futex_result = syscall(SYS_futex, &word, FUTEX_WAKE_PRIVATE, 1,
                                NULL, NULL, 0);
    int futex_errno = errno;

    errno = 0;
    long pid_result = syscall(SYS_getpid);
    int pid_errno = errno;

    errno = 0;
    long socket_result = syscall(SYS_socket, AF_UNIX, SOCK_STREAM, 0);
    int socket_errno = errno;
    if (socket_result >= 0) {
        (void)close((int)socket_result);
    }

    int no_new_privs = prctl(PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0);
    int seccomp_mode = prctl(PR_GET_SECCOMP, 0, 0, 0, 0);
    struct rlimit cpu = {0, 0};
    struct rlimit address = {0, 0};
    struct rlimit nofile = {0, 0};
    struct rlimit nproc = {0, 0};
    (void)getrlimit(RLIMIT_CPU, &cpu);
    (void)getrlimit(RLIMIT_AS, &address);
    (void)getrlimit(RLIMIT_NOFILE, &nofile);
    (void)getrlimit(RLIMIT_NPROC, &nproc);
    int cwd_ok = getcwd(cwd, sizeof(cwd)) != NULL;
    if (!cwd_ok) {
        (void)strcpy(cwd, "<getcwd-failed>");
    }

    int used = snprintf(line, sizeof(line),
        "uid=%lu euid=%lu gid=%lu egid=%lu nnp=%d seccomp=%d "
        "read=%zd futex=%ld futex_errno=%d pid=%ld pid_errno=%d "
        "socket=%ld socket_errno=%d cwd=%s cpu=%llu as=%llu nofile=%llu nproc=%llu "
        "argc=%d arg1=%s arg2=%s\n",
        (unsigned long)getuid(), (unsigned long)geteuid(),
        (unsigned long)getgid(), (unsigned long)getegid(),
        no_new_privs, seccomp_mode, read_result, futex_result, futex_errno,
        pid_result, pid_errno, socket_result, socket_errno, cwd,
        (unsigned long long)cpu.rlim_cur, (unsigned long long)address.rlim_cur,
        (unsigned long long)nofile.rlim_cur, (unsigned long long)nproc.rlim_cur,
        argc, argc > 1 ? argv[1] : "<missing>",
        argc > 2 ? argv[2] : "<missing>");
    if (used < 0 || (size_t)used >= sizeof(line)) {
        return 90;
    }
    emit(line);
    (void)dprintf(STDERR_FILENO, "stderr-quote=\"safe\"\\n\n");
    return 0;
}
'''

FAULT_WRAPPER_SOURCE = r'''
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdint.h>
#include <linux/landlock.h>
#include <sys/syscall.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int __real_close(int fd);
int __real_closedir(DIR *directory);
int __real_chroot(const char *path);
long __real_syscall(long number, ...);

int __wrap_chroot(const char *path) {
    const char *mode = getenv("XTOS_RUNNER_FAULT_MODE");
    if (mode != NULL && strcmp(mode, "chroot_eperm") == 0) {
        errno = EPERM;
        return -1;
    }
    return __real_chroot(path);
}

long __wrap_syscall(long number, ...) {
    va_list ap;
    va_start(ap, number);
    const char *mode = getenv("XTOS_RUNNER_FAULT_MODE");
    long result;
    if (number == SYS_landlock_create_ruleset) {
        void *attr = va_arg(ap, void *);
        size_t size = va_arg(ap, size_t);
        unsigned int flags = va_arg(ap, unsigned int);
        if (mode != NULL && strcmp(mode, "landlock_enosys") == 0) {
            errno = ENOSYS; result = -1;
        } else if (mode != NULL && strcmp(mode, "landlock_old_abi") == 0 &&
                   flags == LANDLOCK_CREATE_RULESET_VERSION) {
            result = 2;
        } else if (mode != NULL && strcmp(mode, "landlock_create_eio") == 0 && flags == 0) {
            errno = EIO; result = -1;
        } else result = __real_syscall(number, attr, size, flags);
    } else if (number == SYS_landlock_add_rule) {
        int fd = va_arg(ap, int);
        int type = va_arg(ap, int);
        void *attr = va_arg(ap, void *);
        int flags = va_arg(ap, int);
        if (mode != NULL && strcmp(mode, "landlock_add_eio") == 0) {
            errno = EIO; result = -1;
        } else result = __real_syscall(number, fd, type, attr, flags);
    } else if (number == SYS_landlock_restrict_self) {
        int fd = va_arg(ap, int);
        int flags = va_arg(ap, int);
        if (mode != NULL && strcmp(mode, "landlock_restrict_eio") == 0) {
            errno = EIO; result = -1;
        } else result = __real_syscall(number, fd, flags);
    } else if (number == SYS_openat2) {
        int fd = va_arg(ap, int);
        const char *path = va_arg(ap, const char *);
        void *how = va_arg(ap, void *);
        size_t size = va_arg(ap, size_t);
        result = __real_syscall(number, fd, path, how, size);
    } else { errno = ENOSYS; result = -1; }
    va_end(ap);
    return result;
}

static int close_fault_used;
static int closedir_fault_used;

static int selected_fd(void) {
    const char *text = getenv("XTOS_RUNNER_FAULT_FD");
    char *end = NULL;
    long value;
    if (text == NULL || *text == '\0') return -1;
    errno = 0;
    value = strtol(text, &end, 10);
    if (errno != 0 || end == text || *end != '\0' || value < 0 || value > 2147483647L)
        return -1;
    return (int)value;
}

int __wrap_close(int fd) {
    const char *mode = getenv("XTOS_RUNNER_FAULT_MODE");
    if (!close_fault_used && fd == selected_fd() && mode != NULL &&
        (strcmp(mode, "close_eintr") == 0 || strcmp(mode, "close_eio") == 0)) {
        close_fault_used = 1;
        (void)__real_close(fd);
        errno = strcmp(mode, "close_eintr") == 0 ? EINTR : EIO;
        return -1;
    }
    return __real_close(fd);
}

int __wrap_closedir(DIR *directory) {
    const char *mode = getenv("XTOS_RUNNER_FAULT_MODE");
    int fd = selected_fd();
    if (!closedir_fault_used && mode != NULL &&
        strcmp(mode, "closedir_eio") == 0 && fd >= 0 &&
        fcntl(fd, F_GETFD) < 0 && errno == EBADF) {
        closedir_fault_used = 1;
        (void)__real_closedir(directory);
        errno = EIO;
        return -1;
    }
    return __real_closedir(directory);
}
'''


def compile_binary(output: Path, source: Path, *extra: str) -> None:
    subprocess.run(
        ["gcc", *CFLAGS, *extra, "-o", str(output), str(source)],
        check=True,
        capture_output=True,
        text=True,
    )


def run(binary: Path, *args: str, pass_fds: tuple[int, ...] = (),
        env: dict[str, str] | None = None,
        timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(binary), *args],
        capture_output=True,
        text=True,
        pass_fds=pass_fds,
        env=env,
        timeout=timeout,
    )


def parse_result(result: subprocess.CompletedProcess[str]) -> dict:
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"runner did not emit JSON: rc={result.returncode} "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        ) from exc
    if not isinstance(value, dict):
        raise AssertionError(f"runner JSON is not an object: {value!r}")
    return value


def runner_args(root: Path, uid: int = 65534, gid: int = 65534,
                include_identity: bool = True) -> list[str]:
    args = [
        "--root", str(root),
        "--cwd", str(root),
        "--write-dir", "/work",
        "--supp-groups-clear",
        "--deny-socket",
        "--deny-getpid",
        "--cpu-sec", "2",
        "--as-bytes", "268435456",
        "--nofile", "64",
        "--nproc", "16",
    ]
    if include_identity:
        args[4:4] = ["--uid", str(uid), "--gid", str(gid)]
    return args


class NamespaceIsolationOptInTests(unittest.TestCase):
    def test_namespace_isolation_flag_is_present_and_opt_in(self) -> None:
        source = SRC.read_text(encoding="utf-8")
        self.assertTrue('"--namespace-isolation"' in source, "explicit opt-in flag is absent")
        # Exercise the real parser only: this binary cannot enter child setup.
        with tempfile.TemporaryDirectory(prefix="runner-parser-") as name:
            td = Path(name)
            harness = td / "parser.c"
            harness.write_text(
                '#define main runner_program_main\n#include "' + str(SRC) + '"\n'
                '#undef main\nint main(void) { struct config c; '
                'char *a[] = {"runner", "--root", "/fixture", "--cwd", "/fixture", '
                '"--uid", "65534", "--gid", "65534", "--supp-groups-clear", '
                '"--cpu-sec", "2", "--as-bytes", "268435456", "--nofile", "64", '
                '"--nproc", "16", "--namespace-isolation", "--", "/probe"}; '
                'if (parse_options(21, a, &c) != 0 || !c.namespace_isolation || c.deny_socket) return 1; '
                'a[18] = "--"; a[19] = "/probe"; '
                'return parse_options(20, a, &c) != 0 || c.namespace_isolation; }\n',
                encoding="utf-8")
            binary = td / "parser"
            compile_binary(binary, harness, "-DXTOS_RUNNER_TEST_MODE=1")
            self.assertEqual(run(binary).returncode, 0)

    def test_namespace_source_contract(self) -> None:
        source = SRC.read_text(encoding="utf-8")
        self.assertIn("unshare(CLONE_NEWNS | CLONE_NEWNET | CLONE_NEWPID)", source)
        self.assertNotIn("CLONE_NEWUSER", source)
        self.assertIn("MS_REC | MS_PRIVATE", source)
        self.assertIn("PR_SET_PDEATHSIG", source)
        self.assertIn("waitpid(target", source)
        self.assertIn("O_NOFOLLOW", source)
        self.assertIn("proc_stat.st_uid != 0", source)
        self.assertIn("(proc_stat.st_mode & 0222) != 0", source)
        self.assertIn("proc_failure = ENOTEMPTY", source)
        self.assertIn("MS_NOSUID | MS_NODEV | MS_NOEXEC", source)
        self.assertIn("__NR_connect", source)
        self.assertIn("__NR_socketpair", source)
        self.assertNotIn("--allow-network", source)
        fixture = (Path(__file__).parent / "fixtures" / "runner_namespaces.c").read_text(encoding="utf-8")
        self.assertIn("SYS_socketpair", fixture)
        self.assertIn("ipv4_default_route", fixture)
        self.assertIn("net_interface_allowed", fixture)
        self.assertIn("ipv4_route_header", fixture)

    def test_route_fixture_parses_default_flags_without_namespace_calls(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "runner_namespaces.c"
        with tempfile.TemporaryDirectory(prefix="runner-route-parser-") as name:
            td = Path(name)
            harness = td / "route-parser.c"
            harness.write_text(
                '#define main namespace_probe_main\n#include "' + str(fixture) + '"\n'
                '#undef main\nint main(int argc, char **argv) { '
                'if (argc != 3) return 2; '
                'int result = strcmp(argv[1], "4") == 0 ? '
                'ipv4_default_route(argv[2]) : ipv6_default_route(argv[2]); '
                'printf("%d\\n", result); return 0; }\n', encoding="utf-8")
            binary = td / "route-parser"
            compile_binary(binary, harness)
            zero = "0" * 32
            cases = [
                ("4", "eth0 00000000 0102000A 0003 0 0 0 00000000 0 0 0", 1),
                ("4", "lo 0000007F 00000000 0001 0 0 0 000000FF 0 0 0", 0),
                ("4", "lo 00000000 00000000 0000 0 0 0 00000000 0 0 0", 0),
                ("4", "lo 00000000 00000000 0201 0 0 0 00000000 0 0 0", 0),
                ("4", "malformed", -1),
                ("6", f"{zero} 00 {zero} 00 {zero} 0 0 0 1 lo", 1),
                ("6", f"{zero} 80 {zero} 00 {zero} 0 0 0 1 lo", 0),
                ("6", f"{zero} 00 {zero} 00 {zero} 0 0 0 0 lo", 0),
                ("6", f"{zero} 00 {zero} 00 {zero} 0 0 0 201 lo", 0),
                ("6", "malformed", -1),
            ]
            for family, row, expected in cases:
                with self.subTest(family=family, row=row):
                    result = run(binary, family, row)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), str(expected))

    def test_fresh_netns_interface_and_route_header_contract(self) -> None:
        # Guest-observed ground truth (disposable QEMU, kernel 6.6.0-rt15 with
        # CONFIG_IPV6_SIT=y): a fresh unconfigured CLONE_NEWNET namespace lists
        # exactly lo and sit0 in /proc/net/dev, and /proc/net/route is entirely
        # empty -- no header row at all. The fixture must accept exactly that
        # shape while still rejecting any foreign interface or usable route.
        fixture = Path(__file__).parent / "fixtures" / "runner_namespaces.c"
        with tempfile.TemporaryDirectory(prefix="runner-netns-contract-") as name:
            td = Path(name)
            harness = td / "netns-contract.c"
            harness.write_text(
                '#define main namespace_probe_main\n#include "' + str(fixture) + '"\n'
                '#undef main\nint main(int argc, char **argv) { '
                'if (argc != 3) return 2; '
                'int result = strcmp(argv[1], "iface") == 0 ? '
                'net_interface_allowed(argv[2]) : ipv4_route_header(argv[2]); '
                'printf("%d\\n", result); return 0; }\n', encoding="utf-8")
            binary = td / "netns-contract"
            compile_binary(binary, harness)
            kernel_header = "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT"
            cases = [
                ("iface", "lo", 1),
                ("iface", "sit0", 1),
                ("iface", "eth0", 0),
                ("iface", "veth0", 0),
                ("iface", "", 0),
                ("header", kernel_header, 1),
                ("header", "lo\t00000000\t00000000\t0003\t0\t0\t0\t00000000\t0\t0\t0", 0),
                ("header", "", 0),
            ]
            for mode, value, expected in cases:
                with self.subTest(mode=mode, value=value):
                    result = run(binary, mode, value)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), str(expected))

    def test_namespace_setup_failures_never_exec(self) -> None:
        # These wrappers NEVER call real unshare/mount, including unexpected paths.
        wrapper = r'''
#define _GNU_SOURCE
#include <errno.h>
#include <sched.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/types.h>
#include <unistd.h>
pid_t __real_fork(void);
int __wrap_unshare(int flags) {
    if (flags != (CLONE_NEWNS | CLONE_NEWNET | CLONE_NEWPID)) _exit(98);
    if (strcmp(getenv("NS_FAIL"), "unshare") == 0) { errno = EPERM; return -1; }
    return 0;
}
int __wrap_mount(const char *src, const char *dst, const char *type,
                 unsigned long flags, const void *data) {
    (void)data;
    if (flags == (MS_NOSUID | MS_NODEV | MS_NOEXEC) &&
        src != NULL && strcmp(src, "proc") == 0 &&
        dst != NULL && strcmp(dst, ".") == 0 &&
        type != NULL && strcmp(type, "proc") == 0) {
        errno = EIO;
        return -1;
    }
    if (flags != (MS_REC | MS_PRIVATE)) _exit(97);
    if (strcmp(getenv("NS_FAIL"), "mount") == 0) { errno = EIO; return -1; }
    return 0;
}
pid_t __wrap_fork(void) {
    static unsigned count;
    if (++count == 1 || strcmp(getenv("NS_FAIL"), "proc") == 0) return __real_fork();
    errno = EAGAIN; return -1;
}
'''
        with tempfile.TemporaryDirectory(prefix="runner-ns-failure-") as name:
            td = Path(name)
            wrapper_path = td / "wrapper.c"
            wrapper_path.write_text(wrapper, encoding="utf-8")
            binary = td / "runner"
            compile_binary(binary, SRC, "-DXTOS_RUNNER_TEST_MODE=1",
                           "-Wl,--wrap=unshare", "-Wl,--wrap=mount", "-Wl,--wrap=fork",
                           str(wrapper_path))
            (td / "proc").mkdir(mode=0o555)
            for mode in ("unshare", "mount", "fork", "proc"):
                with self.subTest(mode=mode):
                    args = [x for x in runner_args(td) if x not in ("--deny-socket", "--deny-getpid")]
                    result = run(binary, *args, "--namespace-isolation", "--",
                                 "/target-must-not-exec", env={**os.environ, "NS_FAIL": mode})
                    data = parse_result(result)
                    self.assertEqual(result.returncode, 125, data)
                    self.assertEqual(data.get("status"), "setup_error", data)
                    stage = "private proc mount" if mode == "proc" else "namespace setup"
                    self.assertIn(stage, data.get("setup_error") or "")
                    self.assertEqual(data.get("stdout"), "")



def main() -> int:
    if sys.platform != "linux":
        print("skip: linux only")
        return 0

    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        print(f"  {'PASS' if condition else 'FAIL'} {name}")
        if not condition:
            failures.append(f"{name}: {detail}")

    with tempfile.TemporaryDirectory(prefix="xtos-runner-") as td_name:
        td = Path(td_name)
        td.chmod(0o755)
        fixture_root = td / "fixture-root"
        outside = td / "outside"
        fixture_root.mkdir()
        outside.mkdir()
        # The child drops to 65534, so every fixture directory and executable
        # must be traversable without granting any host-wide permissions.
        fixture_root.chmod(0o755)
        outside.chmod(0o755)
        work = fixture_root / "work"
        work.mkdir()
        os.chown(work, 65534, 65534)
        work.chmod(0o700)
        not_allowed = fixture_root / "not-allowed"
        not_allowed.mkdir()
        os.chown(not_allowed, 65534, 65534)
        not_allowed.chmod(0o700)
        existing = not_allowed / "existing"
        existing.write_bytes(b"keep-existing\n")
        os.chown(existing, 65534, 65534)
        probe_source = td / "runner_probe.c"
        probe_source.write_text(PROBE_SOURCE, encoding="utf-8")
        probe = fixture_root / "runner_probe"
        runner = td / "xtos-runner"
        build_runner = subprocess.run(
            ["gcc", *CFLAGS, "-DXTOS_RUNNER_TEST_MODE=1", "-o", str(runner), str(SRC)],
            capture_output=True,
            text=True,
        )
        check(
            "runner builds with strict static flags",
            build_runner.returncode == 0,
            build_runner.stderr,
        )
        if build_runner.returncode != 0:
            print("STOP: runner source is unavailable or does not compile")
            return 1
        compile_binary(probe, probe_source)
        probe.chmod(probe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        fault_wrapper_source = td / "runner_fault_wrapper.c"
        fault_wrapper_source.write_text(FAULT_WRAPPER_SOURCE, encoding="utf-8")
        fault_runner = td / "xtos-runner-fault-wrap"
        build_fault_runner = subprocess.run(
            ["gcc", *CFLAGS, "-DXTOS_RUNNER_TEST_MODE=1",
             "-Wl,--wrap=close", "-Wl,--wrap=closedir", "-Wl,--wrap=chroot", "-Wl,--wrap=syscall",
             "-o", str(fault_runner), str(SRC), str(fault_wrapper_source)],
            capture_output=True,
            text=True,
        )
        check("fault-wrapped runner builds with static linker wrapping",
              build_fault_runner.returncode == 0, build_fault_runner.stderr)
        if build_fault_runner.returncode != 0:
            print("STOP: linker-wrapped runner fixture does not compile")
            return 1

        special_arg = 'quote="x"\nline;$(not-a-shell)'
        result = run(
            runner,
            *runner_args(fixture_root),
            "--cpu-sec", "2",
            "--as-bytes", str(256 * 1024 * 1024),
            "--nofile", "64",
            "--nproc", "16",
            "--timeout-ms", "1000",
            "--",
            "./runner_probe",
            "value with spaces",
            special_arg,
        )
        data = parse_result(result)
        stdout = data.get("stdout", "")
        check("fixed argv executes without a shell", data.get("exit_code") == 0,
              repr(data))
        check("argv preserves spaces and shell metacharacters",
              "arg1=value with spaces" in stdout and special_arg in stdout,
              repr(stdout))
        check("child drops to configured non-root identity",
              "uid=65534 euid=65534 gid=65534 egid=65534" in stdout,
              repr(stdout))
        check("no_new_privs readback is enabled", "nnp=1" in stdout, repr(stdout))
        check("seccomp filter readback is active", "seccomp=2" in stdout, repr(stdout))
        check("read/write and futex remain allowed", "read=0" in stdout and "futex=0" in stdout,
              repr(stdout))
        check("socket is blocked by the allowlist", "socket=-1 socket_errno=1" in stdout,
              repr(stdout))
        check("getpid is blocked by the selected deny rule", "pid=-1 pid_errno=1" in stdout,
              repr(stdout))
        check("all requested rlimits are read back",
              all(token in stdout for token in (
                  "cpu=2", "as=268435456", "nofile=64", "nproc=16")),
              repr(stdout))
        check("cwd is rebased to slash inside the isolated root",
              "cwd=/ " in stdout,
              repr(stdout))
        check("stderr is captured as JSON-safe data",
              data.get("stderr") == 'stderr-quote="safe"\\n\n',
              repr(data))
        check("result JSON reports a non-timeout exit",
              data.get("status") == "exit" and data.get("timed_out") is False,
              repr(data))

        sentinel_path = td / "fd-sentinel"
        sentinel_fd = os.open(sentinel_path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
        high_sentinel_fd = fcntl.fcntl(sentinel_fd, fcntl.F_DUPFD, 65)
        inherited_socket_a, inherited_socket_b = socket.socketpair()
        try:
            fd_check = run(
                runner,
                *runner_args(fixture_root),
                "--", "./runner_probe", "--fd-check",
                str(inherited_socket_a.fileno()), str(inherited_socket_b.fileno()),
                str(sentinel_fd), str(high_sentinel_fd),
                pass_fds=(inherited_socket_a.fileno(), inherited_socket_b.fileno(),
                          sentinel_fd, high_sentinel_fd),
            )
            fd_data = parse_result(fd_check)
            fd_stdout = fd_data.get("stdout", "")
            check("inherited socket descriptors are closed before exec",
                  "socket_a=EBADF socket_b=EBADF" in fd_stdout,
                  repr(fd_data))
            check("inherited file descriptor above child RLIMIT_NOFILE is closed",
                  high_sentinel_fd > 64 and "file=EBADF high_file=EBADF" in fd_stdout,
                  f"fd={high_sentinel_fd} result={fd_data!r}")
            check("child stdio remains usable after inherited FD cleanup",
                  fd_data.get("exit_code") == 0 and
                  "stdin=usable stdout=usable stderr=usable" in fd_stdout and
                  fd_data.get("stderr") == "fd-check-stderr\n",
                  repr(fd_data))
        finally:
            inherited_socket_a.close()
            inherited_socket_b.close()
            os.close(high_sentinel_fd)
            os.close(sentinel_fd)

        cleanup_failure = run(
            runner,
            *runner_args(fixture_root),
            "--nofile", "3",
            "--", "./runner_probe", "--touch", "work/fd-cleanup-executed",
        )
        cleanup_failure_data = parse_result(cleanup_failure)
        check("FD enumeration failure reports exec error and prevents target exec",
              cleanup_failure.returncode == 127 and
              cleanup_failure_data.get("status") == "exec_error" and
              "exec:" in (cleanup_failure_data.get("setup_error") or "") and
              not (work / "fd-cleanup-executed").exists(),
              repr(cleanup_failure_data))

        fault_fd_path = td / "fault-inherited-fd"
        fault_fd = os.open(fault_fd_path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
        selected_fault_fd = fcntl.fcntl(fault_fd, fcntl.F_DUPFD, 20)
        try:
            control_marker = work / "fault-control-executed"
            control_env = os.environ.copy()
            control_env.pop("XTOS_RUNNER_FAULT_MODE", None)
            control_env["XTOS_RUNNER_FAULT_FD"] = str(selected_fault_fd)
            control = run(
                fault_runner, *runner_args(fixture_root), "--", "./runner_probe",
                "--touch-fd", f"work/{control_marker.name}", str(selected_fault_fd),
                pass_fds=(selected_fault_fd,), env=control_env,
            )
            control_data = parse_result(control)
            check("unfaulted wrapped runner executes target after closing inherited FD",
                  control.returncode == 0 and control_data.get("status") == "exit" and
                  control_marker.read_text(encoding="utf-8") == "executed\n",
                  repr(control_data))

            for mode, expected_error in (
                ("close_eintr", "Interrupted system call"),
                ("close_eio", "Input/output error"),
                ("closedir_eio", "Input/output error"),
            ):
                marker = work / f"fault-{mode}-executed"
                fault_env = os.environ.copy()
                fault_env["XTOS_RUNNER_FAULT_MODE"] = mode
                fault_env["XTOS_RUNNER_FAULT_FD"] = str(selected_fault_fd)
                fault_result = run(
                    fault_runner, *runner_args(fixture_root), "--", "./runner_probe", "--touch", f"work/{marker.name}",
                    pass_fds=(selected_fault_fd,), env=fault_env,
                )
                fault_data = parse_result(fault_result)
                setup_error = fault_data.get("setup_error") or ""
                check(f"{mode} cleanup error blocks target exec",
                      fault_result.returncode == 127 and
                      fault_data.get("status") == "exec_error" and
                      expected_error in setup_error and not marker.exists(),
                      repr(fault_data))
        finally:
            os.close(selected_fault_fd)
            os.close(fault_fd)

        root_sentinel = outside / "root-sentinel"
        root_sentinel.write_bytes(b"outside-root-sentinel\n")
        root_sentinel.chmod(0o644)
        escape_link = fixture_root / "outside-symlink"
        # proc's cwd magic-link traverses from the already-open cwd, so the
        # unconfined control cannot pass merely because /root is mode 0700.
        absolute_escape = "/proc/self/cwd/../outside/root-sentinel"
        escape_link.symlink_to(absolute_escape)
        root_directory_fd = os.open(outside, os.O_RDONLY | os.O_DIRECTORY)
        try:
            root_result = run(
                runner, *runner_args(fixture_root), "--", "./runner_probe",
                "--root-check", absolute_escape, "../outside/root-sentinel",
                "outside-symlink", str(root_directory_fd), "work/root-isolated",
                pass_fds=(root_directory_fd,),
            )
            root_data = parse_result(root_result)
            check("absolute parent symlink and inherited directory escapes are blocked",
                  root_result.returncode == 0 and
                  "absolute=BLOCKED parent=BLOCKED symlink=BLOCKED fd=EBADF" in root_data.get("stdout", "") and
                  (work / "root-isolated").is_file() and
                  (work / "root-isolated").read_bytes() == b"root-isolated\n" and
                  root_sentinel.read_bytes() == b"outside-root-sentinel\n",
                  repr(root_data))
        finally:
            os.close(root_directory_fd)

        write_result = run(runner, *runner_args(fixture_root), "--", "/runner_probe", "--write-check")
        write_data = parse_result(write_result)
        check("only allowlisted directories permit write create and truncate",
              write_result.returncode == 0 and
              "allowed=OPEN denied=BLOCKED truncate=BLOCKED" in write_data.get("stdout", "") and
              (work / "allowed").read_bytes() == b"allowed\n" and
              not (not_allowed / "denied").exists() and existing.read_bytes() == b"keep-existing\n",
              repr(write_data))

        (fixture_root / "write-link").symlink_to("not-allowed", target_is_directory=True)
        for number, invalid_write_dir in enumerate(("/", "/work/..", "/work/../not-allowed",
                                                     "//work", "work", "/write-link", "/absent")):
            marker = work / f"bad-write-rule-{number}"
            invalid_rule = run(runner, *runner_args(fixture_root),
                               "--write-dir", invalid_write_dir, "--", "/runner_probe",
                               "--touch", f"/work/{marker.name}")
            invalid_data = parse_result(invalid_rule)
            check(f"invalid write allowlist {invalid_write_dir!r} prevents exec",
                  invalid_rule.returncode == 125 and invalid_data.get("status") == "setup_error" and
                  "filesystem write policy" in (invalid_data.get("setup_error") or "") and
                  not marker.exists(), repr(invalid_data))

        no_write_args = runner_args(fixture_root)
        i = no_write_args.index("--write-dir")
        del no_write_args[i:i + 2]
        readonly_marker = work / "readonly-default"
        readonly = run(runner, *no_write_args, "--", "/runner_probe", "--touch", "/work/readonly-default")
        readonly_data = parse_result(readonly)
        check("no write allowlist defaults to read-only even in uid-owned directory",
              readonly.returncode == 91 and readonly_data.get("status") == "exit" and
              not readonly_marker.exists(), repr(readonly_data))

        for mode, stage in (("chroot_eperm", "filesystem root setup"),
                            ("landlock_enosys", "filesystem write policy"),
                            ("landlock_old_abi", "filesystem write policy"),
                            ("landlock_create_eio", "filesystem write policy"),
                            ("landlock_add_eio", "filesystem write policy"),
                            ("landlock_restrict_eio", "filesystem write policy")):
            marker = work / mode
            env = os.environ.copy()
            env["XTOS_RUNNER_FAULT_MODE"] = mode
            result = run(fault_runner, *runner_args(fixture_root), "--", "/runner_probe",
                         "--touch", f"/work/{mode}", env=env)
            data = parse_result(result)
            check(f"{mode} fails closed before exec",
                  result.returncode == 125 and data.get("status") == "setup_error" and
                  stage in (data.get("setup_error") or "") and not marker.exists(), repr(data))

        id_result = run(
            runner,
            *runner_args(fixture_root),
            "--timeout-ms", "1000",
            "--", "/runner_probe", "--uid",
        )
        id_data = parse_result(id_result)
        check("ordinary fixed command executes without shell",
              id_result.returncode == 0 and id_data.get("stdout") == "65534\n",
              repr(id_data))

        # Production-default safety: identity is never guessed from the host.
        missing_identity = run(
            runner,
            "--root", str(fixture_root),
            "--cwd", str(fixture_root),
            "--supp-groups-clear",
            "--",
            str(probe),
        )
        missing_data = parse_result(missing_identity)
        check("missing identity fails closed before fork",
              missing_identity.returncode == 2 and "identity" in missing_data.get("error", "").lower(),
              repr(missing_data))

        production = td / "xtos-runner-production"
        compile_binary(production, SRC)
        production_result = run(
            production,
            *runner_args(fixture_root),
            "--", "./runner_probe",
        )
        production_data = parse_result(production_result)
        check("production default rejects runtime identity overrides",
              production_result.returncode == 2 and production_data.get("status") == "error",
              repr(production_data))

        production_missing = run(
            production,
            *runner_args(fixture_root, include_identity=False),
            "--", "./runner_probe",
        )
        production_missing_data = parse_result(production_missing)
        check("production without configured identity fails closed",
              production_missing.returncode == 2 and
              "identity" in production_missing_data.get("error", "").lower(),
              repr(production_missing_data))

        configured = td / "xtos-runner-configured"
        compile_binary(configured, SRC, "-DXTOS_RUNNER_UID=65534", "-DXTOS_RUNNER_GID=65534")
        configured_result = run(
            configured,
            *runner_args(fixture_root, include_identity=False),
            "--", "./runner_probe",
        )
        configured_data = parse_result(configured_result)
        check("compile-time production identity executes without runtime uid/gid",
              configured_result.returncode == 0 and
              "uid=65534 euid=65534 gid=65534 egid=65534" in configured_data.get("stdout", ""),
              repr(configured_data))

        # Real production identity and unwrapped syscalls: DAC permits both
        # directories, while Landlock must authorize only /work.
        for directory in (work, not_allowed):
            for leaf in ("delete-at", "delete-legacy"):
                target = directory / leaf
                target.write_bytes(b"preserve-delete\n")
                os.chown(target, 65534, 65534)
            for leaf in ("remove-at", "remove-legacy"):
                target = directory / leaf
                target.mkdir()
                os.chown(target, 65534, 65534)
        for directory, allowed in ((work, True), (not_allowed, False)):
            mutation = run(configured, *runner_args(fixture_root, include_identity=False),
                           "--", "/runner_probe", "--directory-ops", f"/{directory.name}")
            mutation_data = parse_result(mutation)
            rows = {line.split("=", 1)[0]: line.split("=", 1)[1]
                    for line in mutation_data.get("stdout", "").splitlines()}
            operations = {"mkdirat": "new-at", "unlinkat": "delete-at",
                          "unlinkat-dir": "remove-at", "mkdir": "new-legacy",
                          "unlink": "delete-legacy", "rmdir": "remove-legacy"}
            states_ok = True
            for name, leaf in operations.items():
                if name not in rows:
                    states_ok &= name not in ("mkdirat", "unlinkat", "unlinkat-dir")
                    continue  # Legacy syscall numbers do not exist on AArch64.
                target = directory / leaf
                if name.startswith("mkdir"):
                    states_ok &= target.is_dir() if allowed else not target.exists()
                else:
                    states_ok &= not target.exists() if allowed else target.exists()
                    if not allowed and name in ("unlink", "unlinkat") and target.is_file():
                        states_ok &= target.read_bytes() == b"preserve-delete\n"
                states_ok &= (rows[name] == "0 errno=0" if allowed else
                              rows[name].startswith("-1 errno="))
            check(f"directory syscalls {'succeed in /work' if allowed else 'deny /not-allowed and preserve originals'}",
                  mutation.returncode == 0 and states_ok, repr(mutation_data))
            check(f"link symlink rename and mknod stay blocked in /{directory.name}",
                  all(rows.get(name) == "-1 errno=1" for name in
                      ("symlinkat", "linkat", "mknodat", "renameat")) and
                  all(not (directory / leaf).exists() and not (directory / leaf).is_symlink()
                      for leaf in ("new-link", "hard-link", "new-fifo", "renamed")) and
                  (work / "allowed").read_bytes() == b"allowed\n",
                  repr(mutation_data))

        original = b"preserve-truncation\n"
        for directory in (work, not_allowed):
            for leaf in ("ftruncate", "truncate"):
                target = directory / leaf
                target.write_bytes(original)
                os.chown(target, 65534, 65534)
            truncated = run(configured, *runner_args(fixture_root, include_identity=False),
                            "--", "/runner_probe", "--truncate-ops", f"/{directory.name}")
            truncated_data = parse_result(truncated)
            rows = {line.split("=", 1)[0]: line.split("=", 1)[1]
                    for line in truncated_data.get("stdout", "").splitlines()}
            allowed = directory == work
            check(f"real ftruncate {'succeeds in /work' if allowed else 'denies /not-allowed and preserves data'}",
                  truncated.returncode == 0 and
                  (rows.get("ftruncate") == "0 errno=0" if allowed else
                   rows.get("write-open", "").startswith("-1 errno=") and
                   rows.get("ftruncate", "").startswith("-1 errno=")) and
                  (directory / "ftruncate").read_bytes() == (original[:3] if allowed else original),
                  repr(truncated_data))
            check(f"real truncate {'succeeds in /work' if allowed else 'denies /not-allowed and preserves data'}",
                  truncated.returncode == 0 and
                  (rows.get("truncate") == "0 errno=0" if allowed else
                   rows.get("truncate", "").startswith("-1 errno=")) and
                  (directory / "truncate").read_bytes() == (original[:4] if allowed else original),
                  repr(truncated_data))

        for mode, expected_stage in (
            ("NO_NEW_PRIVS", "no_new_privs"),
            ("SECCOMP", "seccomp"),
        ):
            failing_runner = td / f"runner-fail-{mode.lower()}"
            compile_binary(failing_runner, SRC, "-DXTOS_RUNNER_TEST_MODE=1",
                           f"-DXTOS_RUNNER_TEST_FAIL_{mode}=1")
            failure_result = run(
                failing_runner, *runner_args(fixture_root), "--",
                "./runner_probe", "--touch", f"work/{mode.lower()}-executed",
            )
            failure_data = parse_result(failure_result)
            check(f"{expected_stage} setup failure prevents exec",
                  failure_result.returncode == 125 and
                  failure_data.get("status") == "setup_error" and
                  expected_stage in (failure_data.get("setup_error") or "") and
                  not (work / f"{mode.lower()}-executed").exists(),
                  repr(failure_data))

        outside_cwd = run(
            runner,
            *runner_args(fixture_root),
            "--cwd", str(outside),
            "--",
            str(probe),
        )
        outside_data = parse_result(outside_cwd)
        check("cwd outside the root allowlist is rejected",
              outside_cwd.returncode == 2 and "cwd" in outside_data.get("error", "").lower(),
              repr(outside_data))

        timeout_result = run(
            runner,
            *runner_args(fixture_root),
            "--timeout-ms", "50",
            "--",
            "/runner_probe",
            "--sleep",
            timeout=10,
        )
        timeout_data = parse_result(timeout_result)
        check("timeout kills and reaps the child",
              timeout_result.returncode == 124 and timeout_data.get("timed_out") is True and
              timeout_data.get("status") == "timeout",
              repr(timeout_data))

    if failures:
        print(f"FAIL: {len(failures)}")
        for failure in failures:
            print("  " + failure)
        return 1
    print("ALL PASS (runner fixture; no namespace or host policy mutation)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
