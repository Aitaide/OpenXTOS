"""Trusted root broker adapter for the native runner.

The root-admin deployed root, cwd, and target executable are trusted inputs; their
file contents are not pinned. The runner executable inode itself is pinned.
"""
import json
import os
import stat
import subprocess
import sys
from types import MappingProxyType

RUNNER = "/usr/libexec/xtos/runner"
RUNNER_UID = RUNNER_GID = 62000
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


def _safe_stat(info, *, owner=0, directory=False, executable=False):
    kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    return (kind and info.st_uid == owner and not info.st_mode & 0o022
            and not info.st_mode & 0o7000
            and (not executable or info.st_mode & 0o111))


def _open_path(path, *, directory, final_owner=0):
    """Walk an absolute canonical path one component at a time without symlinks."""
    if type(path) is not str or not path.startswith("/") or path.startswith("//"):
        raise ValueError("invalid absolute path")
    parts = [] if path == "/" else path.split("/")[1:]
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("non-canonical path")
    if not parts:
        if directory:
            fd = os.open("/", _DIR_FLAGS)
            if _safe_stat(os.fstat(fd), owner=final_owner, directory=True):
                return fd
            os.close(fd)
        raise ValueError("non-canonical path")
    fd = os.open("/", _DIR_FLAGS)
    try:
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            flags = (_DIR_FLAGS if directory or not last else _FILE_FLAGS)
            nxt = os.open(part, flags, dir_fd=fd)
            old_fd, fd = fd, nxt  # Transfer ownership before Linux close can fail.
            os.close(old_fd)
            info = os.fstat(fd)
            if not _safe_stat(info, owner=final_owner if last else 0,
                              directory=(directory or not last)):
                raise ValueError("unsafe path component")
        return fd
    except BaseException as primary_error:
        try:
            os.close(fd)
        except OSError as exc:
            primary_error.add_note(f"descriptor cleanup also failed: {exc}")
        raise


def _assert_frozen(request):
    if (type(request) is not MappingProxyType or type(request.get("argv")) is not tuple
            or type(request.get("writable_dirs")) is not tuple
            or type(request.get("limits")) is not MappingProxyType
            or type(request.get("sandbox_policy")) is not MappingProxyType):
        raise ValueError("immutable validated request required")
    if request.get("schema") != "xtos.action.v1":
        raise ValueError("validated action schema required")
    if type(request.get("uid")) is not int or type(request.get("gid")) is not int:
        raise ValueError("fixed runner identity required")
    if (request["uid"], request["gid"]) != (RUNNER_UID, RUNNER_GID):
        raise ValueError("runner identity is fixed")
    if request["sandbox_policy"].get("clear_groups") is not True:
        raise ValueError("supplementary group clearing is mandatory")


def _parse_runner_result(stdout, returncode):
    """Validate emit_result's outcome and the runner process's actual exit status."""
    try:
        response = json.loads(stdout)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("native runner returned invalid JSON result") from exc
    if type(response) is not dict:
        raise RuntimeError("native runner returned malformed result")
    status, code = response.get("status"), response.get("exit_code")
    timed_out, signum = response.get("timed_out"), response.get("signal")
    valid = (type(code) is int and 0 <= code <= 255
             and type(returncode) is int and returncode == code
             and type(timed_out) is bool and "signal" in response)
    if status == "exit":
        valid = valid and not timed_out and signum is None
    elif status == "signal":
        valid = (valid and not timed_out and type(signum) is int
                 and 1 <= signum < 65 and code == 128 + signum)
    elif status == "timeout":
        valid = valid and timed_out and signum is None and code == 124
    elif status in ("setup_error", "exec_error", "wait_error"):
        valid = (valid and not timed_out and signum is None
                 and code == (127 if status == "exec_error" else 125))
    else:
        valid = False
    if not valid:
        raise RuntimeError("native runner result contradicts process outcome")
    return response


class NativeRunnerExecutor:
    """Callable intended only as ActionGate.execute's trusted root callback."""
    def __call__(self, request):
        if os.geteuid() != 0:
            raise PermissionError("native action broker requires effective uid 0")
        _assert_frozen(request)
        root_path, cwd_path = request["root"], request["cwd"]
        root_fd = cwd_fd = runner_fd = target_fd = None
        try:
            root_fd = _open_path(root_path, directory=True)
            cwd_fd = _open_path(cwd_path, directory=True)
            target = request["argv"][0]
            if not target.startswith("/") or target.startswith("//"):
                raise ValueError("target executable must be absolute")
            relative = target.lstrip("/")
            target_path = root_path.rstrip("/") + "/" + relative
            for writable in request["writable_dirs"]:
                if writable == "/" or not writable.startswith("/"):
                    raise ValueError("root-wide writable grant refused")
                if relative == writable.lstrip("/") or relative.startswith(writable.lstrip("/") + "/"):
                    raise ValueError("executable is inside a writable directory")
                write_fd = _open_path(root_path.rstrip("/") + writable, directory=True,
                                      final_owner=RUNNER_UID)
                os.close(write_fd)
            target_fd = _open_path(target_path, directory=False)
            if not _safe_stat(os.fstat(target_fd), owner=0, executable=True):
                raise ValueError("unsafe target executable")
            runner_fd = _open_path(RUNNER, directory=False)
            if not _safe_stat(os.fstat(runner_fd), owner=0, executable=True):
                raise ValueError("unsafe runner executable")
            runner_path = f"/proc/self/fd/{runner_fd}"
            limits = request["limits"]
            policy = request["sandbox_policy"]
            argv = [runner_path, "--root", root_path, "--cwd", cwd_path,
                    "--supp-groups-clear", "--cpu-sec", str(limits["cpu_sec"]),
                    "--as-bytes", str(limits["as_bytes"]), "--nofile", str(limits["nofile"]),
                    "--nproc", str(limits["nproc"]), "--timeout-ms", str(request["timeout_ms"])]
            for directory in request["writable_dirs"]:
                argv.extend(["--write-dir", directory])
            for field in ("deny_socket", "deny_getpid"):
                if policy[field]:
                    argv.append("--" + field.replace("_", "-"))
            argv.extend(["--", *request["argv"]])
            result = subprocess.run(argv, executable=runner_path, pass_fds=(runner_fd,),
                                    env={"LANG": "C", "LC_ALL": "C"}, close_fds=True,
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                    timeout=request["timeout_ms"] / 1000 + 5, check=False)
            return _parse_runner_result(result.stdout, result.returncode)
        finally:
            primary_error = sys.exc_info()[1]
            cleanup_error = None
            for fd in (target_fd, runner_fd, cwd_fd, root_fd):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError as exc:
                        if cleanup_error is None:
                            cleanup_error = exc
            if cleanup_error is not None:
                if primary_error is None:
                    raise cleanup_error
                primary_error.add_note(f"descriptor cleanup also failed: {cleanup_error}")
