"""Independent human approval contract. No runner or inference integration."""
import hashlib
import hmac
import math
import os
import secrets
import time
from .schema import GateError, canonical, integer, keys, loads, snapshot, text
from .store import FixtureStore, TrustedStore, TRUSTED_ROOT


class ActionGate:
    """Consume-before-callback, at most once; never return a reusable allow flag."""
    def __init__(self, store, *, clock=time.time):
        self.store = store
        self.clock = clock

    def _now(self):
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            raise GateError("invalid trusted clock")
        return now

    def approve(self, request, *, actor, ttl_seconds):
        # Actor is audit text ONLY. OS credentials/store access confer authority.
        text(actor, 128)
        integer(ttl_seconds, 1, 3600)
        encoded, _ = snapshot(request)
        now = self._now()
        nonce = secrets.token_hex(32)
        body = {"schema": "xtos.approval.v1", "nonce": nonce, "actor": actor,
                "issuer_uid": os.geteuid(), "digest": hashlib.sha256(encoded).hexdigest(),
                "issued": now, "expires": now + ttl_seconds}
        with self.store.locked() as (directory, key):
            mac = hmac.new(key, canonical(body), hashlib.sha256).hexdigest()
            self.store.publish(directory, nonce, canonical({"body": body, "mac": mac}))
        return nonce

    def execute(self, nonce, request, executor):
        # Freeze only AFTER durable consumption: even malformed attempts burn nonce.
        with self.store.locked() as (directory, key):
            record = loads(self.store.consume(directory, nonce))
            keys(record, "body mac")
            body = record["body"]
            keys(body, "schema nonce actor issuer_uid digest issued expires")
            if (type(record["mac"]) is not str or len(record["mac"]) != 64
                    or not record["mac"].isascii() or not hmac.compare_digest(
                        record["mac"], hmac.new(key, canonical(body), hashlib.sha256).hexdigest())):
                raise GateError("approval integrity failure")
            if body["schema"] != "xtos.approval.v1" or body["nonce"] != nonce:
                raise GateError("approval identity mismatch")
            text(body["actor"], 128)
            integer(body["issuer_uid"], 0, 2**32 - 2)
            issued, expires = body["issued"], body["expires"]
            if (type(issued) not in (int, float) or type(expires) not in (int, float)
                    or not math.isfinite(issued) or not math.isfinite(expires)
                    or not 0 < expires - issued <= 3600):
                raise GateError("invalid approval deadline")
            now = self._now()
            if not issued <= now < expires:
                raise GateError("expired or clock rollback")
            encoded, frozen = snapshot(request)
            if body["digest"] != hashlib.sha256(encoded).hexdigest():
                raise GateError("request changed")
        # Only immutable validated data crosses the executor boundary.
        if not issued <= self._now() < expires:
            raise GateError("approval expired before executor")
        return executor(frozen)
