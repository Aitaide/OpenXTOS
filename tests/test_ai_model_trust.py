"""Strict model/policy signature tests; test-only keys and stores live in ai_trust_fixtures."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "tools/xtos-ai-runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from xtos_ai_runtime.trust import (  # noqa: E402
    FilePolicyStore,
    NativeEd25519Verifier,
    PolicyCheckpoint,
    ProductionTrustPolicy,
    TrustError,
)
from ai_trust_fixtures import (  # noqa: E402
    FakeTime,
    MemoryAnchor,
    MemoryStore,
    make_fixture,
    model_claims,
    policy_claims,
    signed_envelope,
)

MODEL_DOMAIN = b"XTOS-AI-MODEL-v1\x00"
POLICY_DOMAIN = b"XTOS-AI-POLICY-v1\x00"


class TrustPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xtos-ai-trust-test-")
        self.addCleanup(self.temp.cleanup)
        self.fx = make_fixture(Path(self.temp.name) / "fixture")

    def model_envelope(self, model_path="model.onnx", **claim_changes):
        claims = model_claims(self.fx.now, model_path, self.fx.model_bytes, **claim_changes)
        key_id = hashlib.sha256(self.fx.publisher.public_raw).hexdigest()
        return signed_envelope("xtos-ai-model-v1", key_id, claims, self.fx.publisher, MODEL_DOMAIN)

    def policy_bundle(self, sequence: int, **claim_changes):
        claims = policy_claims(self.fx.now, self.fx.publisher, sequence=sequence, **claim_changes)
        root_id = hashlib.sha256(self.fx.root_signer.public_raw).hexdigest()
        return signed_envelope("xtos-ai-policy-v1", root_id, claims, self.fx.root_signer,
                               POLICY_DOMAIN)

    def assert_reason(self, reason, operation):
        with self.assertRaises(TrustError) as caught:
            operation()
        self.assertEqual(caught.exception.reason, reason)

    def test_real_ed25519_signatures_authorize_model_and_bind_metadata(self):
        authorized = self.fx.policy.authorize(self.model_envelope(), self.fx.model_root)
        self.assertEqual(authorized.manifest.sha256, hashlib.sha256(self.fx.model_bytes).hexdigest())
        self.assertEqual(authorized.policy_sequence, 1)
        self.fx.policy.check(authorized)

        tampered = json.loads(self.model_envelope())
        tampered["claims"]["provider"] = "CUDAExecutionProvider"
        self.assert_reason("signature_invalid", lambda: self.fx.policy.authorize(tampered, self.fx.model_root))

    def test_unsigned_legacy_manifest_is_signature_required(self):
        self.assert_reason("signature_required", lambda: self.fx.policy.authorize(
            {"name": "unsigned", "sha256": "0" * 64}, self.fx.model_root))

    def test_authorized_claims_are_deeply_immutable(self):
        authorized = self.fx.policy.authorize(self.model_envelope(), self.fx.model_root)
        with self.assertRaises(TypeError):
            authorized.claims["input"]["name"] = "tampered"

    def test_model_expiry_tick_invalidates_idle_authorization(self):
        authorized = self.fx.policy.authorize(self.model_envelope(), self.fx.model_root)
        self.fx.clock.now = authorized.claims["expires_at"] - 1
        self.fx.policy.tick()
        self.fx.clock.now = authorized.claims["expires_at"]
        self.assert_reason("model_signature_expired", lambda: self.fx.policy.check(authorized))
        self.assertTrue(authorized.deny_event.is_set())

    def test_publisher_key_expiry_invalidates_existing_authorization(self):
        authorized = self.fx.policy.authorize(self.model_envelope(), self.fx.model_root)
        key_expiry = json.loads(self.fx.policy_bundle)["claims"]["publisher_keys"][0]["expires_at"]
        self.fx.clock.now = key_expiry
        self.fx.policy.tick()
        self.assertTrue(authorized.deny_event.is_set())

    def test_policy_key_window_update_invalidates_authorization_before_not_before(self):
        authorized = self.fx.policy.authorize(self.model_envelope(), self.fx.model_root)
        claims = policy_claims(self.fx.now, self.fx.publisher, sequence=2)
        claims["publisher_keys"][0]["not_before"] = self.fx.now + 60
        root_id = hashlib.sha256(self.fx.root_signer.public_raw).hexdigest()
        bundle = signed_envelope("xtos-ai-policy-v1", root_id, claims, self.fx.root_signer,
                                 POLICY_DOMAIN)
        self.fx.policy.install_policy(bundle)
        self.assert_reason("signing_key_not_yet_valid", lambda: self.fx.policy.check(authorized))
        self.assertTrue(authorized.deny_event.is_set())

    def test_signed_but_unsupported_metadata_is_rejected(self):
        self.assert_reason("model_format_unsupported", lambda: self.fx.policy.authorize(
            self.model_envelope(format="gguf"), self.fx.model_root))
        self.assert_reason("architecture_mismatch", lambda: self.fx.policy.authorize(
            self.model_envelope(architecture="aarch64"), self.fx.model_root))
        self.assert_reason("runtime_version_unsupported", lambda: self.fx.policy.authorize(
            self.model_envelope(min_runtime_version="1.10.0"), self.fx.model_root))
        self.assert_reason("license_not_allowed", lambda: self.fx.policy.authorize(
            self.model_envelope(license="GPL-3.0-only"), self.fx.model_root))

    def test_policy_cannot_override_p1_architecture_allowlist(self):
        policy = ProductionTrustPolicy(
            self.fx.root_signer.public_raw, verifier=self.fx.verifier,
            time_source=self.fx.clock, store=self.fx.store, anchor=self.fx.anchor,
            expected_arch="aarch64", minimum_policy_sequence=1)
        envelope = self.model_envelope(architecture="aarch64")
        self.assert_reason("architecture_mismatch", lambda: policy.authorize(
            envelope, self.fx.model_root))

    def test_signed_path_cannot_follow_symlink_inside_model_root(self):
        link = self.fx.model_root / "alias.onnx"
        link.symlink_to("model.onnx")
        envelope = self.model_envelope(model_path="alias.onnx")
        self.assert_reason("malformed_envelope", lambda: self.fx.policy.authorize(
            envelope, self.fx.model_root))

    def test_model_and_publisher_expiry_boundaries_fail_closed(self):
        envelope = self.model_envelope(not_before=self.fx.now + 1)
        self.fx.clock.now = self.fx.now
        self.assert_reason("model_not_yet_valid", lambda: self.fx.policy.authorize(envelope, self.fx.model_root))
        self.fx.clock.now = json.loads(envelope)["claims"]["expires_at"]
        self.assert_reason("model_signature_expired", lambda: self.fx.policy.authorize(envelope, self.fx.model_root))

    def test_untrusted_or_expired_policy_time_is_denied(self):
        self.fx.clock.trusted = False
        self.assert_reason("policy_time_untrusted", lambda: self.fx.policy.authorize(
            self.model_envelope(), self.fx.model_root))
        self.fx.clock.trusted = True
        self.fx.clock.now = 1_800_003_600
        self.assert_reason("policy_expired", lambda: self.fx.policy.tick())

    def test_clock_failure_tick_permanently_denies_existing_authorization(self):
        authorized = self.fx.policy.authorize(self.model_envelope(), self.fx.model_root)
        self.fx.clock.trusted = False
        self.assert_reason("policy_time_untrusted", self.fx.policy.tick)
        self.assertTrue(authorized.deny_event.is_set())
        self.assert_reason("policy_time_untrusted", lambda: self.fx.policy.check(authorized))

    def test_authorize_current_failure_permanently_denies_existing_ticket(self):
        envelope = self.model_envelope()
        authorized = self.fx.policy.authorize(envelope, self.fx.model_root)
        self.fx.clock.trusted = False
        self.assert_reason("policy_time_untrusted", lambda: self.fx.policy.authorize(
            envelope, self.fx.model_root))
        self.assertTrue(authorized.deny_event.is_set())
        self.fx.clock.trusted = True
        self.assert_reason("policy_time_untrusted", lambda: self.fx.policy.check(authorized))

    def test_invalid_new_model_envelope_preserves_healthy_ticket(self):
        authorized = self.fx.policy.authorize(self.model_envelope(), self.fx.model_root)
        invalid = json.loads(self.model_envelope())
        invalid["claims"]["version"] = "2.0.0"
        for raw, reason in ((b"{}", "signature_required"), (invalid, "signature_invalid")):
            with self.subTest(reason=reason):
                self.assert_reason(reason, lambda: self.fx.policy.authorize(raw, self.fx.model_root))
                self.assertFalse(authorized.deny_event.is_set())
                self.fx.policy.check(authorized)

    def test_authorize_retires_ticket_if_final_recheck_fails(self):
        envelope = self.model_envelope()
        clock = self.fx.clock
        class FailOnSecondRead:
            def __init__(self):
                self.reads = 0
            def read(self):
                self.reads += 1
                if self.reads == 2:
                    clock.trusted = False
                return clock.read()
        self.fx.policy.time_source = FailOnSecondRead()
        self.assert_reason("policy_time_untrusted", lambda: self.fx.policy.authorize(
            envelope, self.fx.model_root))
        self.assertEqual(self.fx.policy._authorized, {})

    def test_boottime_prevents_wall_clock_rollback_from_extending_model(self):
        authorized = self.fx.policy.authorize(
            self.model_envelope(expires_at=self.fx.now + 15), self.fx.model_root)
        self.fx.clock.now = self.fx.now + 10
        self.fx.clock.boottime = 110.0
        self.fx.policy.tick()
        self.fx.clock.now = self.fx.now + 3
        self.fx.clock.boottime = 120.0
        self.assert_reason("model_signature_expired", lambda: self.fx.policy.check(authorized))

    def test_boottime_keeps_later_trusted_utc_highwater(self):
        self.fx.policy.authorize(
            self.model_envelope(expires_at=self.fx.now + 100), self.fx.model_root)
        self.fx.clock.now = self.fx.now + 90
        self.fx.clock.boottime = 110.0
        first = self.fx.policy._reading().utc_seconds
        self.fx.clock.now = self.fx.now + 1
        self.fx.clock.boottime = 111.0
        second = self.fx.policy._reading().utc_seconds
        self.assertGreaterEqual(second, first)

    def test_forward_trusted_time_reanchors_boottime_deadline(self):
        authorized = self.fx.policy.authorize(
            self.model_envelope(expires_at=self.fx.now + 100), self.fx.model_root)
        self.fx.clock.now = self.fx.now + 99
        self.fx.clock.boottime = 110.0
        self.fx.policy.tick()
        self.fx.clock.now = self.fx.now + 1
        self.fx.clock.boottime = 112.0
        self.assert_reason("model_signature_expired", lambda: self.fx.policy.check(authorized))
        self.assertTrue(authorized.deny_event.is_set())

    def test_expiring_policy_marks_existing_authorization_denied(self):
        authorized = self.fx.policy.authorize(self.model_envelope(), self.fx.model_root)
        self.fx.clock.now = json.loads(self.fx.policy_bundle.decode())["claims"]["expires_at"]
        self.assert_reason("policy_expired", self.fx.policy.tick)
        self.assertTrue(authorized.deny_event.is_set())

    def test_higher_fresh_policy_can_replace_expired_checkpoint_without_reset(self):
        self.fx.clock.now += 3600
        bundle = self.policy_bundle(2, expires_at=self.fx.now + 7200)
        installed = self.fx.policy.install_policy(bundle)
        self.assertEqual(installed.sequence, 2)

    def test_duplicate_json_keys_and_malformed_base64_are_rejected(self):
        valid = self.model_envelope()
        duplicate = b'{"schema":"xtos-ai-model-v1","schema":"xtos-ai-model-v1"}'
        self.assert_reason("malformed_envelope", lambda: self.fx.policy.authorize(duplicate, self.fx.model_root))
        doc = json.loads(valid)
        doc["signature"] = base64.b64encode(b"x" * 63).decode("ascii")
        self.assert_reason("signature_invalid", lambda: self.fx.policy.authorize(doc, self.fx.model_root))
        self.assert_reason("malformed_envelope", lambda: self.fx.policy.authorize(
            "\ud800", self.fx.model_root))

    def test_json_rejects_nan_boolean_null_and_oversized_envelopes(self):
        for raw in (
            b'{"schema":"xtos-ai-model-v1","key_id":"' + b"0" * 64 +
            b'","claims":{"x":NaN},"signature":""}',
            b'{"schema":"xtos-ai-model-v1","key_id":"' + b"0" * 64 +
            b'","claims":{"x":true},"signature":""}',
            b'{"schema":"xtos-ai-model-v1","key_id":"' + b"0" * 64 +
            b'","claims":{"x":null},"signature":""}',
            b" " * (64 * 1024 + 1),
        ):
            self.assert_reason("malformed_envelope", lambda raw=raw: self.fx.policy.authorize(
                raw, self.fx.model_root))

    def test_policy_rollback_equivocation_idempotence_and_revocation_union(self):
        seq2 = self.policy_bundle(2, revoked_models=[hashlib.sha256(self.fx.model_bytes).hexdigest()])
        installed = self.fx.policy.install_policy(seq2)
        self.assertEqual(installed.sequence, 2)
        commits = self.fx.store.commit_count
        self.fx.policy.install_policy(seq2)
        self.assertEqual(self.fx.store.commit_count, commits, "same bytes must not rewrite TTL/checkpoint")
        self.assert_reason("policy_rollback", lambda: self.fx.policy.install_policy(self.policy_bundle(1)))
        self.assert_reason("policy_equivocation", lambda: self.fx.policy.install_policy(
            self.policy_bundle(2, licenses=["MIT", "BSD-3-Clause"])))
        self.assert_reason("model_revoked", lambda: self.fx.policy.authorize(
            self.model_envelope(), self.fx.model_root))

        seq3 = self.policy_bundle(3, revoked_models=[])
        final = self.fx.policy.install_policy(seq3)
        self.assertIn(hashlib.sha256(self.fx.model_bytes).hexdigest(), final.revoked_model_sha256)
        self.assert_reason("model_revoked", lambda: self.fx.policy.authorize(
            self.model_envelope(), self.fx.model_root))

    def test_provisioned_minimum_policy_sequence_rejects_older_checkpoint(self):
        policy = ProductionTrustPolicy(
            self.fx.root_signer.public_raw, verifier=self.fx.verifier,
            time_source=self.fx.clock, store=self.fx.store, anchor=self.fx.anchor,
            minimum_policy_sequence=2)
        self.assert_reason("policy_rollback", lambda: policy.authorize(
            self.model_envelope(), self.fx.model_root))

    def test_provisioned_minimum_allows_installing_policy_at_floor(self):
        policy = ProductionTrustPolicy(
            self.fx.root_signer.public_raw, verifier=self.fx.verifier,
            time_source=self.fx.clock, store=self.fx.store, anchor=self.fx.anchor,
            minimum_policy_sequence=2)
        self.assertEqual(policy.install_policy(self.policy_bundle(2)).sequence, 2)

    def test_policy_install_accepts_json_text_without_reencoding_error(self):
        bundle = self.policy_bundle(2)
        installed = self.fx.policy.install_policy(bundle.decode("ascii"))
        self.assertEqual(installed.sequence, 2)

    def test_deleted_publisher_key_and_revoked_key_are_not_authorized(self):
        key_id = hashlib.sha256(self.fx.publisher.public_raw).hexdigest()
        # Re-sign a policy with no publisher keys: omission is revocation for authorization.
        claims = policy_claims(self.fx.now, self.fx.publisher, sequence=2)
        claims["publisher_keys"] = []
        root_id = hashlib.sha256(self.fx.root_signer.public_raw).hexdigest()
        bundle = signed_envelope("xtos-ai-policy-v1", root_id, claims, self.fx.root_signer,
                                 POLICY_DOMAIN)
        self.fx.policy.install_policy(bundle)
        self.assert_reason("signing_key_revoked", lambda: self.fx.policy.authorize(
            self.model_envelope(), self.fx.model_root))
        self.assertNotEqual(key_id, "")

    def test_deleting_publisher_key_denies_existing_authorization(self):
        authorized = self.fx.policy.authorize(self.model_envelope(), self.fx.model_root)
        claims = policy_claims(self.fx.now, self.fx.publisher, sequence=2)
        claims["publisher_keys"] = []
        root_id = hashlib.sha256(self.fx.root_signer.public_raw).hexdigest()
        bundle = signed_envelope("xtos-ai-policy-v1", root_id, claims, self.fx.root_signer,
                                 POLICY_DOMAIN)
        self.fx.policy.install_policy(bundle)
        self.assertTrue(authorized.deny_event.is_set())
        self.assert_reason("signing_key_revoked", lambda: self.fx.policy.check(authorized))

    def test_deleted_publisher_key_cannot_be_reintroduced_later(self):
        root_id = hashlib.sha256(self.fx.root_signer.public_raw).hexdigest()
        deleted = policy_claims(self.fx.now, self.fx.publisher, sequence=2)
        deleted["publisher_keys"] = []
        self.fx.policy.install_policy(signed_envelope(
            "xtos-ai-policy-v1", root_id, deleted, self.fx.root_signer, POLICY_DOMAIN))
        reintroduced = policy_claims(self.fx.now, self.fx.publisher, sequence=3)
        self.fx.policy.install_policy(signed_envelope(
            "xtos-ai-policy-v1", root_id, reintroduced, self.fx.root_signer, POLICY_DOMAIN))
        self.assert_reason("signing_key_revoked", lambda: self.fx.policy.authorize(
            self.model_envelope(), self.fx.model_root))

    def test_anchor_must_match_whole_committed_checkpoint(self):
        self.fx.anchor.sequence = 0
        self.assert_reason("policy_state_unavailable", lambda: self.fx.policy.authorize(
            self.model_envelope(), self.fx.model_root))

    def test_stale_external_anchor_cannot_authorize_newer_checkpoint(self):
        self.fx.policy.install_policy(self.policy_bundle(2))
        self.fx.anchor.sequence = 1
        self.fx.anchor.payload_sha256 = self.fx.policy_payload_sha256
        self.assert_reason("policy_state_unavailable", lambda: self.fx.policy.authorize(
            self.model_envelope(), self.fx.model_root))

    def test_checkpoint_sequence_rejects_boolean_with_valid_anchor(self):
        current = self.fx.store.read()
        self.fx.store.checkpoint = PolicyCheckpoint(
            current.policy_bundle, True, current.policy_payload_sha256,
            current.revoked_key_ids, current.revoked_model_sha256, current.last_trusted_utc)
        self.assert_reason("policy_state_unavailable", lambda: self.fx.policy.authorize(
            self.model_envelope(), self.fx.model_root))

    def test_missing_trusted_time_checkpoint_or_anchor_never_bootstraps(self):
        root_key = self.fx.root_signer.public_raw
        verifier = self.fx.verifier
        missing_time = ProductionTrustPolicy(root_key, verifier=verifier)
        self.assert_reason("policy_time_untrusted", lambda: missing_time.authorize(
            self.model_envelope(), self.fx.model_root))
        self.assert_reason("policy_time_untrusted", lambda: missing_time.install_policy(self.fx.policy_bundle))

        no_state = ProductionTrustPolicy(root_key, verifier=verifier, time_source=self.fx.clock)
        self.assert_reason("policy_state_unavailable", lambda: no_state.authorize(
            self.model_envelope(), self.fx.model_root))
        empty = MemoryStore(None)
        no_checkpoint = ProductionTrustPolicy(root_key, verifier=verifier, time_source=self.fx.clock,
                                              store=empty, anchor=self.fx.anchor,
                                              )
        self.assert_reason("policy_state_unavailable", lambda: no_checkpoint.install_policy(self.fx.policy_bundle))
        no_anchor = ProductionTrustPolicy(root_key, verifier=verifier, time_source=self.fx.clock,
                                          store=self.fx.store)
        self.assert_reason("policy_state_unavailable", lambda: no_anchor.tick())

    def test_missing_trust_root_minimum_sequence_is_fail_closed(self):
        policy = ProductionTrustPolicy(
            self.fx.root_signer.public_raw, verifier=self.fx.verifier,
            time_source=self.fx.clock, store=self.fx.store, anchor=self.fx.anchor)
        self.assert_reason("policy_state_unavailable", lambda: policy.authorize(
            self.model_envelope(), self.fx.model_root))

    def _precheck_native_verifier(self):
        helper = Path(self.temp.name) / "precheck-modelverify"
        native = ROOT / "tools/xtos-native"
        vendor = native / "vendor/monocypher"
        subprocess.run(["/usr/bin/gcc", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-I", str(native / "common"), "-I", str(vendor),
                        str(native / "modelverify/modelverify.c"), str(vendor / "monocypher.c"),
                        str(vendor / "monocypher-ed25519.c"), "-o", str(helper)],
                       check=True, capture_output=True, timeout=30)
        return NativeEd25519Verifier(helper, spool_parent=Path(self.temp.name) / "precheck-spool")

    def test_install_precheck_failure_permanently_denies_existing_tickets(self):
        from dataclasses import replace

        verifier = self._precheck_native_verifier()
        for fault, reason in (("anchor-digest", "policy_state_unavailable"),
                              ("untrusted-time", "policy_time_untrusted"),
                              ("corrupt-checkpoint", "policy_state_unavailable"),
                              ("checkpoint-highwater", "policy_time_untrusted")):
            with self.subTest(fault=fault):
                local = make_fixture(Path(self.temp.name) / fault)
                local.policy.verifier = verifier
                checkpoint = local.store.read()
                envelope = signed_envelope(
                    "xtos-ai-model-v1", hashlib.sha256(local.publisher.public_raw).hexdigest(),
                    model_claims(local.now, "model.onnx", local.model_bytes), local.publisher,
                    MODEL_DOMAIN)
                tickets = [local.policy.authorize(envelope, local.model_root) for _ in range(2)]
                bundle = signed_envelope(
                    "xtos-ai-policy-v1", hashlib.sha256(local.root_signer.public_raw).hexdigest(),
                    policy_claims(local.now, local.publisher, sequence=2), local.root_signer,
                    POLICY_DOMAIN)
                if fault == "anchor-digest":
                    local.anchor.payload_sha256 = "0" * 64
                elif fault == "untrusted-time":
                    local.clock.trusted = False
                elif fault == "corrupt-checkpoint":
                    local.store.checkpoint = replace(
                        checkpoint, policy_bundle=checkpoint.policy_bundle[:-1] + b"X")
                else:
                    local.store.checkpoint = replace(checkpoint, last_trusted_utc=local.now + 1)
                failed_checkpoint = local.store.read()
                failed_anchor = local.anchor.read()
                self.assert_reason(reason, lambda: local.policy.install_policy(bundle))
                for ticket in tickets:
                    self.assertTrue(ticket.deny_event.is_set(), "install must deny before any check")
                self.assertEqual(local.store.commit_count, 0)
                self.assertEqual(local.store.read(), failed_checkpoint)
                self.assertEqual(local.anchor.read(), failed_anchor)

                # Repair the fault before check: denial must not depend on rediscovering it.
                local.clock.trusted = True
                local.store.checkpoint = checkpoint
                local.anchor.sequence = checkpoint.highest_sequence
                local.anchor.payload_sha256 = checkpoint.policy_payload_sha256
                for ticket in tickets:
                    self.assert_reason(reason, lambda ticket=ticket: local.policy.check(ticket))
                self.assertEqual(local.policy.install_policy(bundle).sequence, 2)
                for ticket in tickets:
                    self.assertTrue(ticket.deny_event.is_set())
                    self.assert_reason(reason, lambda ticket=ticket: local.policy.check(ticket))
                local.policy.check(local.policy.authorize(envelope, local.model_root))

    def test_bad_incoming_policy_preserves_healthy_existing_ticket(self):
        self.fx.policy.verifier = self._precheck_native_verifier()
        self.fx.policy.install_policy(self.policy_bundle(2))
        ticket = self.fx.policy.authorize(self.model_envelope(), self.fx.model_root)
        checkpoint = self.fx.store.read()
        anchor = self.fx.anchor.read()
        commits = self.fx.store.commit_count
        invalid_signature = json.loads(self.policy_bundle(3))
        invalid_signature["signature"] = base64.b64encode(b"\x00" * 64).decode("ascii")
        future_claims = policy_claims(self.fx.now, self.fx.publisher, sequence=3)
        future_claims["issued_at"] = self.fx.now + 1
        future = signed_envelope(
            "xtos-ai-policy-v1", hashlib.sha256(self.fx.root_signer.public_raw).hexdigest(),
            future_claims, self.fx.root_signer, POLICY_DOMAIN)
        for reason, bundle in (("malformed_envelope", b"{"),
                               ("signature_invalid", invalid_signature),
                               ("policy_rollback", self.policy_bundle(1)),
                               ("policy_time_untrusted", future),
                               ("policy_expired", self.policy_bundle(3, expires_at=self.fx.now)),
                               ("policy_equivocation", self.policy_bundle(2, licenses=["MIT", "BSD-3-Clause"]))):
            with self.subTest(reason=reason):
                self.assert_reason(reason, lambda: self.fx.policy.install_policy(bundle))
                self.assertFalse(ticket.deny_event.is_set())
                self.fx.policy.check(ticket)
                self.assertFalse(ticket.deny_event.is_set())
                self.assertEqual(self.fx.store.read(), checkpoint)
                self.assertEqual(self.fx.anchor.read(), anchor)
                self.assertEqual(self.fx.store.commit_count, commits)

    def test_policy_install_rejects_time_below_checkpoint_highwater(self):
        self.fx.clock.now = self.fx.now - 1
        policy = ProductionTrustPolicy(
            self.fx.root_signer.public_raw, verifier=self.fx.verifier,
            time_source=self.fx.clock, store=self.fx.store, anchor=self.fx.anchor,
            minimum_policy_sequence=1)
        self.assert_reason("policy_time_untrusted", lambda: policy.install_policy(
            self.policy_bundle(2)))

    def test_higher_policy_cannot_silently_replace_invalid_current_checkpoint(self):
        current = self.fx.store.read()
        self.fx.store.checkpoint = PolicyCheckpoint(
            current.policy_bundle[:-1] + b"X", current.highest_sequence,
            current.policy_payload_sha256, current.revoked_key_ids,
            current.revoked_model_sha256, current.last_trusted_utc)
        self.assert_reason("policy_state_unavailable", lambda: self.fx.policy.install_policy(
            self.policy_bundle(2)))

    def test_native_verifier_has_no_fallback_when_fixed_helper_is_missing(self):
        helper = Path("/usr/libexec/xtos/modelverify")
        if helper.exists():
            self.skipTest("installed native helper is present; its integration is tested by its owner")
        verifier = NativeEd25519Verifier()
        self.assert_reason("signature_verifier_unavailable", lambda: verifier.verify(
            self.fx.root_signer.public_raw, b"message", b"x" * 64))

    def test_native_verifier_cleanup_failure_cannot_authorize(self):
        from unittest.mock import patch
        helper = Path(self.temp.name) / "silent-success"
        helper.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
        helper.chmod(0o700)
        spool = Path(self.temp.name) / "spool"
        verifier = NativeEd25519Verifier(helper, spool_parent=spool)
        with patch("xtos_ai_runtime.trust.shutil.rmtree", side_effect=OSError("cleanup fault")):
            self.assert_reason("signature_verifier_unavailable", lambda: verifier.verify(
                self.fx.publisher.public_raw, b"message", b"x" * 64))

    def test_native_verifier_rejects_symlink_helper_and_spool_parent(self):
        helper = Path(self.temp.name) / "silent-success"
        helper.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
        helper.chmod(0o700)
        helper_link = Path(self.temp.name) / "helper-link"
        helper_link.symlink_to(helper)
        self.assert_reason("signature_verifier_unavailable", lambda: NativeEd25519Verifier(
            helper_link).verify(self.fx.publisher.public_raw, b"message", b"x" * 64))
        parent_link = Path(self.temp.name) / "parent-link"
        parent_link.symlink_to(Path(self.temp.name), target_is_directory=True)
        self.assert_reason("signature_verifier_unavailable", lambda: NativeEd25519Verifier(
            parent_link / helper.name).verify(
                self.fx.publisher.public_raw, b"message", b"x" * 64))

        spool_target = Path(self.temp.name) / "spool-target"
        spool_target.mkdir(mode=0o700)
        spool_link = Path(self.temp.name) / "spool-link"
        spool_link.symlink_to(spool_target, target_is_directory=True)
        self.assert_reason("signature_verifier_unavailable", lambda: NativeEd25519Verifier(
            helper, spool_parent=spool_link).verify(
                self.fx.publisher.public_raw, b"message", b"x" * 64))
        self.assert_reason("signature_verifier_unavailable", lambda: NativeEd25519Verifier(
            helper, spool_parent=Path("/")).verify(
                self.fx.publisher.public_raw, b"message", b"x" * 64))

    def test_native_verifier_default_spool_parent_ignores_environment(self):
        helper = Path(self.temp.name) / "silent-success"
        helper.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
        helper.chmod(0o700)
        unsafe_tmp = Path(self.temp.name) / "environment-tmp"
        unsafe_tmp.mkdir(mode=0o700)
        with patch.dict(os.environ, {"TMPDIR": str(unsafe_tmp)}):
            with patch("xtos_ai_runtime.trust.tempfile.mkdtemp",
                       wraps=tempfile.mkdtemp) as make_temp:
                NativeEd25519Verifier(helper).verify(
                    self.fx.publisher.public_raw, b"message", b"x" * 64)
        self.assertEqual(make_temp.call_args.kwargs["dir"], "/tmp")
        self.assertEqual(list(unsafe_tmp.iterdir()), [])

    def test_native_verifier_spool_is_owner_only_and_removed_after_real_verification(self):
        helper = Path(self.temp.name) / "modelverify"
        native = ROOT / "tools/xtos-native"
        vendor = native / "vendor/monocypher"
        subprocess.run(["/usr/bin/gcc", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-I", str(native / "common"), "-I", str(vendor),
                        str(native / "modelverify/modelverify.c"), str(vendor / "monocypher.c"),
                        str(vendor / "monocypher-ed25519.c"), "-o", str(helper)],
                       check=True, capture_output=True, timeout=30)
        spool = Path(self.temp.name) / "spool"
        verifier = NativeEd25519Verifier(helper, spool_parent=spool)
        message = MODEL_DOMAIN + b'{"test":"native-real"}'
        signature = self.fx.publisher.sign(message)
        verifier.verify(self.fx.publisher.public_raw, message, signature)
        self.assertEqual(spool.stat().st_mode & 0o777, 0o700)
        self.assertEqual(list(spool.iterdir()), [])
        self.assert_reason("signature_invalid", lambda: verifier.verify(
            self.fx.publisher.public_raw, message + b"!", signature))
        self.assertEqual(list(spool.iterdir()), [])

    def test_file_checkpoint_adapter_persists_whole_checkpoint_atomically(self):
        state_dir = Path(self.temp.name) / "secure-state"
        state_dir.mkdir(mode=0o700)
        adapter = FilePolicyStore(state_dir / "checkpoint.json")
        self.assertIsNone(adapter.read())
        checkpoint = self.fx.store.read()
        adapter.commit(checkpoint)
        loaded = adapter.read()
        self.assertEqual(loaded, checkpoint)
        self.assertEqual((state_dir / "checkpoint.json").stat().st_mode & 0o777, 0o600)

    def test_file_checkpoint_adapter_roundtrips_anchor_identity(self):
        state_dir = Path(self.temp.name) / "secure-state-identity"
        state_dir.mkdir(mode=0o700)
        adapter = FilePolicyStore(state_dir / "checkpoint.json")
        checkpoint = self.fx.store.read()
        checkpoint = PolicyCheckpoint(
            checkpoint.policy_bundle, checkpoint.highest_sequence,
            checkpoint.policy_payload_sha256, checkpoint.revoked_key_ids,
            checkpoint.revoked_model_sha256, checkpoint.last_trusted_utc,
            "deployment-anchor-a")
        adapter.commit(checkpoint)
        self.assertEqual(adapter.read(), checkpoint)

    def test_file_checkpoint_roundtrips_large_real_signed_bundle(self):
        state_dir = Path(self.temp.name) / "secure-large-state"
        state_dir.mkdir(mode=0o700)
        adapter = FilePolicyStore(state_dir / "checkpoint.json")
        adapter.commit(self.fx.store.read())
        self.fx.policy.store = adapter
        revoked = sorted(hashlib.sha256(str(index).encode()).hexdigest() for index in range(80))
        bundle = self.policy_bundle(2, revoked_models=revoked)
        self.assertGreater(len(bundle), 4096)
        self.fx.policy.install_policy(bundle)
        loaded = adapter.read()
        self.assertEqual(loaded.policy_bundle, bundle)
        self.assertEqual(loaded.highest_sequence, 2)
        self.assertEqual(loaded.revoked_model_sha256, frozenset(revoked))

    def test_file_checkpoint_adapter_rejects_sequence_downgrade(self):
        state_dir = Path(self.temp.name) / "secure-state-rollback"
        state_dir.mkdir(mode=0o700)
        adapter = FilePolicyStore(state_dir / "checkpoint.json")
        first = self.fx.store.read()
        adapter.commit(first)
        newer_bundle = self.policy_bundle(2)
        policy_doc = json.loads(newer_bundle)
        body = {"schema": policy_doc["schema"], "key_id": policy_doc["key_id"],
                "claims": policy_doc["claims"]}
        from ai_trust_fixtures import _canonical
        newer = PolicyCheckpoint(newer_bundle, 2,
                                 hashlib.sha256(_canonical(body)).hexdigest(),
                                 first.revoked_key_ids, first.revoked_model_sha256, self.fx.now)
        # The adapter enforces sequence monotonicity even if a caller races policy code.
        adapter.commit(newer)
        with self.assertRaises(TrustError) as caught:
            adapter.commit(first)
        self.assertEqual(caught.exception.reason, "policy_rollback")

    def test_file_checkpoint_adapter_rejects_malformed_checkpoint_types(self):
        state_dir = Path(self.temp.name) / "secure-state-malformed"
        state_dir.mkdir(mode=0o700)
        adapter = FilePolicyStore(state_dir / "checkpoint.json")
        first = self.fx.store.read()
        malformed = PolicyCheckpoint(
            first.policy_bundle, True, first.policy_payload_sha256,
            first.revoked_key_ids, first.revoked_model_sha256, first.last_trusted_utc)
        self.assert_reason("policy_state_unavailable", lambda: adapter.commit(malformed))

    def test_file_checkpoint_adapter_rejects_trusted_time_highwater_regression(self):
        state_dir = Path(self.temp.name) / "secure-state-time"
        state_dir.mkdir(mode=0o700)
        adapter = FilePolicyStore(state_dir / "checkpoint.json")
        first = self.fx.store.read()
        adapter.commit(first)
        regressed = PolicyCheckpoint(
            first.policy_bundle, first.highest_sequence, first.policy_payload_sha256,
            first.revoked_key_ids, first.revoked_model_sha256, first.last_trusted_utc - 1)
        self.assert_reason("policy_time_untrusted", lambda: adapter.commit(regressed))

    def test_checkpoint_write_faults_deny_existing_authorizations(self):
        from dataclasses import replace
        import xtos_ai_runtime.trust as trust

        for fault in ("file-fsync", "rename", "dir-fsync"):
            with self.subTest(fault=fault):
                local = make_fixture(Path(self.temp.name) / ("fault-" + fault))
                state_dir = local.root / "state"
                state_dir.mkdir(mode=0o700)
                adapter = FilePolicyStore(state_dir / "checkpoint.json")
                adapter.commit(local.store.read())
                local.policy.store = adapter
                envelope = signed_envelope(
                    "xtos-ai-model-v1", hashlib.sha256(local.publisher.public_raw).hexdigest(),
                    model_claims(local.now, "model.onnx", local.model_bytes), local.publisher,
                    MODEL_DOMAIN)
                authorized = local.policy.authorize(envelope, local.model_root)
                bundle = signed_envelope(
                    "xtos-ai-policy-v1", hashlib.sha256(local.root_signer.public_raw).hexdigest(),
                    policy_claims(local.now, local.publisher, sequence=2), local.root_signer,
                    POLICY_DOMAIN)
                real_fsync = trust.os.fsync
                count = 0

                def fail_sync(fd):
                    nonlocal count
                    count += 1
                    if count == (2 if fault == "dir-fsync" else 1):
                        raise OSError("injected fsync fault")
                    return real_fsync(fd)

                context = (patch.object(trust.os, "replace", side_effect=OSError("rename fault"))
                           if fault == "rename" else patch.object(trust.os, "fsync", side_effect=fail_sync))
                with context:
                    self.assert_reason("policy_state_unavailable", lambda: local.policy.install_policy(bundle))
                self.assertTrue(authorized.deny_event.is_set())
                self.assert_reason("policy_state_unavailable", lambda: local.policy.check(authorized))
                self.assertEqual(adapter.read().highest_sequence, 2 if fault == "dir-fsync" else 1)

    def test_verifier_false_result_cannot_authorize(self):
        class FalseVerifier:
            def verify(self, public_key, message, signature):
                return False

        policy = ProductionTrustPolicy(
            self.fx.root_signer.public_raw, verifier=FalseVerifier(),
            time_source=self.fx.clock, store=self.fx.store, anchor=self.fx.anchor,
            minimum_policy_sequence=1)
        self.assert_reason("signature_verifier_unavailable", lambda: policy.authorize(
            self.model_envelope(), self.fx.model_root))

    def test_missing_native_verifier_is_reported_not_misclassified_as_state_error(self):
        policy = ProductionTrustPolicy(
            self.fx.root_signer.public_raw, time_source=self.fx.clock, store=self.fx.store,
            anchor=self.fx.anchor, minimum_policy_sequence=1)
        self.assert_reason("signature_verifier_unavailable", lambda: policy.authorize(
            self.model_envelope(), self.fx.model_root))


if __name__ == "__main__":
    unittest.main()
