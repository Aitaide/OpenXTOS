"""Fail-closed model signatures, policy revocation, and sequence state.

This module is deliberately independent of the runtime service.  It has no
unsigned or environment-controlled escape hatch: production construction with
missing time, state, anchor, or verifier dependencies remains unavailable.
"""
from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import json
import math
import os
import platform
import re
import selectors
import shutil
import stat
import subprocess
import tempfile
import time
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from .protocol import ManifestError, ModelManifest, validate_manifest

from . import RUNTIME_VERSION
MODEL_SCHEMA = "xtos-ai-model-v1"
POLICY_SCHEMA = "xtos-ai-policy-v1"
MODEL_DOMAIN = b"XTOS-AI-MODEL-v1\x00"
POLICY_DOMAIN = b"XTOS-AI-POLICY-v1\x00"
MAX_ENVELOPE_BYTES = 64 * 1024
MAX_CHECKPOINT_BYTES = 256 * 1024
MAX_POLICY_LIFETIME = 86_400
MAX_POLICY_KEYS = 128
MAX_REVOKED = 512
MAX_LICENSES = 64
MAX_SEQUENCE = (1 << 63) - 1
_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_B64 = re.compile(r"^[A-Za-z0-9+/]*={0,2}$")



class TrustError(ValueError):
    """A trust decision failed; ``reason`` is the stable machine code."""

    def __init__(self, reason: str, message: str | None = None):
        self.reason = reason
        super().__init__(message or reason)


class SignatureVerifier(Protocol):
    def verify(self, public_key: bytes, message: bytes, signature: bytes) -> None: ...


@dataclass(frozen=True, slots=True)
class ClockReading:
    utc_seconds: int
    boottime_seconds: float
    trusted: bool
    source: str


class TimeSource(Protocol):
    def read(self) -> ClockReading: ...


@dataclass(frozen=True, slots=True)
class PolicyCheckpoint:
    policy_bundle: bytes
    highest_sequence: int
    policy_payload_sha256: str
    revoked_key_ids: frozenset[str] = frozenset()
    revoked_model_sha256: frozenset[str] = frozenset()
    last_trusted_utc: int = 0
    anchor_identity: str | None = None


class PolicyStore(Protocol):
    def read(self) -> PolicyCheckpoint | None: ...
    def commit(self, checkpoint: PolicyCheckpoint) -> None: ...


class RollbackAnchor(Protocol):
    def read(self) -> tuple[int, str] | None: ...
    def advance(self, sequence: int, payload_sha256: str) -> None: ...


@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    sequence: int
    issued_at: int
    expires_at: int
    payload_sha256: str
    publisher_keys: Mapping[str, bytes]
    publisher_windows: Mapping[str, tuple[int, int]]
    revoked_key_ids: frozenset[str]
    revoked_model_sha256: frozenset[str]
    allowed_licenses: frozenset[str]
    envelope: bytes


@dataclass(frozen=True, slots=True, eq=False)
class AuthorizedModel:
    manifest: ModelManifest
    key_id: str
    signed_payload_sha256: str
    policy_sequence: int
    deny_event: threading.Event
    claims: Mapping[str, Any]


# ---------- constrained JSON / encoding helpers ----------


def _duplicate_reject(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError("non-finite number")


def _json_load(raw: bytes | str | Mapping[str, Any], *, max_bytes: int = MAX_ENVELOPE_BYTES,
               max_string_length: int = 4096) -> tuple[dict[str, Any], bytes]:
    if isinstance(raw, Mapping):
        # Mapping input is already decoded; the public byte contract remains
        # canonicalized before signature verification.
        value: Any = dict(raw)
        try:
            encoded = _canonical(value, max_string_length=max_string_length)
        except (TypeError, ValueError, OverflowError, RecursionError):
            raise TrustError("malformed_envelope") from None
        if len(encoded) > max_bytes:
            raise TrustError("malformed_envelope")
        # Detach nested mutable objects from caller-owned mappings before
        # signature verification and subsequent claim validation.
        value = json.loads(encoded.decode("ascii"), object_pairs_hook=_duplicate_reject,
                           parse_constant=_reject_constant)
    else:
        if isinstance(raw, str):
            try:
                encoded = raw.encode("utf-8")
            except UnicodeEncodeError:
                raise TrustError("malformed_envelope") from None
        elif isinstance(raw, bytes):
            encoded = raw
        else:
            raise TrustError("malformed_envelope")
        if len(encoded) > max_bytes:
            raise TrustError("malformed_envelope")
        try:
            value = json.loads(encoded.decode("utf-8"), object_pairs_hook=_duplicate_reject,
                               parse_constant=_reject_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError, RecursionError):
            raise TrustError("malformed_envelope") from None
    if type(value) is not dict:
        raise TrustError("malformed_envelope")
    try:
        _check_json_value(value, 0, max_string_length)
        canonical = _canonical(value, max_string_length=max_string_length)
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise TrustError("malformed_envelope") from None
    if len(canonical) > max_bytes:
        raise TrustError("malformed_envelope")
    return value, canonical


def _check_json_value(value: Any, depth: int, max_string_length: int = 4096) -> None:
    if depth > 16:
        raise ValueError("json too deep")
    if value is None or type(value) is bool:
        raise ValueError("null/bool not allowed")
    if type(value) is int:
        return
    if type(value) is float:
        raise ValueError("float not allowed")
    if type(value) is str:
        if any(ord(ch) < 0x20 or ord(ch) > 0x7e for ch in value):
            raise ValueError("non-printable string")
        if len(value) > max_string_length:
            raise ValueError("string too long")
        return
    if type(value) is list:
        if len(value) > 1024:
            raise ValueError("array too long")
        for item in value:
            _check_json_value(item, depth + 1, max_string_length)
        return
    if type(value) is dict:
        if len(value) > 256:
            raise ValueError("object too large")
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("non-string key")
            _check_json_value(key, depth + 1, max_string_length)
            _check_json_value(item, depth + 1, max_string_length)
        return
    raise ValueError("unsupported json type")


def _canonical(value: Any, *, max_string_length: int = 4096) -> bytes:
    _check_json_value(value, 0, max_string_length)
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _exact(value: Any, expected: set[str], label: str) -> None:
    if type(value) is not dict or set(value) != expected or any(type(k) is not str for k in value):
        raise TrustError("malformed_envelope", f"{label} keys")


def _int(value: Any, label: str, low: int = 0, high: int = MAX_SEQUENCE) -> int:
    if type(value) is not int or not low <= value <= high:
        raise TrustError("malformed_envelope", label)
    return value


def _text(value: Any, label: str, max_len: int = 4096) -> str:
    if type(value) is not str or not value or len(value) > max_len:
        raise TrustError("malformed_envelope", label)
    if any(ord(ch) < 0x20 or ord(ch) > 0x7e for ch in value):
        raise TrustError("malformed_envelope", label)
    return value


def _hex(value: Any, label: str) -> str:
    if type(value) is not str or not _HEX64.fullmatch(value):
        raise TrustError("malformed_envelope", label)
    return value


def _b64(value: Any, label: str, length: int) -> bytes:
    if type(value) is not str or not _B64.fullmatch(value) or len(value) % 4:
        raise TrustError("malformed_envelope", label)
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, binascii.Error):
        raise TrustError("malformed_envelope", label) from None
    if base64.b64encode(decoded).decode("ascii") != value or len(decoded) != length:
        raise TrustError("malformed_envelope", label)
    return decoded


