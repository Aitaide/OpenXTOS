"""Bounded AF_UNIX JSON-lines inference service."""
from __future__ import annotations

import ctypes
import errno
import fcntl
import json
import math
import os
from pathlib import Path
import secrets
import socketserver
import stat
import threading
import time
import weakref
from numbers import Real
from typing import Any, TYPE_CHECKING

from .protocol import ManifestError, RequestError, validate_manifest
from . import RUNTIME_VERSION
from .trust import AuthorizedModel, FailClosedPolicy, TrustError

if TYPE_CHECKING:
    from .worker import Worker


SCHEMA = "xtos-ai-runtime-v1"
MAX_REQUEST_BYTES = 65536
MAX_JSON_DEPTH = 32
MAX_CONNECTIONS = 8
MAX_MODELS = 4
SHUTDOWN_TIMEOUT = 5.0
TRUST_WATCH_INTERVAL = 0.25
SERVER_CLOSE_LOCK_TIMEOUT = 0.1
_UMASK_LOCK = threading.RLock()


class _WorkerCloseState:
    """One shared close attempt; a successful result remains terminal."""

    def __init__(self):
        self.done = threading.Event()
        self.error = None


def _rename_noreplace(source: str, source_fd: int, target: str, target_fd: int) -> None:
    """Atomically restore a quarantined directory entry without clobbering a new path."""
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except (AttributeError, OSError):
        raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable") from None
    renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    renameat2.restype = ctypes.c_int
    result = renameat2(source_fd, os.fsencode(source), target_fd, os.fsencode(target), 1)  # RENAME_NOREPLACE
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), target)


def _cleanup_token(name: str, prefix: str = ".xtos-socket-cleanup-") -> str | None:
    if not name.startswith(prefix):
        return None
    token = name[len(prefix):]
    if len(token) != 32 or any(char not in "0123456789abcdef" for char in token):
        return None
    return token


def _socket_identity(info):
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))


def _cleanup_lock(parent_fd: int) -> int:
    lock_fd = os.dup(parent_fd)
    deadline = time.monotonic() + SERVER_CLOSE_LOCK_TIMEOUT
    try:
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return lock_fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("socket cleanup is already in progress") from None
                time.sleep(.005)
    except BaseException:
        os.close(lock_fd)
        raise


def _close_cleanup_lock(lock_fd: int) -> None:
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)


def _cleanup_record(token: str, target: str, identity: tuple[int, int, int],
                    quarantine_identity: tuple[int, int]) -> bytes:
    value = {"device": identity[0], "inode": identity[1], "target": target,
             "token": token, "type": identity[2], "uid": os.geteuid(), "version": 1,
             "quarantine_device": quarantine_identity[0],
             "quarantine_inode": quarantine_identity[1]}
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


def _write_cleanup_state(parent_fd: int, state_name: str, record: bytes) -> None:
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    with _UMASK_LOCK:
        previous_umask = os.umask(0o077)
        try:
            fd = os.open(state_name, flags, 0o600, dir_fd=parent_fd)
        finally:
            os.umask(previous_umask)
    try:
        view = memoryview(record)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError(errno.EIO, "short socket cleanup state write")
            view = view[written:]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        os.unlink(state_name, dir_fd=parent_fd)
        raise
    else:
        os.close(fd)
    os.fsync(parent_fd)


def _read_cleanup_state(parent_fd: int, state_name: str, token: str) -> dict[str, Any]:
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(state_name, flags, dir_fd=parent_fd)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size > 4096):
            raise RuntimeError("socket cleanup state is not a private service-owned file")
        raw = os.read(fd, 4097)
        # Reuse the protocol's bounded, duplicate-key-free JSON parser.
        value = parse_request_line(raw + b"\n")
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError("socket cleanup state is ambiguous") from exc
    finally:
        os.close(fd)
    required = {"device", "inode", "target", "token", "type", "uid", "version",
                "quarantine_device", "quarantine_inode"}
    if type(value) is not dict or set(value) != required:
        raise RuntimeError("socket cleanup state is ambiguous")
    if any(type(value[key]) is not int for key in
           ("device", "inode", "type", "uid", "version", "quarantine_device", "quarantine_inode")):
        raise RuntimeError("socket cleanup state is ambiguous")
    target = value["target"]
    if (value["token"] != token or value["uid"] != os.geteuid() or value["version"] != 1
            or type(target) is not str or target in ("", ".", "..")
            or Path(target).name != target or os.sep in target):
        raise RuntimeError("socket cleanup state is ambiguous")
    return value


def _private_quarantine_fd(parent_fd: int, name: str) -> int:
    flags = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    qfd = os.open(name, flags, dir_fd=parent_fd)
    try:
        info = os.fstat(qfd)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise PermissionError("socket cleanup quarantine is not private and service-owned")
        return qfd
    except BaseException:
        os.close(qfd)
        raise


