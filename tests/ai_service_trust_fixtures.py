"""Explicit scheduler/process fixtures; never a production policy."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import threading
from xtos_ai_runtime.protocol import validate_manifest
from xtos_ai_runtime.trust import AuthorizedModel, TrustError

ROOT = Path(__file__).resolve().parents[1]


def fixture_ticket(manifest=None, name=None):
    if manifest is None:
        raw = json.loads((ROOT / "tools/xtos-ai-runtime/model-manifest.example.json").read_text())
        manifest = validate_manifest(raw, ROOT)
    if name is not None:
        manifest = replace(manifest, name=name)
    return AuthorizedModel(manifest, "0" * 64, manifest.sha256, 1,
                           threading.Event(), {"expires_at": 2**62})


class FixtureTrustPolicy:
    mode = "test_fixture"
    status = "ok"
    def __init__(self):
        self.tickets = []
        self.revoked = set()
        self.failure = None
    def authorize(self, raw, model_root):
        if isinstance(raw, (bytes, str)) or (type(raw) is dict and "claims" in raw):
            raise TrustError("test_fixture_manifest_only")
        ticket = fixture_ticket(validate_manifest(raw, model_root))
        self.tickets.append(ticket)
        return ticket
    def check(self, ticket):
        if self.failure:
            ticket.deny_event.set()
            raise TrustError(self.failure)
        if ticket.deny_event.is_set() or ticket.manifest.sha256 in self.revoked:
            ticket.deny_event.set()
            raise TrustError("model_revoked")
    def tick(self):
        if self.failure:
            raise TrustError(self.failure)
    def install_policy(self, digest):
        self.revoked.add(digest)


class TicketedModels(dict):
    """Explicitly bind legacy scheduler fixtures inserted without load()."""
    def __init__(self, service):
        self.service = service
    def __setitem__(self, name, worker):
        self.service._worker_tickets[worker] = fixture_ticket(name=name)
        super().__setitem__(name, worker)
    def update(self, *args, **kwargs):
        for key, value in dict(*args, **kwargs).items():
            self[key] = value


def make_real_signed_service_fixture(parent):
    """Make a signed real ONNX service fixture using the native verifier."""
    import subprocess
    from xtos_ai_runtime.trust import NativeEd25519Verifier
    from ai_trust_fixtures import make_fixture, model_claims, signed_envelope
    root = Path(parent) / "signed-service"
    fx = make_fixture(root)
    fx.model_bytes = (ROOT / "tools/xtos-onnx-baseline/fixtures/fixed_classifier.onnx").read_bytes()
    (fx.model_root / "model.onnx").write_bytes(fx.model_bytes)
    native = ROOT / "tools/xtos-native"
    vendor = native / "vendor/monocypher"
    helper = root / "modelverify"
    subprocess.run(["/usr/bin/gcc", "-O2", "-Wall", "-Wextra", "-Werror",
                    "-I", str(native / "common"), "-I", str(vendor),
                    str(native / "modelverify/modelverify.c"),
                    str(vendor / "monocypher.c"), str(vendor / "monocypher-ed25519.c"),
                    "-o", str(helper)], check=True, capture_output=True, timeout=30)
    fx.policy.verifier = NativeEd25519Verifier(helper, spool_parent=root / "verify-spool")
    claims = model_claims(fx.now, "model.onnx", fx.model_bytes)
    key_id = hashlib.sha256(fx.publisher.public_raw).hexdigest()
    envelope = signed_envelope("xtos-ai-model-v1", key_id, claims, fx.publisher,
                               b"XTOS-AI-MODEL-v1\x00")
    return fx, envelope


def bind_fixture_models(service):
    service._models = TicketedModels(service)
    return service