def _signature(value: Any) -> bytes:
    try:
        return _b64(value, "signature", 64)
    except TrustError:
        raise TrustError("signature_invalid") from None


def _semver(value: Any, label: str) -> tuple[int, int, int]:
    text = _text(value, label, 64)
    match = _SEMVER.fullmatch(text)
    if not match:
        raise TrustError("malformed_envelope", label)
    numbers = tuple(int(part) for part in match.groups())
    if any(number > 2_147_483_647 for number in numbers):
        raise TrustError("malformed_envelope", label)
    return numbers



def _version_at_least(actual: str, minimum: str) -> bool:
    return tuple(map(int, _SEMVER.fullmatch(actual).groups())) >= tuple(map(int, _SEMVER.fullmatch(minimum).groups()))


def _fingerprint(public_key: bytes) -> str:
    return hashlib.sha256(public_key).hexdigest()


def _freeze(value: Any) -> Any:
    if type(value) is dict:
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if type(value) is list:
        return tuple(_freeze(item) for item in value)
    return value


def _verify(verifier: SignatureVerifier | None, public_key: bytes, message: bytes,
            signature: bytes) -> None:
    if verifier is None:
        raise TrustError("signature_verifier_unavailable")
    try:
        result = verifier.verify(public_key, message, signature)
        if result is not None:
            raise TrustError("signature_verifier_unavailable")
    except TrustError:
        raise
    except Exception:
        raise TrustError("signature_verifier_unavailable") from None


# ---------- fixed helper verifier ----------


class NativeEd25519Verifier:
    """Run only the explicitly selected native helper; never use PATH fallback."""

    def __init__(self, helper_path: str | Path = "/usr/libexec/xtos/modelverify",
                 *, timeout: float = 2.0, spool_parent: str | Path | None = None):
        self.helper_path = Path(helper_path)
        self.timeout = min(float(timeout), 2.0)
        self.spool_parent = Path(spool_parent) if spool_parent is not None else None

    @staticmethod
    def _write_private(path: Path, data: bytes) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            view = memoryview(data)
            while view:
                count = os.write(fd, view)
                view = view[count:]
            # These private files are temporary verifier inputs, removed after
            # the helper exits. Closing completes their same-host visibility;
            # durability belongs to FilePolicyStore's checkpoint transaction.
        finally:
            os.close(fd)

    @staticmethod
    def _private_directory(path: Path) -> None:
        current = Path(path.anchor)
        parts = path.parts[1:]
        if not parts or any(component in (".", "..") for component in parts):
            raise TrustError("signature_verifier_unavailable")
        for index, component in enumerate(parts):
            current /= component
            try:
                item = current.lstat()
            except FileNotFoundError:
                current.mkdir(mode=0o700)
                item = current.lstat()
            if stat.S_ISLNK(item.st_mode) or not stat.S_ISDIR(item.st_mode):
                raise TrustError("signature_verifier_unavailable")
            if item.st_uid not in (0, os.geteuid()):
                raise TrustError("signature_verifier_unavailable")
            if index == len(parts) - 1:
                if item.st_uid != os.geteuid() or item.st_mode & 0o077:
                    raise TrustError("signature_verifier_unavailable")
            elif item.st_mode & 0o022 and not (item.st_mode & stat.S_ISVTX and item.st_uid == 0):
                raise TrustError("signature_verifier_unavailable")

    @staticmethod
    def _private_ancestors(path: Path) -> None:
        current = Path(path.anchor)
        for component in path.parts[1:-1]:
            current /= component
            try:
                item = current.lstat()
            except OSError:
                raise TrustError("signature_verifier_unavailable") from None
            if (stat.S_ISLNK(item.st_mode) or not stat.S_ISDIR(item.st_mode) or
                    item.st_uid not in (0, os.geteuid()) or
                    item.st_mode & 0o022 and not (item.st_mode & stat.S_ISVTX and item.st_uid == 0)):
                raise TrustError("signature_verifier_unavailable")

    def _run_helper(self, argv: list[str]) -> int:
        """Run helper with bounded time and reject any stdout/stderr bytes."""
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd="/", close_fds=True,
                                env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
                                bufsize=0)
        selector = selectors.DefaultSelector()
        assert proc.stdout is not None and proc.stderr is not None
        selector.register(proc.stdout, selectors.EVENT_READ)
        selector.register(proc.stderr, selectors.EVENT_READ)
        deadline = time.monotonic() + self.timeout
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TrustError("signature_verifier_unavailable")
                for key, _ in selector.select(remaining):
                    data = os.read(key.fileobj.fileno(), 4096)
                    if data:
                        raise TrustError("signature_verifier_unavailable")
                    selector.unregister(key.fileobj)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TrustError("signature_verifier_unavailable")
            try:
                return proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                raise TrustError("signature_verifier_unavailable") from None
        except BaseException:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            raise
        finally:
            selector.close()
            proc.stdout.close()
            proc.stderr.close()

    def verify(self, public_key: bytes, message: bytes, signature: bytes) -> None:
        if type(public_key) is not bytes or len(public_key) != 32:
            raise TrustError("signature_invalid")
        if type(signature) is not bytes or len(signature) != 64:
            raise TrustError("signature_invalid")
        if type(message) is not bytes or len(message) > MAX_ENVELOPE_BYTES:
            raise TrustError("signature_invalid")
        if not self.helper_path.is_absolute():
            raise TrustError("signature_verifier_unavailable")
        self._private_ancestors(self.helper_path)
        try:
            helper_stat = self.helper_path.lstat()
        except OSError:
            raise TrustError("signature_verifier_unavailable") from None
        if (stat.S_ISLNK(helper_stat.st_mode) or not stat.S_ISREG(helper_stat.st_mode) or
                helper_stat.st_nlink != 1 or
                helper_stat.st_uid not in (0, os.geteuid()) or helper_stat.st_mode & 0o022):
            raise TrustError("signature_verifier_unavailable")
        directory: Path | None = None
        try:
            parent = self.spool_parent
            if parent is None:
                parent = Path("/tmp")
                try:
                    parent_stat = parent.lstat()
                except OSError:
                    raise TrustError("signature_verifier_unavailable") from None
                if (stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode) or
                        parent_stat.st_uid != 0 or not parent_stat.st_mode & stat.S_ISVTX):
                    raise TrustError("signature_verifier_unavailable")
            else:
                self._private_directory(parent.absolute())
            directory = Path(tempfile.mkdtemp(prefix="xtos-modelverify-", dir=str(parent)))
            os.chmod(directory, 0o700)
            pub = directory / "pubkey"
            msg = directory / "message"
            sig = directory / "signature"
            self._write_private(pub, public_key)
            self._write_private(msg, message)
            self._write_private(sig, signature)
            return_code = self._run_helper(
                [str(self.helper_path), "--pubkey", str(pub), "--message", str(msg),
                 "--signature", str(sig)])
            if return_code == 0:
                return
            if return_code == 1:
                raise TrustError("signature_invalid")
            raise TrustError("signature_verifier_unavailable")
        except TrustError:
            raise
        except (OSError, subprocess.SubprocessError, ValueError):
            raise TrustError("signature_verifier_unavailable") from None
        finally:
            if directory is not None:
                try:
                    shutil.rmtree(directory)
                except OSError:
                    raise TrustError("signature_verifier_unavailable") from None


