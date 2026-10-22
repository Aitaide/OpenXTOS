"""Explicit test-only trust fixtures using real OpenSSL Ed25519 signatures."""
from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "tools/xtos-ai-runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from xtos_ai_runtime.trust import (  # noqa: E402
    ClockReading,
    PolicyCheckpoint,
    ProductionTrustPolicy,
    TrustError,
)

OPENSSL = Path(shutil.which("openssl") or "/usr/bin/openssl").resolve()
_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


@dataclass(frozen=True)
class Ed25519KeyPair:
    private_pem: Path
    public_raw: bytes
    work_dir: Path

    @classmethod
    def create(cls, parent: Path, name: str) -> "Ed25519KeyPair":
        work = parent / name
        work.mkdir(mode=0o700)
        private = work / "private.pem"
        public_pem = work / "public.pem"
        subprocess.run([str(OPENSSL), "genpkey", "-algorithm", "ED25519",
                        "-out", str(private)], check=True, capture_output=True, timeout=5)
        subprocess.run([str(OPENSSL), "pkey", "-in", str(private), "-pubout",
                        "-out", str(public_pem)], check=True, capture_output=True, timeout=5)
        der = subprocess.run([str(OPENSSL), "pkey", "-pubin", "-in", str(public_pem),
                              "-outform", "DER"], check=True, capture_output=True, timeout=5).stdout
        if len(der) != 44 or not der.startswith(_SPKI_PREFIX):
            raise RuntimeError("unexpected OpenSSL Ed25519 SPKI encoding")
        return cls(private, der[-32:], work)

    def sign(self, message: bytes) -> bytes:
        msg = self.work_dir / "to-sign.bin"
        sig = self.work_dir / "signature.bin"
        msg.write_bytes(message)
        subprocess.run([str(OPENSSL), "pkeyutl", "-sign", "-inkey", str(self.private_pem),
                        "-rawin", "-in", str(msg), "-out", str(sig)],
                       check=True, capture_output=True, timeout=5)
        return sig.read_bytes()