def _remove_constructor_quarantine(parent_fd: int, quarantine_name: str, state_name: str,
                                   record: dict[str, Any], qfd: int, detached: bool = False) -> None:
    qinfo = os.fstat(qfd)
    if (qinfo.st_dev, qinfo.st_ino) != (record["quarantine_device"], record["quarantine_inode"]):
        raise RuntimeError("socket cleanup quarantine changed identity")
    identity = (record["device"], record["inode"], record["type"])
    entries = set(os.listdir(qfd))
    if entries - {"owner", "entry"}:
        raise RuntimeError("socket cleanup quarantine contains an unrelated entry")
    try:
        owner_info = os.stat("owner", dir_fd=qfd, follow_symlinks=False)
    except FileNotFoundError:
        owner_info = None
    if owner_info is not None and (_socket_identity(owner_info) != identity
                                   or not stat.S_ISSOCK(owner_info.st_mode)):
        raise RuntimeError("socket cleanup ownership anchor changed identity")
    if owner_info is None and ("entry" in entries or not detached):
        raise RuntimeError("socket cleanup entry has no stable ownership anchor")

    try:
        entry_info = os.stat("entry", dir_fd=qfd, follow_symlinks=False)
    except FileNotFoundError:
        entry_info = None
    if entry_info is None and owner_info is not None and not detached:
        try:
            public_info = os.stat(record["target"], dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            public_info = None
        if public_info is not None and (_socket_identity(public_info) == identity
                                        and stat.S_ISSOCK(public_info.st_mode)):
            try:
                os.rename(record["target"], "entry", src_dir_fd=parent_fd, dst_dir_fd=qfd)
            except FileNotFoundError:
                entry_info = None
            else:
                entry_info = os.stat("entry", dir_fd=qfd, follow_symlinks=False)
    if entry_info is not None:
        if _socket_identity(entry_info) == identity and stat.S_ISSOCK(entry_info.st_mode):
            if not detached:
                _rename_noreplace(state_name, parent_fd, state_name + ".detached", parent_fd)
                state_name += ".detached"
                detached = True
                os.fsync(parent_fd)
            os.unlink("entry", dir_fd=qfd)
        else:
            if detached:
                raise RuntimeError("detached socket cleanup entry changed identity")
            _rename_noreplace("entry", qfd, record["target"], parent_fd)
    if "entry" in os.listdir(qfd):
        raise RuntimeError("socket cleanup retained an unrecognized entry")
    if not detached:
        _rename_noreplace(state_name, parent_fd, state_name + ".detached", parent_fd)
        state_name += ".detached"
        os.fsync(parent_fd)
    if owner_info is not None:
        os.unlink("owner", dir_fd=qfd)
    # The full identity record survives owner unlink, rmdir and record-unlink
    # errors. A detached record never revisits the public socket pathname.
    os.rmdir(quarantine_name, dir_fd=parent_fd)
    os.fsync(parent_fd)
    os.unlink(state_name, dir_fd=parent_fd)
    os.fsync(parent_fd)


def _recover_socket_cleanups(parent: Path) -> None:
    parent_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_CLOEXEC", 0))
    lock_fd = -1
    try:
        lock_fd = _cleanup_lock(parent_fd)
        states = {}
        prefix = ".xtos-socket-state-"
        for name in os.listdir(parent_fd):
            detached = name.endswith(".detached")
            token = _cleanup_token(name[:-9] if detached else name, prefix)
            if token is None:
                continue
            if token in states:
                raise RuntimeError("socket cleanup state has conflicting phases")
            states[token] = (name, detached)
        for token, (state_name, detached) in sorted(states.items()):
            record = _read_cleanup_state(parent_fd, state_name, token)
            quarantine_name = ".xtos-socket-cleanup-" + token
            try:
                qfd = _private_quarantine_fd(parent_fd, quarantine_name)
            except FileNotFoundError:
                if not detached:
                    raise RuntimeError("socket cleanup state has no ownership quarantine") from None
                os.unlink(state_name, dir_fd=parent_fd)
                os.fsync(parent_fd)
                continue
            try:
                _remove_constructor_quarantine(parent_fd, quarantine_name, state_name,
                                               record, qfd, detached=detached)
            finally:
                os.close(qfd)
    finally:
        if lock_fd >= 0:
            _close_cleanup_lock(lock_fd)
        os.close(parent_fd)


def _unlink_constructor_socket(path: Path, reference_fd: int,
                               quarantine_name: str | None = None) -> None:
    """Persist constructor cleanup before releasing its last stable reference."""
    if reference_fd < 0:
        raise OSError(errno.ENOTSUP, "cannot safely identify Unix socket")
    path = Path(path)
    token = _cleanup_token(quarantine_name or ".xtos-socket-cleanup-" + secrets.token_hex(16))
    if token is None:
        raise ValueError("invalid socket cleanup quarantine name")
    quarantine_name = ".xtos-socket-cleanup-" + token
    state_name = ".xtos-socket-state-" + token
    parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_CLOEXEC", 0))
    lock_fd = -1
    qfd = -1
    state_created = False
    try:
        lock_fd = _cleanup_lock(parent_fd)
        ref_info = os.fstat(reference_fd)
        identity = _socket_identity(ref_info)
        if not stat.S_ISSOCK(ref_info.st_mode):
            raise OSError(errno.ENOTSUP, "stable reference is not a Unix socket")
        try:
            public_info = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if _socket_identity(public_info) != identity or not stat.S_ISSOCK(public_info.st_mode):
            return

        with _UMASK_LOCK:
            previous_umask = os.umask(0o077)
            try:
                os.mkdir(quarantine_name, 0o700, dir_fd=parent_fd)
            finally:
                os.umask(previous_umask)
        qfd = _private_quarantine_fd(parent_fd, quarantine_name)
        qinfo = os.fstat(qfd)
        record_bytes = _cleanup_record(token, path.name, identity, (qinfo.st_dev, qinfo.st_ino))
        _write_cleanup_state(parent_fd, state_name, record_bytes)
        state_created = True
        os.link(f"/proc/self/fd/{reference_fd}", "owner", dst_dir_fd=qfd,
                follow_symlinks=True)
        owner_info = os.stat("owner", dir_fd=qfd, follow_symlinks=False)
        if _socket_identity(owner_info) != identity or not stat.S_ISSOCK(owner_info.st_mode):
            raise RuntimeError("socket cleanup anchor does not match the held socket")
        os.fsync(qfd)
        os.fsync(parent_fd)
        try:
            os.rename(path.name, "entry", src_dir_fd=parent_fd, dst_dir_fd=qfd)
        except FileNotFoundError:
            pass
        else:
            entry_info = os.stat("entry", dir_fd=qfd, follow_symlinks=False)
            if _socket_identity(entry_info) != identity or not stat.S_ISSOCK(entry_info.st_mode):
                _rename_noreplace("entry", qfd, path.name, parent_fd)
        record = _read_cleanup_state(parent_fd, state_name, token)
        _remove_constructor_quarantine(parent_fd, quarantine_name, state_name, record, qfd)
        state_created = False
    except BaseException:
        if qfd >= 0 and not state_created:
            try:
                if not os.listdir(qfd):
                    os.rmdir(quarantine_name, dir_fd=parent_fd)
            except OSError:
                pass
        raise
    finally:
        if qfd >= 0:
            os.close(qfd)
        if lock_fd >= 0:
            _close_cleanup_lock(lock_fd)
        os.close(parent_fd)


def _unlink_socket_with_reference(path: Path, reference_fd: int,
                                 quarantine_name: str | None = None) -> None:
    """Remove only the socket entry proven to match a held stable reference."""
    if reference_fd < 0:
        raise OSError(errno.ENOTSUP, "cannot safely identify Unix socket")
    path = Path(path)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    parent_fd = os.open(path.parent, directory_flags)
    if quarantine_name is None:
        quarantine_name = ".xtos-socket-cleanup-" + secrets.token_hex(16)
    quarantine_fd = -1
    moved = False
    try:
        try:
            with _UMASK_LOCK:
                previous_umask = os.umask(0o077)
                try:
                    os.mkdir(quarantine_name, 0o700, dir_fd=parent_fd)
                finally:
                    os.umask(previous_umask)
        except FileExistsError:
            # A prior cleanup attempt may have left its entry here, or may
            # have removed the entry but failed to remove this directory.
            pass
        quarantine_fd = os.open(quarantine_name, directory_flags | getattr(os, "O_NOFOLLOW", 0),
                                dir_fd=parent_fd)
        reference_info = os.fstat(reference_fd)
        try:
            info = os.stat("entry", dir_fd=quarantine_fd, follow_symlinks=False)
        except FileNotFoundError:
            try:
                os.rename(path.name, "entry", src_dir_fd=parent_fd, dst_dir_fd=quarantine_fd)
                moved = True
            except FileNotFoundError:
                info = None
            else:
                info = os.stat("entry", dir_fd=quarantine_fd, follow_symlinks=False)

        if info is not None:
            # Keep an unresolved entry in quarantine if restoring it would
            # replace a newer public owner. The caller can still close its
            # own socket/reference without deleting somebody else's inode.
            moved = True
            reference_info = os.fstat(reference_fd)
            identity = (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))
            reference_identity = (reference_info.st_dev, reference_info.st_ino,
                                  stat.S_IFMT(reference_info.st_mode))
            if identity == reference_identity and stat.S_ISSOCK(info.st_mode):
                os.unlink("entry", dir_fd=quarantine_fd)
                moved = False
            else:
                _rename_noreplace("entry", quarantine_fd, path.name, parent_fd)
                moved = False
    finally:
        try:
            if quarantine_fd >= 0:
                os.close(quarantine_fd)
            if not moved:
                os.rmdir(quarantine_name, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)