# ---------- durable whole-checkpoint store ----------


class FilePolicyStore:
    """Owner-only checkpoint adapter using write/fsync/rename/dir-fsync."""

    def __init__(self, path: str | Path):
        self.path = Path(path).absolute()
        self._thread_lock = threading.RLock()

    def _secure_parent(self) -> Path:
        parent = self.path.parent
        current = Path(parent.anchor)
        for component in parent.parts[1:]:
            current = current / component
            try:
                st = current.lstat()
            except FileNotFoundError:
                try:
                    current.mkdir(mode=0o700)
                except FileExistsError:
                    pass
                st = current.lstat()
            if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
                raise TrustError("policy_state_unavailable")
            if st.st_uid not in (0, os.geteuid()):
                raise TrustError("policy_state_unavailable")
            if st.st_mode & 0o022 and not (st.st_mode & stat.S_ISVTX and st.st_uid == 0):
                raise TrustError("policy_state_unavailable")
        if parent.stat().st_uid != os.geteuid() or parent.stat().st_mode & 0o077:
            raise TrustError("policy_state_unavailable")
        return parent

    @contextmanager
    def _locked(self):
        with self._thread_lock:
            parent = self._secure_parent()
            lock_path = parent / (self.path.name + ".lock")
            try:
                fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
            except OSError:
                raise TrustError("policy_state_unavailable") from None
            try:
                st = os.fstat(fd)
                if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or
                        st.st_nlink != 1 or st.st_mode & 0o077):
                    raise TrustError("policy_state_unavailable")
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            except OSError:
                raise TrustError("policy_state_unavailable") from None
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

    def read(self) -> PolicyCheckpoint | None:
        with self._locked():
            return self._read_unlocked()

    def _read_unlocked(self) -> PolicyCheckpoint | None:
        try:
            try:
                st = self.path.lstat()
            except FileNotFoundError:
                return None
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or
                    st.st_nlink != 1 or st.st_mode & 0o077 or st.st_size > MAX_CHECKPOINT_BYTES):
                raise TrustError("policy_state_unavailable")
            fd = os.open(self.path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            try:
                opened = os.fstat(fd)
                if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.geteuid() or
                        opened.st_nlink != 1 or opened.st_mode & 0o077 or
                        (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino)):
                    raise TrustError("policy_state_unavailable")
                chunks = bytearray()
                while len(chunks) <= MAX_CHECKPOINT_BYTES:
                    part = os.read(fd, min(65536, MAX_CHECKPOINT_BYTES + 1 - len(chunks)))
                    if not part:
                        break
                    chunks.extend(part)
                if len(chunks) > MAX_CHECKPOINT_BYTES:
                    raise TrustError("policy_state_unavailable")
                raw = bytes(chunks)
            finally:
                os.close(fd)
            doc, _ = _json_load(raw, max_bytes=MAX_CHECKPOINT_BYTES,
                                max_string_length=MAX_CHECKPOINT_BYTES)
            _exact(doc, {"schema", "policy_bundle", "highest_sequence", "policy_payload_sha256",
                         "revoked_key_ids", "revoked_model_sha256", "last_trusted_utc",
                         "anchor_identity"}, "checkpoint")
            if doc["schema"] != "xtos-ai-policy-checkpoint-v1":
                raise TrustError("policy_state_unavailable")
            bundle = _b64(doc["policy_bundle"], "policy_bundle", len(base64.b64decode(doc["policy_bundle"])))
            highest = _int(doc["highest_sequence"], "highest_sequence")
            digest = _hex(doc["policy_payload_sha256"], "policy_payload_sha256")
            key_ids = _revocation_list(doc["revoked_key_ids"], "revoked_key_ids")
            model_ids = _revocation_list(doc["revoked_model_sha256"], "revoked_model_sha256")
            last = _int(doc["last_trusted_utc"], "last_trusted_utc")
            identity = doc["anchor_identity"]
            if type(identity) is not str or len(identity) > 256:
                raise TrustError("policy_state_unavailable")
            if identity:
                identity = _text(identity, "anchor_identity", 256)
            else:
                identity = None
            return PolicyCheckpoint(bundle, highest, digest, frozenset(key_ids),
                                    frozenset(model_ids), last, identity)
        except TrustError:
            raise
        except (OSError, ValueError, TypeError, binascii.Error):
            raise TrustError("policy_state_unavailable") from None

    def commit(self, checkpoint: PolicyCheckpoint) -> None:
        with self._locked():
            self._commit_unlocked(checkpoint)

    def _commit_unlocked(self, checkpoint: PolicyCheckpoint) -> None:
        if not isinstance(checkpoint, PolicyCheckpoint):
            raise TrustError("policy_state_unavailable")
        if (type(checkpoint.policy_bundle) is not bytes or
                not checkpoint.policy_bundle or len(checkpoint.policy_bundle) > MAX_ENVELOPE_BYTES or
                type(checkpoint.highest_sequence) is not int or
                not 1 <= checkpoint.highest_sequence <= MAX_SEQUENCE or
                type(checkpoint.policy_payload_sha256) is not str or
                not _HEX64.fullmatch(checkpoint.policy_payload_sha256) or
                type(checkpoint.revoked_key_ids) is not frozenset or
                type(checkpoint.revoked_model_sha256) is not frozenset or
                len(checkpoint.revoked_key_ids) > MAX_REVOKED or
                len(checkpoint.revoked_model_sha256) > MAX_REVOKED or
                any(not _HEX64.fullmatch(item) for item in checkpoint.revoked_key_ids) or
                any(not _HEX64.fullmatch(item) for item in checkpoint.revoked_model_sha256) or
                type(checkpoint.last_trusted_utc) is not int or checkpoint.last_trusted_utc < 0 or
                (checkpoint.anchor_identity is not None and
                 (type(checkpoint.anchor_identity) is not str or
                  not checkpoint.anchor_identity))):
            raise TrustError("policy_state_unavailable")
        parent = self._secure_parent()
        old = self._read_unlocked()
        if old is not None:
            if checkpoint.highest_sequence < old.highest_sequence:
                raise TrustError("policy_rollback")
            if (checkpoint.highest_sequence == old.highest_sequence and
                    checkpoint.policy_payload_sha256 != old.policy_payload_sha256):
                raise TrustError("policy_equivocation")
            if checkpoint.last_trusted_utc < old.last_trusted_utc:
                raise TrustError("policy_time_untrusted")
            if not checkpoint.revoked_key_ids.issuperset(old.revoked_key_ids):
                raise TrustError("policy_state_unavailable")
            if not checkpoint.revoked_model_sha256.issuperset(old.revoked_model_sha256):
                raise TrustError("policy_state_unavailable")
        document = {
            "schema": "xtos-ai-policy-checkpoint-v1",
            "policy_bundle": base64.b64encode(checkpoint.policy_bundle).decode("ascii"),
            "highest_sequence": checkpoint.highest_sequence,
            "policy_payload_sha256": checkpoint.policy_payload_sha256,
            "revoked_key_ids": sorted(checkpoint.revoked_key_ids),
            "revoked_model_sha256": sorted(checkpoint.revoked_model_sha256),
            "last_trusted_utc": checkpoint.last_trusted_utc,
            "anchor_identity": checkpoint.anchor_identity or "",
        }
        encoded = _canonical(document, max_string_length=MAX_CHECKPOINT_BYTES)
        if len(encoded) > MAX_CHECKPOINT_BYTES:
            raise TrustError("policy_state_unavailable")
        try:
            leaf = self.path.lstat()
        except FileNotFoundError:
            leaf = None
        if leaf is not None:
            if (not stat.S_ISREG(leaf.st_mode) or leaf.st_uid != os.geteuid() or
                    leaf.st_nlink != 1 or leaf.st_mode & 0o077):
                raise TrustError("policy_state_unavailable")
        tmp_fd = -1
        tmp_name: Path | None = None
        try:
            tmp_fd, tmp = tempfile.mkstemp(prefix=".checkpoint-", dir=str(parent))
            tmp_name = Path(tmp)
            os.fchmod(tmp_fd, 0o600)
            with os.fdopen(tmp_fd, "wb", closefd=True) as stream:
                tmp_fd = -1
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_name, self.path)
            tmp_name = None
            dir_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except (OSError, ValueError):
            raise TrustError("policy_state_unavailable") from None
        finally:
            if tmp_fd >= 0:
                os.close(tmp_fd)
            if tmp_name is not None:
                try:
                    tmp_name.unlink()
                except OSError:
                    pass


