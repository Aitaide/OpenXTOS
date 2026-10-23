"""Private filesystem authority; Linux stdlib only, never request-selected paths."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import secrets
import stat
import time

from .schema import GateError, MAX_JSON

TRUSTED_ROOT = Path("/var/lib/xtos/action-gate")
FLAGS = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
NONCE = re.compile(r"[0-9a-f]{64}\Z")


class _Store:
    fixture = False

    def _access(self):
        if not self.fixture and os.geteuid() != 0:
            raise GateError("root management authority required")

    def _directory(self, *, create=False):
        self._access()
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | FLAGS)
        try:
            components = self.path.parts[1:]
            for index, component in enumerate(components):
                if create and index == len(components) - 1:
                    os.mkdir(component, mode=0o700, dir_fd=fd)
                    os.fsync(fd)
                try:
                    nxt = os.open(component, os.O_RDONLY | os.O_DIRECTORY | FLAGS, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(component, mode=0o700, dir_fd=fd)
                    os.fsync(fd)
                    nxt = os.open(component, os.O_RDONLY | os.O_DIRECTORY | FLAGS, dir_fd=fd)
                oldfd = fd
                fd = nxt
                os.close(oldfd)
                info = os.fstat(fd)
                if info.st_uid not in (0, self.uid) or info.st_mode & 0o022:
                    raise GateError("unsafe store ancestor")
            info = os.fstat(fd)
            if (stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != self.uid or
                    info.st_gid != self.gid):
                raise GateError("unsafe store directory")
            if hasattr(self, "identity") and self.identity != (info.st_dev, info.st_ino):
                raise GateError("store directory replaced")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _file(self, directory, name):
        fd = os.open(name, os.O_RDONLY | FLAGS, dir_fd=directory)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != self.uid or info.st_gid != self.gid
                    or info.st_nlink != 1 or info.st_size > MAX_JSON):
                raise GateError("unsafe authority file")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _read(self, directory, name):
        fd = self._file(directory, name)
        try:
            data = os.read(fd, MAX_JSON + 1)
            if len(data) > MAX_JSON:
                raise GateError("oversized authority file")
            return data
        finally:
            os.close(fd)

    def _write(self, directory, name, data):
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | FLAGS,
                     0o600, dir_fd=directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(fd)
        finally:
            os.close(fd)

    def _initialize(self):
        directory = self._directory()
        try:
            info = os.fstat(directory)
            self.identity = (info.st_dev, info.st_ino)
            self._write(directory, ".key", secrets.token_bytes(32))
            self._write(directory, ".lock", b"")
            os.fsync(directory)
        finally:
            os.close(directory)

    @contextmanager
    def locked(self):
        directory = lock = None
        try:
            directory = self._directory()
            lock = self._file(directory, ".lock")
            deadline = time.monotonic() + 1
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise GateError("authority busy")
                    time.sleep(.005)
            key = self._read(directory, ".key")
            if len(key) != 32:
                raise GateError("invalid authority key")
            yield directory, key
        except OSError as exc:
            raise GateError("authority storage refused") from exc
        finally:
            cleanup_error = None
            if lock is not None:
                try:
                    os.close(lock)
                except OSError as exc:
                    cleanup_error = exc
            if directory is not None:
                try:
                    os.close(directory)
                except OSError as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
            if cleanup_error is not None:
                raise GateError("authority storage refused during cleanup") from cleanup_error

    def publish(self, directory, nonce, data):
        for suffix in (".pending", ".spent"):
            try:
                os.stat(nonce + suffix, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise GateError("nonce collision")
        temporary = nonce + ".tmp"
        self._write(directory, temporary, data)
        os.rename(temporary, nonce + ".pending", src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)

    def consume(self, directory, nonce):
        if type(nonce) is not str or NONCE.fullmatch(nonce) is None:
            raise GateError("invalid approval nonce")
        try:
            os.stat(nonce + ".spent", dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise GateError("approval already consumed")
        # Durable consume precedes ALL parsing, validation, and executor callbacks.
        os.rename(nonce + ".pending", nonce + ".spent",
                  src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
        return self._read(directory, nonce + ".spent")


class FixtureStore(_Store):
    """Explicit injected fixture, never used by the production CLI."""
    fixture = True

    def __init__(self, path):
        self.path = Path(path).absolute()
        self.uid, self.gid = os.geteuid(), os.getegid()
        self.path.mkdir(mode=0o700)
        self._initialize()


class TrustedStore(_Store):
    """Fixed root-owned management store; not a setuid Python entrypoint."""
    def __init__(self, *, initialize=False):
        self._access()
        self.path = TRUSTED_ROOT
        self.uid = self.gid = 0
        if initialize:
            # Create only through verified directory descriptors, never symlink paths.
            fd = self._directory(create=True)
            os.close(fd)
            self._initialize()
        else:
            fd = self._directory()
            info = os.fstat(fd)
            self.identity = (info.st_dev, info.st_ino)
            os.close(fd)