def parse_request_line(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
        raise ValueError("request exceeds size limit or lacks newline")
    text = raw.decode("utf-8", errors="strict")
    depth = 0
    in_string = escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise ValueError("JSON nesting limit exceeded")
        elif char in "]}":
            depth -= 1

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("non-finite JSON number")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non-finite JSON number")
        return number

    value = json.loads(text, object_pairs_hook=pairs, parse_constant=reject_constant,
                       parse_float=finite_float)
    if type(value) is not dict:
        raise ValueError("request must be a JSON object")
    return value


def _valid_id(value: Any) -> bool:
    return type(value) is str and 1 <= len(value) <= 128


def _envelope(request_id: Any, status: str, reason: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "id": request_id if _valid_id(request_id) else None,
        "status": status,
        "safe_action": "no_action",
        "reason": reason,
        "output": None,
    }


class InferenceService:
    """A single active inference and one waiting request across loaded models."""

    def __init__(self, model_root: Path, python_command=None, max_inflight: int = 2,
                 worker_factory=None, *, trust_policy=None):
        if type(max_inflight) is not int or max_inflight != 2:
            raise ValueError("P1 service is fixed to one active and one queued request")
        self.model_root = Path(model_root).resolve()
        self.python_command = python_command
        self._worker_factory = worker_factory
        self.trust_policy = trust_policy if trust_policy is not None else FailClosedPolicy()
        self._worker_tickets = {}
        self._deny_reasons = {}
        self._inflight_tickets = {}
        self._request_denials = {}
        self._models: dict[str, Worker] = {}
        self._condition = threading.Condition(threading.RLock())
        self._inflight: dict[str, threading.Event] = {}
        self._active_id: str | None = None
        self._queued_id: str | None = None
        self._worker_users: dict[Worker, int] = {}
        self._retired: set[Worker] = set()
        self._staging: dict[Worker, threading.Event] = {}
        self._recovering: dict[Worker, threading.Event] = {}
        self._recovery_threads: dict[Worker, threading.Thread] = {}
        self._dispatch_threads: set[threading.Thread] = set()
        self._recovery_errors: dict[Worker, str] = {}
        self._management = threading.Lock()
        # Trust I/O never runs while this admission gate is held. It orders
        # the non-I/O failure publication with worker factory/start entry.
        self._admission_lock = threading.Lock()
        self._closed = False
        self._shutdown_error: str | None = None
        self._shutdown_lock = threading.Lock()
        self._shutdown_close_threads: dict[Worker, threading.Thread] = {}
        self._shutdown_close_errors: dict[Worker, str] = {}
        self._close_states: dict[Worker, _WorkerCloseState] = {}
        self._closed_workers = weakref.WeakSet()
        self._retirement_states: dict[Worker, _WorkerCloseState] = {}
        self._shutdown_deadline: float | None = None
        self._watch_stop = threading.Event()
        self._watch_thread = threading.Thread(target=self._trust_watch, daemon=True,
                                              name="xtos-ai-trust-watch")
        self._watch_threads = {self._watch_thread}
        self._trust_status = getattr(self.trust_policy, "status", "ok")
        self._trust_error = None
        self._trust_failure = None
        self._watch_thread.start()

    def _load(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self._management.acquire(blocking=False):
            return _envelope(request.get("id"), "busy", "model_change_in_progress")
        try:
            return self._stage(request)
        finally:
            self._management.release()

    def _new_worker(self, authorized_model):
        if self._worker_factory is not None:
            return self._worker_factory(authorized_model)
        from .worker import Worker
        return Worker(authorized_model, python_command=self.python_command)

    def _retire_ticket(self, ticket):
        retire = getattr(self.trust_policy, "retire", None)
        if retire is not None:
            retire(ticket)

    def _release_closed_ticket(self, worker, deadline=None):
        if deadline is None:
            deadline = time.monotonic() + SHUTDOWN_TIMEOUT
        with self._condition:
            if (worker not in self._closed_workers or self._worker_users.get(worker, 0)
                    or worker in self._staging or worker in self._recovering):
                return None
            ticket = self._worker_tickets.get(worker)
            if ticket is None:
                self._deny_reasons.pop(worker, None)
                self._recovery_errors.pop(worker, None)
                return None
            state = self._retirement_states.get(worker)
            if state is None or (state.done.is_set() and state.error is not None):
                state = _WorkerCloseState()
                self._retirement_states[worker] = state
                def retirement_owner():
                    try:
                        self._retire_ticket(ticket)
                    except BaseException as exc:
                        state.error = exc
                    with self._condition:
                        if state.error is None and self._worker_tickets.get(worker) is ticket:
                            self._worker_tickets.pop(worker, None)
                            self._deny_reasons.pop(worker, None)
                            self._recovery_errors.pop(worker, None)
                            self._retirement_states.pop(worker, None)
                            self._retired.discard(worker)
                            self._shutdown_close_errors.pop(worker, None)
                        elif state.error is not None:
                            self._retired.add(worker)
                            self._shutdown_close_errors[worker] = str(state.error)[:512]
                        state.done.set()
                        self._condition.notify_all()
                threading.Thread(target=retirement_owner, daemon=True,
                                 name="xtos-ai-ticket-retire").start()
        if not state.done.wait(max(0.0, deadline - time.monotonic())):
            with self._condition:
                if not state.done.is_set():
                    error = TimeoutError("ticket retirement timed out")
                    self._retired.add(worker)
                    self._shutdown_close_errors[worker] = str(error)
                    return error
        return state.error

    def _safe_close(self, worker, deadline=None):
        # One actual close owner per worker; callers only wait a bounded time.
        if deadline is None:
            deadline = time.monotonic() + SHUTDOWN_TIMEOUT
        with self._condition:
            closed = worker in self._closed_workers
            if closed:
                self._retired.discard(worker)
                self._shutdown_close_errors.pop(worker, None)
            state = self._close_states.get(worker)
            if not closed and (state is None or (state.done.is_set() and state.error is not None)):
                state = _WorkerCloseState()
                self._close_states[worker] = state
                def close_owner():
                    error = None
                    try:
                        worker.close()
                        with self._condition:
                            self._closed_workers.add(worker)
                    except BaseException as exc:
                        error = exc
                    with self._condition:
                        state.error = error
                        if error is None:
                            self._closed_workers.add(worker)
                            self._close_states.pop(worker, None)
                            self._retired.discard(worker)
                            self._shutdown_close_errors.pop(worker, None)

                        else:
                            self._retired.add(worker)
                            self._shutdown_close_errors[worker] = str(error)[:512]
                        state.done.set()
                        self._condition.notify_all()
                    if error is None:
                        self._release_closed_ticket(worker)
                threading.Thread(target=close_owner, daemon=True,
                                 name="xtos-ai-worker-close").start()
        if closed:
            return self._release_closed_ticket(worker, deadline)
        if not state.done.wait(max(0.0, deadline - time.monotonic())):
            with self._condition:
                if not state.done.is_set():
                    self._retired.add(worker)
                    return TimeoutError("worker close timed out")
        if state.error is not None and not isinstance(state.error, Exception):
            raise state.error
        if state.error is not None:
            return state.error
        return self._release_closed_ticket(worker, deadline)

    def _stage(self, request):
        with self._condition:
            if self._trust_failure:
                return _envelope(request.get("id"), "unavailable", self._trust_failure)
        raw = request.get("manifest")
        try:
            ticket = self.trust_policy.authorize(raw, self.model_root)
            manifest = ticket.manifest
        except TrustError as error:
            with self._condition:
                self._trust_status, self._trust_error = error.reason, error.reason
            return _envelope(request.get("id"), "unavailable" if error.reason.startswith("policy_") else "invalid", error.reason)
        except (ManifestError, TypeError, ValueError):
            return _envelope(request.get("id"), "invalid", "invalid_manifest")
        candidate = None
        installed = False
        cancel = threading.Event()
        try:
            # Authorization above does trust I/O outside both locks. Once the
            # gate is held, a fatal watcher result cannot commit before this
            # stage's final pre-factory/pre-start refusal check.
            with self._admission_lock:
                with self._condition:
                    refusal = self._trust_failure
                    if refusal is None and ticket.deny_event.is_set():
                        refusal = "model_revoked"
                    if refusal is None and self._closed:
                        refusal = "service_closed"
                    if (refusal is None and manifest.name not in self._models
                            and len(self._models) >= MAX_MODELS):
                        refusal = "model_registry_full"
                if refusal is None:
                    candidate = self._new_worker(ticket)
                    with self._condition:
                        self._worker_tickets[candidate] = ticket
                        refusal = self._trust_failure
                        if refusal:
                            ticket.deny_event.set()
                            self._deny_reasons[candidate] = refusal
                            self._retired.add(candidate)
                        elif ticket.deny_event.is_set():
                            refusal = self._deny_reasons.get(candidate, "model_revoked")
                            self._deny_reasons[candidate] = refusal
                            self._retired.add(candidate)
                        elif self._closed:
                            refusal = "service_closed"
                        else:
                            self._staging[candidate] = cancel
                    if refusal is None:
                        health = candidate.start(cancel=cancel)
            if refusal:
                return _envelope(request.get("id"), "unavailable", refusal)
            reason = self._check_ticket(ticket)
            denial = TrustError(reason) if reason else None
            with self._condition:
                self._staging.pop(candidate, None)
                if denial is None and (ticket.deny_event.is_set() or self._trust_failure):
                    denial = TrustError(self._deny_reasons.get(candidate, self._trust_failure or "model_revoked"))
                if denial is not None:
                    self._deny_reasons[candidate] = denial.reason
                    ticket.deny_event.set()
                    self._retired.add(candidate)
                if denial is not None or self._closed or cancel.is_set():
                    installed = False
                    old, close_old = None, False
                else:
                    old = self._models.get(manifest.name)
                    self._models[manifest.name] = candidate
                    self._trust_status, self._trust_error = getattr(self.trust_policy, "status", "ok"), None
                    if old is not None:
                        self._retired.add(old)
                        if old in self._recovering:
                            self._recovering[old].set()
                        close_old = self._worker_users.get(old, 0) == 0 and old not in self._recovering
                        if close_old:
                            self._retired.discard(old)
                            self._recovery_errors.pop(old, None)
                    else:
                        close_old = False
                    installed = True
            if not installed:
                reason = (denial.reason if denial is not None else
                          self._deny_reasons.get(candidate, "service_closed"))
                return _envelope(request.get("id"), "unavailable", reason)
            if old is not None and close_old:
                self._safe_close(old)
            return {
                "schema": SCHEMA, "id": request.get("id"), "status": "ok",
                "safe_action": "no_action", "reason": None, "output": None,
                "model": manifest.name, "provider": manifest.provider,
                "runtime_version": health.get("runtime_version"), "model_sha256": manifest.sha256,
            }
        except (OSError, RuntimeError, ValueError, TrustError) as exc:
            with self._condition:
                if candidate is not None:
                    self._staging.pop(candidate, None)
                    if isinstance(exc, TrustError):
                        self._deny_reasons[candidate] = exc.reason
                        ticket.deny_event.set()
            reason = exc.reason if isinstance(exc, TrustError) else "model_load_failed"
            result = _envelope(request.get("id"), "unavailable", reason)
            if not isinstance(exc, TrustError):
                result["diagnostics"] = str(exc)[-2000:]
            return result
        finally:
            if candidate is None:
                self._retire_ticket(ticket)
            elif not installed:
                with self._condition:
                    self._staging.pop(candidate, None)
                self._safe_close(candidate)

    def _set_global_trust_failure(self, reason):
        """Publish fatal denial in order with worker admission.

        Call only after trust-policy I/O has completed. The admission gate is
        never held while authorize/check/tick/install_policy executes.
        """
        with self._admission_lock:
            with self._condition:
                if self._trust_failure is None:
                    self._trust_failure = reason
                reason = self._trust_failure
                self._trust_status = self._trust_error = reason
                self._watch_stop.set()
                return reason

    def _check_ticket(self, ticket):
        with self._condition:
            failure = self._trust_failure
        if failure:
            ticket.deny_event.set()
            return failure
        try:
            self.trust_policy.check(ticket)
        except TrustError as error:
            return error.reason
        except BaseException:
            self._set_global_trust_failure("policy_unavailable")
            with self._condition:
                victims = {worker: "policy_unavailable" for worker in self._worker_tickets}
            self._deny_workers(victims)
            for worker in victims:
                self._safe_close(worker)
            return "policy_unavailable"
        return None

    def _trust_watch(self):
        while not self._watch_stop.wait(TRUST_WATCH_INTERVAL):
            try:
                self.refresh_trust()
            except BaseException:
                self._set_global_trust_failure("policy_unavailable")
                with self._condition:
                    victims = {worker: "policy_unavailable" for worker in self._worker_tickets}
                self._deny_workers(victims)
                deadline = time.monotonic() + SHUTDOWN_TIMEOUT
                for worker in victims:
                    try:
                        self._safe_close(worker, deadline)
                    except BaseException:
                        pass  # The close owner retains the failed state for retry.
                return

    def _deny_workers(self, denials):
        # Only local state/event publication under the service condition.
        with self._condition:
            for worker, reason in denials.items():
                ticket = self._worker_tickets.get(worker)
                if ticket is None:
                    continue
                ticket.deny_event.set()
                self._deny_reasons.setdefault(worker, reason)
                self._retired.add(worker)
                for name, active in list(self._models.items()):
                    if active is worker:
                        self._models.pop(name, None)
                for states in (self._staging, self._recovering):
                    cancel = states.get(worker)
                    if cancel is not None:
                        cancel.set()
                for request_id, request_ticket in self._inflight_tickets.items():
                    if request_ticket is ticket:
                        self._request_denials[request_id] = self._deny_reasons[worker]
                        event = self._inflight.get(request_id)
                        if event is not None:
                            event.set()
            self._condition.notify_all()

    def refresh_trust(self, bundle=None):
        """Run external policy operations without the scheduler condition."""
        policy_error = None
        fatal = None
        try:
            if bundle is None:
                self.trust_policy.tick()
            else:
                self.trust_policy.install_policy(bundle)
        except TrustError as error:
            policy_error = error.reason
        except BaseException:
            fatal = "policy_unavailable"
        if fatal:
            self._set_global_trust_failure(fatal)
        with self._condition:
            tickets = dict(self._worker_tickets)
            fatal = self._trust_failure
        denials = {}
        for worker, ticket in tickets.items():
            if fatal:
                denials[worker] = fatal
                continue
            try:
                self.trust_policy.check(ticket)
            except TrustError as error:
                denials[worker] = error.reason
            except BaseException:
                fatal = "policy_unavailable"
                break
        if fatal:
            fatal = self._set_global_trust_failure(fatal)
            with self._condition:
                denials = {worker: fatal for worker in self._worker_tickets}
        self._deny_workers(denials)
        with self._condition:
            self._trust_status = fatal or policy_error or getattr(self.trust_policy, "status", "ok")
            self._trust_error = fatal or policy_error
        failures = []
        deadline = time.monotonic() + SHUTDOWN_TIMEOUT
        for worker, reason in denials.items():
            with self._condition:
                recovery = self._recovery_threads.get(worker)
            if recovery is not None and recovery is not threading.current_thread():
                recovery.join(max(0.0, deadline - time.monotonic()))
            if recovery is not None and recovery.is_alive():
                stop_error = TimeoutError("recovery did not stop")
            else:
                if hasattr(worker, "_deny_reason"):
                    worker._deny_reason = reason
                stop_error = self._safe_close(worker, deadline)
            with self._condition:
                if stop_error is not None:
                    failures.append(stop_error)
                    self._retired.add(worker)
                    self._shutdown_close_errors[worker] = str(stop_error)[:512]
                    self._trust_status = self._trust_error = "worker_stop_failed"
                else:
                    self._retired.discard(worker)
                    self._shutdown_close_errors.pop(worker, None)
        if failures:
            return _envelope(None, "unavailable", "worker_stop_failed")
        return {"status": "ok", "revoked_workers": len(denials)}

    def _stop_trust_watcher(self):
        self._watch_stop.set()
        thread = self._watch_thread
        if thread is not threading.current_thread():
            thread.join(timeout=SHUTDOWN_TIMEOUT)
            if thread.is_alive():
                raise RuntimeError("trust watcher did not stop")

    @staticmethod
    def _deadline(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, Real):
            return None
        try:
            number = float(value)
        except (OverflowError, TypeError, ValueError):
            return None
        return number if math.isfinite(number) and 0 < number <= 300000 else None

    def _infer(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request["id"]
        deadline_ms = self._deadline(request.get("deadline_ms", 1000))
        if deadline_ms is None:
            return _envelope(request_id, "invalid", "invalid_deadline")
        expires = time.monotonic() + deadline_ms / 1000
        event = threading.Event()
        with self._condition:
            if self._closed:
                return _envelope(request_id, "unavailable", "service_closed")
            if request_id in self._inflight:
                return _envelope(request_id, "busy", "duplicate_id")
            self._inflight[request_id] = event
            queued_name = request.get("model")
            queued_worker = self._models.get(queued_name) if type(queued_name) is str else None
            queued_ticket = self._worker_tickets.get(queued_worker)
            if queued_worker is not None and queued_ticket is not None:
                self._inflight_tickets[request_id] = queued_ticket
            if self._active_id is not None or self._queued_id is not None:
                if self._queued_id is not None:
                    self._inflight.pop(request_id, None)
                    self._inflight_tickets.pop(request_id, None)
                    return _envelope(request_id, "busy", "queue_full")
                self._queued_id = request_id
                while self._active_id is not None and not self._closed:
                    if event.is_set():
                        self._queued_id = None
                        self._inflight.pop(request_id, None)
                        self._inflight_tickets.pop(request_id, None)
                        reason = self._request_denials.pop(request_id, "cancelled")
                        return _envelope(request_id, "unavailable" if reason != "cancelled" else "cancelled", reason)
                    remaining = expires - time.monotonic()
                    if remaining <= 0:
                        self._queued_id = None
                        self._inflight.pop(request_id, None)
                        self._inflight_tickets.pop(request_id, None)
                        return _envelope(request_id, "timeout", "timeout")
                    self._condition.wait(min(.02, remaining))
                if self._queued_id == request_id:
                    self._queued_id = None
            if self._closed:
                self._inflight.pop(request_id, None)
                self._inflight_tickets.pop(request_id, None)
                return _envelope(request_id, "unavailable", "service_closed")
            if event.is_set():
                self._inflight.pop(request_id, None)
                self._inflight_tickets.pop(request_id, None)
                denial = self._request_denials.pop(request_id, None)
                return _envelope(request_id, "unavailable", denial) if denial else _envelope(request_id, "cancelled", "cancelled")
            self._active_id = request_id
            name = request.get("model")
            worker = self._models.get(name) if type(name) is str else None
            if worker is None:
                self._active_id = None
                self._inflight.pop(request_id, None)
                self._inflight_tickets.pop(request_id, None)
                self._condition.notify_all()
                return _envelope(request_id, "unavailable", "model_unavailable")
            ticket = self._worker_tickets.get(worker)
            if ticket is not None:
                self._inflight_tickets[request_id] = ticket
            if ticket is None:
                self._active_id = None
                self._inflight.pop(request_id, None)
                self._inflight_tickets.pop(request_id, None)
                self._condition.notify_all()
                return _envelope(request_id, "unavailable", "policy_unavailable")
            self._worker_users[worker] = self._worker_users.get(worker, 0) + 1
        try:
            reason = self._check_ticket(ticket)
            if reason:
                self._deny_workers({worker: reason})
                self._safe_close(worker)
                return _envelope(request_id, "unavailable", reason)
            with self._condition:
                if worker in self._recovering or worker in self._recovery_errors:
                    reason = "worker_recovering" if worker in self._recovering else "worker_unavailable"
                    return _envelope(request_id, "unavailable", reason)
                if ticket.deny_event.is_set() or self._trust_failure:
                    return _envelope(request_id, "unavailable", self._deny_reasons.get(worker, self._trust_failure or "model_revoked"))
            remaining_ms = (expires-time.monotonic())*1000
            if remaining_ms <= 0:
                return _envelope(request_id, "timeout", "timeout")
            result = worker.infer(request_id, request.get("input"), remaining_ms, event)
            reason = self._check_ticket(ticket)
            if reason:
                self._deny_workers({worker: reason})
                self._safe_close(worker)
                return _envelope(request_id, "unavailable", reason)
            with self._condition:
                if ticket.deny_event.is_set() or self._trust_failure:
                    return _envelope(request_id, "unavailable", self._deny_reasons.get(worker, self._trust_failure or "model_revoked"))
                return result
        except RequestError:
            return _envelope(request_id, "invalid", "invalid_request")
        finally:
            close_worker = False
            with self._condition:
                self._inflight.pop(request_id, None)
                self._inflight_tickets.pop(request_id, None)
                self._request_denials.pop(request_id, None)
                self._active_id = None
                users = self._worker_users.get(worker, 1)-1
                if users:
                    self._worker_users[worker] = users
                else:
                    self._worker_users.pop(worker, None)
                    if worker in self._retired:
                        # The shutdown path owns the actual close attempt. The
                        # retirement path still joins that same gate so it
                        # cannot race or leave an active dispatch blocked.
                        close_worker = True
                    elif not worker.alive and not self._closed and worker not in self._recovering:
                        cancel = threading.Event()
                        self._recovering[worker] = cancel
                        recovery_thread = threading.Thread(
                            target=self._recover, args=(worker, cancel), daemon=True)
                        self._recovery_threads[worker] = recovery_thread
                        recovery_thread.start()
                self._condition.notify_all()
            self._release_closed_ticket(worker)
            if close_worker:
                self._safe_close(worker)

    def _recover(self, worker, cancel):
        error = None
        ticket = self._worker_tickets.get(worker)
        try:
            if ticket is None:
                raise TrustError("policy_unavailable")
            reason = self._check_ticket(ticket)
            if reason:
                raise TrustError(reason)
            if ticket.deny_event.is_set() or cancel.is_set():
                raise TrustError(self._deny_reasons.get(worker, "model_revoked"))
            worker.restart(cancel=cancel)
            reason = self._check_ticket(ticket)
            if reason:
                raise TrustError(reason)
            if ticket.deny_event.is_set() or cancel.is_set():
                raise TrustError(self._deny_reasons.get(worker, "model_revoked"))
        except (OSError, RuntimeError, ValueError, TrustError) as exc:
            error = exc.reason if isinstance(exc, TrustError) else str(exc)[-2000:]
            if isinstance(exc, TrustError):
                ticket.deny_event.set()
                self._deny_reasons[worker] = exc.reason
        finally:
            with self._condition:
                self._recovering.pop(worker, None)
                if error:
                    self._recovery_errors[worker] = error
                close_now = (self._closed or worker in self._retired or
                             (ticket is not None and ticket.deny_event.is_set()))
                if close_now:
                    self._retired.add(worker)
                    for name, active in list(self._models.items()):
                        if active is worker and ticket is not None and ticket.deny_event.is_set():
                            self._models.pop(name, None)
                self._condition.notify_all()
            if close_now:
                self._safe_close(worker)
            with self._condition:
                self._recovery_threads.pop(worker, None)
                self._condition.notify_all()
            self._release_closed_ticket(worker)

    def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        current = threading.current_thread()
        with self._condition:
            self._dispatch_threads.add(current)
        try:
            return self._dispatch(request)
        finally:
            with self._condition:
                self._dispatch_threads.discard(current)
                self._condition.notify_all()

    def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        if type(request) is not dict:
            return _envelope(None, "invalid", "invalid_request")
        request_id = request.get("id")
        if not _valid_id(request_id):
            return _envelope(None, "invalid", "invalid_id")
        with self._condition:
            if self._closed:
                return _envelope(request_id, "unavailable", "service_closed")
        op = request.get("op")
        if op == "list":
            with self._condition:
                models = sorted(self._models)
            return {"schema": SCHEMA, "id": request_id, "status": "ok",
                    "safe_action": "no_action", "reason": None, "output": None, "models": models}
        if op in ("load", "reload"):
            return self._load(request)
        if op == "unload":
            name = request.get("model")
            with self._condition:
                worker = self._models.pop(name, None) if isinstance(name, str) else None
                if worker is not None:
                    self._retired.add(worker)
                    if worker in self._recovering:
                        self._recovering[worker].set()
                    close_now = self._worker_users.get(worker, 0) == 0 and worker not in self._recovering
                    if close_now:
                        self._retired.discard(worker)
                        self._recovery_errors.pop(worker, None)
            if worker is None:
                return _envelope(request_id, "unavailable", "model_unavailable")
            if close_now:
                self._safe_close(worker)
            return {"schema": SCHEMA, "id": request_id, "status": "ok",
                    "safe_action": "no_action", "reason": None, "output": None, "model": name}
        if op == "health":
            with self._condition:
                health = {name: {"alive": worker.alive, "pid": worker.pid,
                                 "recovering": worker in self._recovering,
                                 "diagnostics": self._recovery_errors.get(worker, "")}
                          for name, worker in self._models.items()}
                inflight, queued = int(self._active_id is not None), int(self._queued_id is not None)
            return {"schema": SCHEMA, "id": request_id, "status": "ok",
                    "safe_action": "no_action", "reason": None, "output": None, "models": health,
                    "inflight": inflight, "queued": queued,
                    "service_runtime_version": RUNTIME_VERSION,
                    "trust": {"mode": getattr(self.trust_policy, "mode", "production"),
                              "status": self._trust_status,
                              "last_error": self._trust_error,
                              "revoked_workers_pending": sum(1 for worker in self._retired
                                                              if worker in self._worker_tickets)}}
        if op == "cancel":
            target = request.get("target_id")
            if not _valid_id(target):
                return _envelope(request_id, "invalid", "invalid_target_id")
            with self._condition:
                event = self._inflight.get(target)
                if event is None:
                    return _envelope(request_id, "unavailable", "request_not_found")
                event.set()
                self._condition.notify_all()
            return {"schema": SCHEMA, "id": request_id, "status": "ok",
                    "safe_action": "no_action", "reason": "cancel_requested", "output": None, "target_id": target}
        if op == "infer":
            return self._infer(request)
        return _envelope(request_id, "invalid", "unknown_operation")

    def close(self):
        self._watch_stop.set()
        if self._watch_thread is not threading.current_thread():
            self._watch_thread.join(timeout=SHUTDOWN_TIMEOUT)
            if self._watch_thread.is_alive():
                raise RuntimeError("trust watcher did not stop")
        current = threading.current_thread()
        deadline = time.monotonic() + SHUTDOWN_TIMEOUT
        if not self._shutdown_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise RuntimeError("worker shutdown timed out")
        try:
            self._close_locked(current, deadline)
        finally:
            self._shutdown_lock.release()

    def _close_locked(self, current, deadline):
        with self._condition:
            if self._closed and self._shutdown_error is None:
                return
            self._closed = True
            self._shutdown_deadline = deadline
            for event in self._inflight.values():
                event.set()
            for event in self._staging.values():
                event.set()
            for event in self._recovering.values():
                event.set()
            workers = set(self._models.values())
            workers.update(self._staging)
            workers.update(self._recovering)
            workers.update(self._retired)
            workers.update(self._worker_users)
            workers.update(self._shutdown_close_errors)
            workers.update(worker for worker, thread in self._shutdown_close_threads.items() if thread.is_alive())
            self._models.clear()
            self._retired.update(workers)
            dispatch_threads = self._dispatch_threads - {current}
            recovery_by_worker = {
                worker: thread for worker, thread in self._recovery_threads.items()
                if thread is not current
            }
            self._condition.notify_all()

        # Close every handle, including active/recovering and retired handles.
        # Run closes concurrently so one stuck implementation cannot starve
        # another handle of its shutdown attempt or the caller of its deadline.
        close_threads = []
        def close_worker(worker):
            error = self._safe_close(worker)
            with self._condition:
                if error is None:
                    self._retired.discard(worker)
                    self._shutdown_close_errors.pop(worker, None)
                else:
                    self._shutdown_close_errors[worker] = type(error).__name__ + ": " + str(error)
                self._condition.notify_all()

        recovering = set(recovery_by_worker)
        for worker in workers - recovering:
            with self._condition:
                thread = self._shutdown_close_threads.get(worker)
                if thread is None or not thread.is_alive():
                    thread = threading.Thread(target=close_worker, args=(worker,), daemon=True)
                    self._shutdown_close_threads[worker] = thread
                    thread.start()
            close_threads.append(thread)

        # Cancellation is cooperative. Never close a handle while its restart
        # is still executing; if it misses the deadline, retain it for retry.
        recovery_threads = list(recovery_by_worker.values())
        for thread in recovery_threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)
        recovery_timed_out = any(thread.is_alive() for thread in recovery_threads)

        for worker, recovery_thread in recovery_by_worker.items():
            if recovery_thread.is_alive():
                continue
            with self._condition:
                thread = self._shutdown_close_threads.get(worker)
                if thread is None or not thread.is_alive():
                    thread = threading.Thread(target=close_worker, args=(worker,), daemon=True)
                    self._shutdown_close_threads[worker] = thread
                    thread.start()
            close_threads.append(thread)

        threads = close_threads + recovery_threads
        for thread in close_threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)

        with self._condition:
            while dispatch_threads & self._dispatch_threads:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            if (recovery_timed_out or any(thread.is_alive() for thread in threads)
                    or dispatch_threads & self._dispatch_threads):
                self._shutdown_error = "worker shutdown timed out"
            elif self._shutdown_close_errors or self._retired:
                errors = list(self._shutdown_close_errors.values()) or ["worker handles remain unclosed"]
                self._shutdown_error = "worker shutdown failed: " + "; ".join(errors)[:2000]
            else:
                self._shutdown_error = None
            self._shutdown_close_threads = {
                worker: thread for worker, thread in self._shutdown_close_threads.items() if thread.is_alive()
            }
            if self._shutdown_error is not None:
                raise RuntimeError(self._shutdown_error)


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(5.0)
        while True:
            try:
                line = self.rfile.readline(MAX_REQUEST_BYTES + 1)
                if not line:
                    return
                if len(line) > MAX_REQUEST_BYTES:
                    while line and not line.endswith(b"\n"):
                        line = self.rfile.readline(MAX_REQUEST_BYTES + 1)
                    self.wfile.write((json.dumps(_envelope(None, "invalid", "invalid_json")) + "\n").encode())
                    self.wfile.flush()
                    return
                request = parse_request_line(line)
                response = self.server.service.dispatch(request)
                payload = (json.dumps(response, allow_nan=False, separators=(",", ":")) + "\n").encode()
                self.wfile.write(payload)
                self.wfile.flush()
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                try:
                    self.wfile.write((json.dumps(_envelope(None, "invalid", "invalid_json")) + "\n").encode())
                    self.wfile.flush()
                except OSError:
                    return
            except (OSError, TimeoutError):
                return


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, path, service):
        self.service = service
        self._connection_slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._bound_socket_reference_fd = -1
        try:
            socketserver.TCPServer.__init__(self, str(path), _Handler, bind_and_activate=False)
            self.server_bind()
            self.server_activate()
        except BaseException as exc:
            cleanup_error = None
            try:
                if self._bound_socket_reference_fd >= 0:
                    _unlink_constructor_socket(self.server_address, self._bound_socket_reference_fd)
            except BaseException as error:
                cleanup_error = error
            finally:
                if hasattr(self, "socket"):
                    self.server_close()
            if cleanup_error is not None and hasattr(exc, "add_note"):
                exc.add_note("bound socket cleanup failed: " + repr(cleanup_error))
            raise

    def server_bind(self):
        super().server_bind()
        info = os.lstat(self.server_address)
        if not stat.S_ISSOCK(info.st_mode):
            raise RuntimeError("bound path is not a Unix socket")
        self.bound_socket_identity = (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))
        reference_fd = -1
        try:
            reference_flags = getattr(os, "O_PATH", 0)
            if not reference_flags:
                raise OSError(errno.ENOTSUP, "stable Unix socket references are unavailable")
            reference_fd = os.open(
                self.server_address,
                reference_flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            )
            reference_info = os.fstat(reference_fd)
            reference_identity = (reference_info.st_dev, reference_info.st_ino,
                                  stat.S_IFMT(reference_info.st_mode))
            if reference_identity != self.bound_socket_identity or not stat.S_ISSOCK(reference_info.st_mode):
                raise RuntimeError("socket identity changed during bind")
            self._bound_socket_reference_fd = reference_fd
            reference_fd = -1
        finally:
            if reference_fd >= 0:
                os.close(reference_fd)

    def take_socket_reference(self):
        reference_fd = self._bound_socket_reference_fd
        self._bound_socket_reference_fd = -1
        if reference_fd < 0:
            raise RuntimeError("socket reference is unavailable")
        return reference_fd

    def server_close(self):
        try:
            super().server_close()
        finally:
            reference_fd = self._bound_socket_reference_fd
            self._bound_socket_reference_fd = -1
            if reference_fd >= 0:
                os.close(reference_fd)

    def process_request(self, request, client_address):
        if not self._connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        original = self.process_request_thread
        def run_and_release(req, addr):
            try:
                original(req, addr)
            finally:
                self._connection_slots.release()
        thread = threading.Thread(target=run_and_release, args=(request, client_address), daemon=True)
        thread.start()