# ---------- signed policy and model validation ----------


def _revocation_list(value: Any, label: str) -> list[str]:
    if type(value) is not list or len(value) > MAX_REVOKED:
        raise TrustError("malformed_envelope", label)
    result = [_hex(item, label) for item in value]
    if result != sorted(set(result)):
        raise TrustError("malformed_envelope", label)
    return result


def _parse_envelope(raw: bytes | str | Mapping[str, Any], schema: str, domain: bytes,
                    verifier: SignatureVerifier | None = None,
                    public_key: bytes | None = None) -> tuple[dict[str, Any], bytes, str]:
    doc, canonical = _json_load(raw)
    if "signature" not in doc:
        raise TrustError("signature_required")
    _exact(doc, {"schema", "key_id", "claims", "signature"}, "envelope")
    if doc["schema"] != schema or type(doc["claims"]) is not dict:
        raise TrustError("malformed_envelope")
    key_id = _hex(doc["key_id"], "key_id")
    signature = _signature(doc["signature"])
    body = {"schema": doc["schema"], "key_id": key_id, "claims": doc["claims"]}
    body_bytes = _canonical(body)
    # Policy envelopes have a fixed root key. Model envelopes are first parsed
    # structurally, then verified exactly once against the publisher key from
    # the already-validated policy.
    if public_key is not None:
        _verify(verifier, public_key, domain + body_bytes, signature)
    return doc, body_bytes, key_id