class OpenSSLTestVerifier:
    """Test injection only: fixed absolute OpenSSL argv, no PATH fallback at verify time."""
    def verify(self, public_key: bytes, message: bytes, signature: bytes) -> None:
        if len(public_key) != 32 or len(signature) != 64:
            raise TrustError("signature_invalid")
        with tempfile.TemporaryDirectory(prefix="xtos-trust-verify-") as directory:
            root = Path(directory)
            pub = root / "public.pem"
            msg = root / "message.bin"
            sig = root / "signature.bin"
            pub.write_bytes(b"-----BEGIN PUBLIC KEY-----\n" +
                            base64.encodebytes(_SPKI_PREFIX + public_key) +
                            b"-----END PUBLIC KEY-----\n")
            msg.write_bytes(message)
            sig.write_bytes(signature)
            result = subprocess.run(
                [str(OPENSSL), "pkeyutl", "-verify", "-pubin", "-inkey", str(pub),
                 "-rawin", "-in", str(msg), "-sigfile", str(sig)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, timeout=3, check=False)
            if result.returncode != 0:
                raise TrustError("signature_invalid")


class FakeTime:
    def __init__(self, now: int, trusted: bool = True):
        self.now = now
        self.trusted = trusted
        self.boottime = 100.0

    def read(self) -> ClockReading:
        return ClockReading(self.now, self.boottime, self.trusted, "test-fixture")


class MemoryStore:
    """Explicit in-process test store; not a production persistence adapter."""
    def __init__(self, checkpoint: PolicyCheckpoint | None = None):
        import threading
        self.checkpoint = checkpoint
        self.commit_count = 0
        self._lock = threading.RLock()

    def read(self) -> PolicyCheckpoint | None:
        with self._lock:
            return self.checkpoint

    def commit(self, checkpoint: PolicyCheckpoint) -> None:
        with self._lock:
            self.checkpoint = checkpoint
            self.commit_count += 1

    def locked(self):
        return self._lock


class MemoryAnchor:
    """Explicit test-only anchor; its state is provisioned separately from the store."""
    def __init__(self, sequence: int, payload_sha256: str):
        self.sequence = sequence
        self.payload_sha256 = payload_sha256

    def read(self) -> tuple[int, str] | None:
        return self.sequence, self.payload_sha256

    def advance(self, sequence: int, payload_sha256: str) -> None:
        if sequence < self.sequence:
            raise TrustError("policy_rollback")
        self.sequence = sequence
        self.payload_sha256 = payload_sha256


def signed_envelope(schema: str, key_id: str, claims: dict[str, Any], signer: Ed25519KeyPair,
                    domain: bytes) -> bytes:
    body = {"schema": schema, "key_id": key_id, "claims": claims}
    message = domain + _canonical(body)
    envelope = {**body, "signature": base64.b64encode(signer.sign(message)).decode("ascii")}
    return _canonical(envelope)


def policy_claims(now: int, publisher: Ed25519KeyPair, *, sequence: int = 1,
                  revoked_keys: list[str] | None = None,
                  revoked_models: list[str] | None = None,
                  licenses: list[str] | None = None,
                  expires_at: int | None = None) -> dict[str, Any]:
    key_id = hashlib.sha256(publisher.public_raw).hexdigest()
    return {
        "sequence": sequence,
        "issued_at": now - 5,
        "expires_at": expires_at if expires_at is not None else now + 3600,
        "publisher_keys": [{"key_id": key_id,
                             "public_key": base64.b64encode(publisher.public_raw).decode("ascii"),
                             "not_before": now - 60, "expires_at": now + 1800}],
        "revoked_key_ids": sorted(revoked_keys or []),
        "revoked_model_sha256": sorted(revoked_models or []),
        "allowed_licenses": sorted(licenses if licenses is not None else ["MIT"]),
    }


def model_claims(now: int, model_path: str, model_bytes: bytes, **changes: Any) -> dict[str, Any]:
    claims: dict[str, Any] = {
        "name": "fixed-classifier", "version": "1.0.0", "model_path": model_path,
        "sha256": hashlib.sha256(model_bytes).hexdigest(), "format": "onnx",
        "provider": "CPUExecutionProvider", "min_runtime_version": "1.0.0",
        "license": "MIT", "architecture": "x86_64", "threads": {"intra_op": 1, "inter_op": 1},
        "input": {"name": "input", "dtype": "float32", "shape": [1, 4]},
        "output": {"name": "probabilities", "dtype": "float32", "shape": [1, 3]},
        "not_before": now - 10, "expires_at": now + 100,
    }
    claims.update(changes)
    return claims


@dataclass
class TrustFixture:
    root: Path
    model_root: Path
    now: int
    model_bytes: bytes
    root_signer: Ed25519KeyPair
    publisher: Ed25519KeyPair
    verifier: OpenSSLTestVerifier
    clock: FakeTime
    store: MemoryStore
    anchor: MemoryAnchor
    policy: ProductionTrustPolicy
    policy_bundle: bytes
    policy_payload_sha256: str


def make_fixture(root: Path, *, now: int = 1_800_000_000) -> TrustFixture:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    model_root = root / "models"
    model_root.mkdir(mode=0o700)
    model_bytes = b"signed fixture bytes; trust layer only"
    (model_root / "model.onnx").write_bytes(model_bytes)
    root_signer = Ed25519KeyPair.create(root, "root-key")
    publisher = Ed25519KeyPair.create(root, "publisher-key")
    root_id = hashlib.sha256(root_signer.public_raw).hexdigest()
    claims = policy_claims(now, publisher)
    bundle = signed_envelope("xtos-ai-policy-v1", root_id, claims, root_signer,
                             b"XTOS-AI-POLICY-v1\x00")
    import json
    envelope = json.loads(bundle)
    payload = _canonical({"schema": envelope["schema"], "key_id": envelope["key_id"],
                          "claims": envelope["claims"]})
    payload_sha = hashlib.sha256(payload).hexdigest()
    checkpoint = PolicyCheckpoint(bundle, 1, payload_sha, frozenset(), frozenset(), now)
    store = MemoryStore(checkpoint)
    anchor = MemoryAnchor(1, payload_sha)
    clock = FakeTime(now)
    verifier = OpenSSLTestVerifier()
    policy = ProductionTrustPolicy(root_signer.public_raw, verifier=verifier,
                                   time_source=clock, store=store, anchor=anchor,
                                   minimum_policy_sequence=1)
    return TrustFixture(root, model_root, now, model_bytes, root_signer, publisher, verifier,
                        clock, store, anchor, policy, bundle, payload_sha)