class UnixInferenceServer:
    """Lifecycle wrapper for a private, exclusive Unix-domain socket."""

    def __init__(self, path: Path, service: InferenceService):
        requested = Path(path)
        if requested.name in ("", ".", ".."):
            raise ValueError("socket path must name a leaf")
        requested.parent.mkdir(parents=True, exist_ok=True)
        parent = requested.parent.resolve()
        parent_stat = parent.stat()
        if parent_stat.st_uid != os.geteuid() or parent_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise PermissionError("socket parent must be owned by the service user and not group/world writable")
        self.path = parent / requested.name
        self.service = service
        # Failed constructors have no returned close handle. Resume their
        # durable, inode-anchored cleanup before considering a new bind.
        _recover_socket_cleanups(parent)
        if self.path.exists() or self.path.is_symlink():
            raise FileExistsError(self.path)
        self._server = None
        self._socket_reference_fd = -1
        self._close_lock = threading.Lock()
        self._socket_cleanup_name = ".xtos-socket-cleanup-" + secrets.token_hex(16)
        self._close_state = "open"
        self._server_closed = False
        self._socket_cleanup_complete = False
        self._closed = False
        try:
            with _UMASK_LOCK:
                previous_umask = os.umask(0o177)
                try:
                    self._server = _UnixServer(self.path, service)
                finally:
                    os.umask(previous_umask)
            self._socket_reference_fd = self._server.take_socket_reference()
            reference_info = os.fstat(self._socket_reference_fd)
            self._socket_identity = (reference_info.st_dev, reference_info.st_ino,
                                     stat.S_IFMT(reference_info.st_mode))
            if self._socket_lstat() != self._socket_identity:
                raise RuntimeError("socket identity changed during initialization")
            os.chmod(self.path, 0o600, follow_symlinks=False)
        except BaseException:
            cleanup_error = None
            try:
                if self._socket_reference_fd >= 0:
                    _unlink_constructor_socket(self.path, self._socket_reference_fd,
                                               self._socket_cleanup_name)
                    self._socket_cleanup_complete = True
            except BaseException as error:
                cleanup_error = error
            finally:
                try:
                    if self._server is not None and not self._server_closed:
                        self._server.server_close()
                        self._server_closed = True
                finally:
                    self._close_socket_reference()
            if cleanup_error is not None:
                raise cleanup_error
            raise

    def _close_socket_reference(self):
        reference_fd = self._socket_reference_fd
        self._socket_reference_fd = -1
        if reference_fd >= 0:
            os.close(reference_fd)

    def _socket_lstat(self):
        info = os.lstat(self.path)
        if not stat.S_ISSOCK(info.st_mode):
            raise RuntimeError("bound path is not a Unix socket")
        return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))

    def _unlink_owned_socket(self):
        if self._socket_reference_fd < 0:
            raise OSError(errno.ENOTSUP, "cannot safely identify Unix socket")
        _unlink_socket_with_reference(
            self.path, self._socket_reference_fd, self._socket_cleanup_name)

    def serve_forever(self):
        try:
            self._server.serve_forever(poll_interval=.1)
        finally:
            self.server_close()

    def shutdown(self):
        self._server.shutdown()

    def server_close(self):
        if not self._close_lock.acquire(timeout=SERVER_CLOSE_LOCK_TIMEOUT):
            raise RuntimeError("server close is already in progress")
        try:
            if self._close_state == "closed":
                return
            if not self._server_closed:
                try:
                    self._server.server_close()
                except BaseException:
                    self._close_state = "cleanup_failed"
                    raise
                self._server_closed = True
            try:
                if not self._socket_cleanup_complete:
                    self._unlink_owned_socket()
                    self._socket_cleanup_complete = True
                self._close_socket_reference()
            except BaseException:
                self._close_state = "cleanup_failed"
                raise
            self._close_state = "closed"
            self._closed = True
        finally:
            self._close_lock.release()