def _parse_policy(raw: bytes | str | Mapping[str, Any], root_key: bytes,
                  verifier: SignatureVerifier | None) -> tuple[PolicySnapshot, dict[str, Any]]:
    doc, body_bytes, key_id = _parse_envelope(raw, POLICY_SCHEMA, POLICY_DOMAIN, verifier, root_key)
    if key_id != _fingerprint(root_key):
        raise TrustError("signature_invalid")
    claims = doc["claims"]
    _exact(claims, {"sequence", "issued_at", "expires_at", "publisher_keys", "revoked_key_ids",
                    "revoked_model_sha256", "allowed_licenses"}, "policy claims")
    sequence = _int(claims["sequence"], "sequence", 1)
    issued = _int(claims["issued_at"], "issued_at")
    expires = _int(claims["expires_at"], "expires_at")
    if not issued < expires or expires - issued > MAX_POLICY_LIFETIME:
        raise TrustError("malformed_envelope")
    entries = claims["publisher_keys"]
    if type(entries) is not list or len(entries) > MAX_POLICY_KEYS:
        raise TrustError("malformed_envelope")
    keys: dict[str, bytes] = {}
    windows: dict[str, tuple[int, int]] = {}
    for entry in entries:
        _exact(entry, {"key_id", "public_key", "not_before", "expires_at"}, "publisher key")
        entry_id = _hex(entry["key_id"], "publisher key id")
        public = _b64(entry["public_key"], "public_key", 32)
        if entry_id != _fingerprint(public) or entry_id in keys:
            raise TrustError("malformed_envelope")
        not_before = _int(entry["not_before"], "key not_before")
        key_expires = _int(entry["expires_at"], "key expires_at")
        if not_before >= key_expires:
            raise TrustError("malformed_envelope")
        keys[entry_id] = public
        windows[entry_id] = (not_before, key_expires)
    revoked_keys = frozenset(_revocation_list(claims["revoked_key_ids"], "revoked_key_ids"))
    revoked_models = frozenset(_revocation_list(claims["revoked_model_sha256"], "revoked_model_sha256"))
    licenses = claims["allowed_licenses"]
    if type(licenses) is not list or len(licenses) > MAX_LICENSES:
        raise TrustError("malformed_envelope")
    license_set = frozenset(_text(item, "allowed license", 256) for item in licenses)
    if len(license_set) != len(licenses):
        raise TrustError("malformed_envelope")
    payload_sha = hashlib.sha256(body_bytes).hexdigest()
    snapshot = PolicySnapshot(sequence, issued, expires, payload_sha,
                              MappingProxyType(keys), MappingProxyType(windows),
                              revoked_keys, revoked_models, license_set,
                              _canonical(doc))
    return snapshot, claims


def _safe_model_path(model_root: Path, value: Any) -> str:
    path = _validate_relative_model_path(value)
    root = model_root.resolve()
    current = root
    for part in path.split("/"):
        current = current / part
        try:
            if current.is_symlink():
                raise TrustError("malformed_envelope")
        except OSError:
            raise TrustError("model_bytes_changed") from None
    return path


def _validate_relative_model_path(value: Any) -> str:
    path = _text(value, "model_path", 4096)
    if path.startswith("/") or "\\" in path or "\x00" in path:
        raise TrustError("malformed_envelope")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise TrustError("malformed_envelope")
    return path


def _claim_tensor(value: Any, label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != {"name", "dtype", "shape"}:
        raise TrustError("malformed_envelope")
    _text(value["name"], label + ".name", 256)
    if value["dtype"] != "float32":
        raise TrustError("malformed_envelope")
    shape = value["shape"]
    if type(shape) is not list or len(shape) != 2 or any(type(dim) is not int or not 1 <= dim <= 65536 for dim in shape):
        raise TrustError("malformed_envelope")
    if shape[0] > 64 or shape[0] * shape[1] > 65536:
        raise TrustError("malformed_envelope")
    return value


def _validate_model_claims(claims: dict[str, Any], snapshot: PolicySnapshot, now: int,
                           expected_arch: str) -> None:
    _exact(claims, {"name", "version", "model_path", "sha256", "format", "provider",
                    "min_runtime_version", "license", "architecture", "threads", "input",
                    "output", "not_before", "expires_at"}, "model claims")
    _text(claims["name"], "name", 128)
    _semver(claims["version"], "version")
    _validate_relative_model_path(claims["model_path"])
    _hex(claims["sha256"], "sha256")
    if claims["format"] != "onnx":
        raise TrustError("model_format_unsupported")
    if claims["provider"] != "CPUExecutionProvider":
        raise TrustError("malformed_envelope")
    minimum = _text(claims["min_runtime_version"], "min_runtime_version", 64)
    _semver(minimum, "min_runtime_version")
    if not _version_at_least(RUNTIME_VERSION, minimum):
        raise TrustError("runtime_version_unsupported")
    license_value = _text(claims["license"], "license", 256)

    if license_value not in snapshot.allowed_licenses:
        raise TrustError("license_not_allowed")
    if expected_arch != "x86_64" or claims["architecture"] != "x86_64":
        raise TrustError("architecture_mismatch")
    threads = claims["threads"]
    if type(threads) is not dict or set(threads) != {"intra_op", "inter_op"}:
        raise TrustError("malformed_envelope")
    for value in threads.values():
        if type(value) is not int or not 1 <= value <= 64:
            raise TrustError("malformed_envelope")
    _claim_tensor(claims["input"], "input")
    _claim_tensor(claims["output"], "output")
    not_before = _int(claims["not_before"], "not_before")
    expires = _int(claims["expires_at"], "expires_at")
    if not not_before < expires:
        raise TrustError("malformed_envelope")
    if now < not_before:
        raise TrustError("model_not_yet_valid")
    if now >= expires:
        raise TrustError("model_signature_expired")


# ---------- policy object ----------


class FailClosedPolicy:
    """Unconfigured deployments expose diagnostics but cannot execute models."""
    mode = "production"
    status = "policy_unavailable"

    def authorize(self, raw, model_root):
        # Retain malformed legacy-manifest diagnostics without granting a ticket.
        try:
            validate_manifest(raw, model_root)
        except ManifestError:
            raise
        raise TrustError("policy_unavailable")

    def check(self, model):
        raise TrustError("policy_unavailable")

    def tick(self):
        raise TrustError("policy_unavailable")

    def install_policy(self, bundle):
        raise TrustError("policy_unavailable")


class ProductionTrustPolicy:
    """Independent signed model/policy admission gate.

    Missing dependencies are not replaced with process time, RAM state, or an
    unsigned fixture.  A caller wanting fixtures must explicitly inject its
    own verifier, clock, store, and anchor, as the tests do.
    """

    def __init__(self, policy_root_public_key: bytes, *, verifier: SignatureVerifier | None = None,
                 time_source: TimeSource | None = None, store: PolicyStore | None = None,
                 anchor: RollbackAnchor | None = None, expected_arch: str | None = None,
                 minimum_policy_sequence: int | None = None):
        if type(policy_root_public_key) is not bytes or len(policy_root_public_key) != 32:
            raise ValueError("policy_root_public_key must be raw Ed25519 bytes")
        if (minimum_policy_sequence is not None and
                (type(minimum_policy_sequence) is not int or
                 not 1 <= minimum_policy_sequence <= MAX_SEQUENCE)):
            raise ValueError("minimum_policy_sequence must be a positive int")

        self.root_public_key = policy_root_public_key
        self.verifier = verifier
        self.time_source = time_source
        self.store = store
        self.anchor = anchor
        # P1 is intentionally x86_64-only.  A caller cannot widen the signed
        # architecture allowlist through a constructor argument.
        self.expected_arch = _machine_arch()
        self.minimum_policy_sequence = minimum_policy_sequence

        self._lock = threading.RLock()
        self._snapshot: PolicySnapshot | None = None
        self._authorized: dict[int, AuthorizedModel] = {}
        self._deny_reasons: dict[int, str] = {}
        self._boot_utc: int | None = None
        self._boot_boottime: float | None = None
        self._trusted_utc_highwater: int | None = None

    def _reading(self) -> ClockReading:
        if self.time_source is None:
            raise TrustError("policy_time_untrusted")
        try:
            reading = self.time_source.read()
        except Exception:
            raise TrustError("policy_time_untrusted") from None
        if (not isinstance(reading, ClockReading) or reading.trusted is not True or
                type(reading.utc_seconds) is not int or reading.utc_seconds < 0 or
                type(reading.boottime_seconds) not in (int, float) or
                not math.isfinite(float(reading.boottime_seconds))):
            raise TrustError("policy_time_untrusted")
        boot = float(reading.boottime_seconds)
        if self._boot_utc is None:
            self._boot_utc = reading.utc_seconds
            self._boot_boottime = boot
        elif boot < self._boot_boottime:  # type: ignore[operator]
            raise TrustError("policy_time_untrusted")
        monotonic_utc = self._boot_utc + int(boot - self._boot_boottime)  # type: ignore[operator]
        if reading.utc_seconds > monotonic_utc:
            self._boot_utc = reading.utc_seconds
            self._boot_boottime = boot
            monotonic_utc = reading.utc_seconds
        candidate = max(reading.utc_seconds, monotonic_utc)
        if (self._trusted_utc_highwater is None or
                candidate > self._trusted_utc_highwater):
            self._trusted_utc_highwater = candidate
        return ClockReading(self._trusted_utc_highwater, boot,
                            reading.trusted, reading.source)

    def _checkpoint(self, *, enforce_minimum: bool = True) -> PolicyCheckpoint:
        if (self.store is None or self.anchor is None or
                self.minimum_policy_sequence is None):
            raise TrustError("policy_state_unavailable")
        try:
            checkpoint = self.store.read()
        except TrustError:
            raise
        except Exception:
            raise TrustError("policy_state_unavailable") from None
        if not isinstance(checkpoint, PolicyCheckpoint):
            raise TrustError("policy_state_unavailable")
        if type(checkpoint.policy_bundle) is not bytes or not checkpoint.policy_bundle:
            raise TrustError("policy_state_unavailable")
        if len(checkpoint.policy_bundle) > MAX_ENVELOPE_BYTES:
            raise TrustError("policy_state_unavailable")
        if (type(checkpoint.highest_sequence) is not int or
                checkpoint.highest_sequence < 1 or checkpoint.highest_sequence > MAX_SEQUENCE):
            raise TrustError("policy_state_unavailable")
        if (enforce_minimum and self.minimum_policy_sequence is not None and
                checkpoint.highest_sequence < self.minimum_policy_sequence):
            raise TrustError("policy_rollback")

        if (type(checkpoint.policy_payload_sha256) is not str or
                not _HEX64.fullmatch(checkpoint.policy_payload_sha256)):
            raise TrustError("policy_state_unavailable")
        if (type(checkpoint.revoked_key_ids) is not frozenset or
                type(checkpoint.revoked_model_sha256) is not frozenset or
                len(checkpoint.revoked_key_ids) > MAX_REVOKED or
                len(checkpoint.revoked_model_sha256) > MAX_REVOKED or
                any(not _HEX64.fullmatch(item) for item in checkpoint.revoked_key_ids) or
                any(not _HEX64.fullmatch(item) for item in checkpoint.revoked_model_sha256) or
                type(checkpoint.last_trusted_utc) is not int or checkpoint.last_trusted_utc < 0):
            raise TrustError("policy_state_unavailable")
        try:
            anchored = self.anchor.read()
        except Exception:
            raise TrustError("policy_state_unavailable") from None
        if (not isinstance(anchored, tuple) or len(anchored) != 2 or
                type(anchored[0]) is not int or type(anchored[1]) is not str):
            raise TrustError("policy_state_unavailable")
        anchor_seq, anchor_digest = anchored
        if anchor_seq < 1 or not _HEX64.fullmatch(anchor_digest):
            raise TrustError("policy_state_unavailable")
        if anchor_seq > checkpoint.highest_sequence:
            raise TrustError("policy_rollback")
        if (anchor_seq != checkpoint.highest_sequence or
                anchor_digest != checkpoint.policy_payload_sha256):
            raise TrustError("policy_state_unavailable")
        return checkpoint

    def _snapshot_from_checkpoint(self, checkpoint: PolicyCheckpoint, now: int,
                                  *, allow_expired: bool = False) -> PolicySnapshot:
        try:
            snapshot, _ = _parse_policy(checkpoint.policy_bundle, self.root_public_key, self.verifier)
        except TrustError as error:
            if error.reason == "signature_verifier_unavailable":
                raise
            raise TrustError("policy_state_unavailable") from None
        if snapshot.sequence != checkpoint.highest_sequence or snapshot.payload_sha256 != checkpoint.policy_payload_sha256:
            raise TrustError("policy_state_unavailable")
        if not checkpoint.revoked_key_ids.issuperset(snapshot.revoked_key_ids):
            raise TrustError("policy_state_unavailable")
        if not checkpoint.revoked_model_sha256.issuperset(snapshot.revoked_model_sha256):
            raise TrustError("policy_state_unavailable")
        union = PolicySnapshot(snapshot.sequence, snapshot.issued_at, snapshot.expires_at,
                               snapshot.payload_sha256, snapshot.publisher_keys,
                               snapshot.publisher_windows,
                               frozenset(checkpoint.revoked_key_ids),
                               frozenset(checkpoint.revoked_model_sha256),
                               snapshot.allowed_licenses, snapshot.envelope)
        if now < union.issued_at:
            raise TrustError("policy_time_untrusted")
        if not allow_expired and now >= union.expires_at:
            raise TrustError("policy_expired")
        return union

    def _current(self, *, allow_expired: bool = False) -> tuple[PolicySnapshot, ClockReading, PolicyCheckpoint]:
        reading = self._reading()
        checkpoint = self._checkpoint()
        # A backwards trusted clock cannot extend the cached checkpoint.
        if checkpoint.last_trusted_utc and reading.utc_seconds < checkpoint.last_trusted_utc:
            raise TrustError("policy_time_untrusted")
        snapshot = self._snapshot_from_checkpoint(checkpoint, reading.utc_seconds,
                                                  allow_expired=allow_expired)
        self._snapshot = snapshot
        return snapshot, reading, checkpoint

    def install_policy(self, bundle: bytes | str | Mapping[str, Any]) -> PolicySnapshot:
        with self._lock:
            try:
                reading = self._reading()
                checkpoint = self._checkpoint(enforce_minimum=False)
                if checkpoint.last_trusted_utc and reading.utc_seconds < checkpoint.last_trusted_utc:
                    raise TrustError("policy_time_untrusted")
                # Never let a new bundle silently bootstrap over a corrupt
                # durable checkpoint. Recovery requires explicit trusted
                # provisioning outside this policy update path.
                current_snapshot = self._snapshot_from_checkpoint(
                    checkpoint, reading.utc_seconds, allow_expired=True)
            except TrustError as error:
                self._deny_all(error.reason)
                raise
            try:
                incoming, _ = _parse_policy(bundle, self.root_public_key, self.verifier)
                if isinstance(bundle, bytes):
                    bundle_bytes = bundle
                elif isinstance(bundle, str):
                    bundle_bytes = bundle.encode("utf-8")
                elif isinstance(bundle, Mapping):
                    bundle_bytes = _canonical(dict(bundle))
                else:
                    raise TrustError("malformed_envelope")
            except TrustError:
                # A malformed/invalid incoming policy must not replace state.
                raise
            if reading.utc_seconds < incoming.issued_at:
                raise TrustError("policy_time_untrusted")
            if reading.utc_seconds >= incoming.expires_at:
                raise TrustError("policy_expired")
            if incoming.sequence < checkpoint.highest_sequence:
                raise TrustError("policy_rollback")
            if (self.minimum_policy_sequence is not None and
                    incoming.sequence < self.minimum_policy_sequence):
                raise TrustError("policy_rollback")
            if incoming.sequence == checkpoint.highest_sequence:
                if incoming.payload_sha256 == checkpoint.policy_payload_sha256:
                    current, _, _ = self._current()
                    return current
                raise TrustError("policy_equivocation")
            if incoming.sequence < 1:
                raise TrustError("policy_rollback")
            union_keys = frozenset(checkpoint.revoked_key_ids | incoming.revoked_key_ids)
            removed_keys = frozenset(set(checkpoint.revoked_key_ids) |
                                     (set(current_snapshot.publisher_keys) - set(incoming.publisher_keys)))
            union_keys = frozenset(union_keys | removed_keys)
            union_models = frozenset(checkpoint.revoked_model_sha256 | incoming.revoked_model_sha256)
            if len(union_keys) > MAX_REVOKED or len(union_models) > MAX_REVOKED:
                raise TrustError("malformed_envelope")
            new_checkpoint = PolicyCheckpoint(
                bundle_bytes,
                incoming.sequence, incoming.payload_sha256, union_keys, union_models,
                reading.utc_seconds)
            try:
                self.store.commit(new_checkpoint)  # type: ignore[union-attr]
                self.anchor.advance(incoming.sequence, incoming.payload_sha256)  # type: ignore[union-attr]
                committed = self.store.read()  # type: ignore[union-attr]
            except TrustError as error:
                self._deny_all(error.reason)
                raise
            except Exception:
                self._deny_all("policy_state_unavailable")
                raise TrustError("policy_state_unavailable") from None
            if committed != new_checkpoint:
                self._deny_all("policy_state_unavailable")
                raise TrustError("policy_state_unavailable")
            try:
                anchored = self.anchor.read()
            except Exception:
                self._deny_all("policy_state_unavailable")
                raise TrustError("policy_state_unavailable") from None
            if anchored != (incoming.sequence, incoming.payload_sha256):
                self._deny_all("policy_state_unavailable")
                raise TrustError("policy_state_unavailable")

            snapshot = PolicySnapshot(incoming.sequence, incoming.issued_at, incoming.expires_at,
                                      incoming.payload_sha256, incoming.publisher_keys,
                                      incoming.publisher_windows, union_keys, union_models,
                                      incoming.allowed_licenses, incoming.envelope)
            self._snapshot = snapshot
            self._apply_revocations(snapshot)
            return snapshot

    def authorize(self, raw: bytes | str | Mapping[str, Any], model_root: str | Path) -> AuthorizedModel:
        with self._lock:
            try:
                snapshot, reading, _ = self._current()
            except TrustError as error:
                self._deny_all(error.reason)
                raise
            doc, body_bytes, key_id = _parse_envelope(raw, MODEL_SCHEMA, MODEL_DOMAIN)
            # The model verifier key is selected only after policy validation;
            # parse_envelope's dummy check is replaced by the real key check.
            claims = doc["claims"]
            if key_id in snapshot.revoked_key_ids:
                raise TrustError("signing_key_revoked")
            public_key = snapshot.publisher_keys.get(key_id)
            if public_key is None:
                raise TrustError("unknown_signing_key")
            not_before, expires = snapshot.publisher_windows[key_id]
            if reading.utc_seconds < not_before:
                raise TrustError("signing_key_not_yet_valid")
            if reading.utc_seconds >= expires:
                raise TrustError("signing_key_expired")
            # Re-verify with the policy-selected publisher key.  The first
            # parse above intentionally performs no trust decision for a model.
            signature = _signature(doc["signature"])
            _verify(self.verifier, public_key, MODEL_DOMAIN + body_bytes, signature)
            _validate_model_claims(claims, snapshot, reading.utc_seconds, self.expected_arch)
            root = Path(model_root).resolve()
            path_text = _safe_model_path(root, claims["model_path"])
            manifest_raw = {
                "name": claims["name"], "version": claims["version"],
                "model_path": path_text, "sha256": claims["sha256"],
                "provider": claims["provider"], "threads": claims["threads"],
                "input": claims["input"], "output": claims["output"],
            }
            try:
                manifest = validate_manifest(manifest_raw, root)
            except ManifestError as exc:
                raise TrustError("model_bytes_changed" if "sha256" in str(exc) else "malformed_envelope") from exc
            digest = hashlib.sha256(body_bytes).hexdigest()
            if manifest.sha256 in snapshot.revoked_model_sha256:
                raise TrustError("model_revoked")
            event = threading.Event()
            authorized = AuthorizedModel(manifest, key_id, digest, snapshot.sequence, event,
                                         _freeze(claims))
            self._authorized[id(authorized)] = authorized
            try:
                self._apply_revocations(snapshot)
                self.check(authorized)
            except TrustError:
                self._authorized.pop(id(authorized), None)
                self._deny_reasons.pop(id(authorized), None)
                raise
            return authorized

    def retire(self, model: AuthorizedModel) -> None:
        """Release a caller-owned ticket once no worker or request uses it."""
        with self._lock:
            if self._authorized.get(id(model)) is model:
                model.deny_event.set()
                self._authorized.pop(id(model))
                self._deny_reasons.pop(id(model), None)

    def check(self, model: AuthorizedModel) -> None:
        if not isinstance(model, AuthorizedModel):
            raise TrustError("malformed_envelope")
        with self._lock:
            if self._authorized.get(id(model)) is not model:
                raise TrustError("malformed_envelope")
            if model.deny_event.is_set() or id(model) in self._deny_reasons:
                raise TrustError(self._deny_reasons.get(id(model), "model_revoked"))
            try:
                snapshot, reading, _ = self._current()
            except TrustError as error:
                self._deny_all(error.reason)
                raise
            if model.policy_sequence > snapshot.sequence:
                raise TrustError("policy_state_unavailable")
            if model.key_id in snapshot.revoked_key_ids:
                model.deny_event.set()
                raise TrustError("signing_key_revoked")
            if model.key_id not in snapshot.publisher_keys:
                model.deny_event.set()
                raise TrustError("unknown_signing_key")
            if model.manifest.sha256 in snapshot.revoked_model_sha256:
                model.deny_event.set()
                raise TrustError("model_revoked")
            if reading.utc_seconds >= snapshot.expires_at:
                model.deny_event.set()
                raise TrustError("policy_expired")
            expires = model.claims.get("expires_at")
            if type(expires) is not int or reading.utc_seconds >= expires:
                self._deny_reasons[id(model)] = "model_signature_expired"
                model.deny_event.set()
                raise TrustError("model_signature_expired")
            key_window = snapshot.publisher_windows.get(model.key_id)
            if key_window is None:
                self._deny_reasons[id(model)] = "signing_key_revoked"
                model.deny_event.set()
                raise TrustError("signing_key_revoked")
            if reading.utc_seconds < key_window[0]:
                self._deny_reasons[id(model)] = "signing_key_not_yet_valid"
                model.deny_event.set()
                raise TrustError("signing_key_not_yet_valid")
            if reading.utc_seconds >= key_window[1]:
                self._deny_reasons[id(model)] = "signing_key_expired"
                model.deny_event.set()
                raise TrustError("signing_key_expired")

    def tick(self) -> None:
        with self._lock:
            try:
                snapshot, reading, _ = self._current()
            except TrustError as error:
                self._deny_all(error.reason)
                raise
            self._apply_revocations(snapshot)
            if reading.utc_seconds >= snapshot.expires_at:
                self._deny_all()
                raise TrustError("policy_expired")
            for model in self._authorized.values():
                if not model.deny_event.is_set() and reading.utc_seconds >= model.claims["expires_at"]:
                    self._deny_reasons[id(model)] = "model_signature_expired"
                    model.deny_event.set()
                key_window = snapshot.publisher_windows.get(model.key_id)
                if (not model.deny_event.is_set() and key_window is not None and
                        reading.utc_seconds < key_window[0]):
                    self._deny_reasons[id(model)] = "signing_key_not_yet_valid"
                    model.deny_event.set()
                elif (not model.deny_event.is_set() and key_window is not None and
                      reading.utc_seconds >= key_window[1]):
                    self._deny_reasons[id(model)] = "signing_key_expired"
                    model.deny_event.set()

    def _apply_revocations(self, snapshot: PolicySnapshot) -> None:
        for model in self._authorized.values():
            reason = None
            if model.key_id in snapshot.revoked_key_ids:
                reason = "signing_key_revoked"
            elif model.key_id not in snapshot.publisher_keys:
                reason = "unknown_signing_key"
            elif model.manifest.sha256 in snapshot.revoked_model_sha256:
                reason = "model_revoked"
            if reason is not None:
                self._deny_reasons.setdefault(id(model), reason)
                model.deny_event.set()

    def _deny_all(self, reason: str = "model_revoked") -> None:
        for model in self._authorized.values():
            self._deny_reasons.setdefault(id(model), reason)
            model.deny_event.set()


def _machine_arch() -> str:
    value = platform.machine().lower()
    if value in ("amd64", "x64"):
        return "x86_64"
    return value


__all__ = [
    "AuthorizedModel", "ClockReading", "FilePolicyStore", "NativeEd25519Verifier",
    "PolicyCheckpoint", "PolicySnapshot", "ProductionTrustPolicy", "RUNTIME_VERSION",
    "TrustError",
]
