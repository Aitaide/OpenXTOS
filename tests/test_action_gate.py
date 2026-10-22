"""Security regressions use an explicit private fixture and marker executors only."""
import copy
import errno
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
try:
    import xtos_action_gate as gate
except ImportError:
    gate = None


def request():
    return {
        "schema": "xtos.action.v1", "argv": ["/bin/echo", "fixture"],
        "root": "/srv/fixture", "cwd": "/srv/fixture/work", "uid": 65534,
        "gid": 65534, "limits": {"cpu_sec": 2, "as_bytes": 67108864,
                                  "nofile": 32, "nproc": 4},
        "timeout_ms": 1000, "writable_dirs": ["/work"],
        "sandbox_policy": {"version": "xtos.runner.v1", "clear_groups": True,
                           "deny_socket": True, "deny_getpid": False},
    }


class GateTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(gate, "independent approval gate is not implemented")
        self.tmp = tempfile.TemporaryDirectory(prefix="action-gate-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "store"
        self.store = gate.FixtureStore(self.root)
        self.clock = lambda: 1000
        self.service = gate.ActionGate(self.store, clock=self.clock)

    def test_approved_exact_request_executes_once(self):
        calls = []
        req = request()
        token = self.service.approve(req, actor="fixture-admin", ttl_seconds=60)
        result = self.service.execute(token, req, lambda frozen: calls.append(frozen) or "marker")
        self.assertEqual(result, "marker")
        self.assertEqual(len(calls), 1)
        with self.assertRaises(gate.GateError):
            self.service.execute(token, req, calls.append)
        self.assertEqual(len(calls), 1)


    def test_fixed_schema_rejects_hostile_requests(self):
        cases = []
        for key, value in (("uid", True), ("gid", 0), ("timeout_ms", 1.0),
                           ("timeout_ms", float("nan")), ("root", "/srv/../etc"),
                           ("cwd", "/outside"), ("argv", ["echo"]),
                           ("writable_dirs", ["/../etc"]), ("schema", "other")):
            bad = request()
            bad[key] = value
            cases.append(bad)
        bad = request()
        bad["action_allowed"] = True
        cases.append(bad)
        for bad in cases:
            with self.subTest(bad=bad):
                with self.assertRaises(gate.GateError):
                    self.service.approve(bad, actor="admin", ttl_seconds=60)
        raw = json.dumps(request())
        for bad in (raw[:-1] + ', "uid": 65534}', raw.replace('65534', 'true', 1),
                    raw.replace('1000', 'NaN'), '{', '[]'):
            with self.subTest(raw=bad):
                with self.assertRaises(gate.GateError):
                    self.service.approve(bad, actor="admin", ttl_seconds=60)

    def test_every_execution_field_is_bound(self):
        for key in request():
            if key == "schema":
                continue
            original = request()
            changed = copy.deepcopy(original)
            replacements = {
                "argv": ["/bin/echo", "changed"], "root": "/srv", "cwd": "/srv/fixture",
                "uid": 65533, "gid": 65533, "timeout_ms": 1001,
                "writable_dirs": [], "limits": {**original["limits"], "cpu_sec": 3},
                "sandbox_policy": {**original["sandbox_policy"], "deny_socket": False},
            }
            changed[key] = replacements[key]
            token = self.service.approve(original, actor="admin", ttl_seconds=60)
            called = []
            with self.subTest(key=key):
                with self.assertRaises(gate.GateError):
                    self.service.execute(token, changed, called.append)
                self.assertFalse(called)

    def test_snapshot_is_recursively_immutable(self):
        original = request()
        token = self.service.approve(original, actor="admin", ttl_seconds=60)
        def marker(frozen):
            original["argv"][1] = "changed"
            self.assertEqual(frozen["argv"][1], "fixture")
            with self.assertRaises(TypeError):
                frozen["argv"][1] = "changed"
            with self.assertRaises(TypeError):
                frozen["limits"]["cpu_sec"] = 99
        self.service.execute(token, original, marker)


    def test_store_security_and_record_integrity(self):
        for mode in (0o755, 0o770):
            self.root.chmod(mode)
            with self.assertRaises(gate.GateError):
                self.service.approve(request(), actor="admin", ttl_seconds=60)
        self.root.chmod(0o700)
        for attack in ("chmod", "symlink", "hardlink", "tamper", "duplicate", "owner"):
            token = self.service.approve(request(), actor="admin", ttl_seconds=60)
            record = self.root / (token + ".pending")
            if attack == "chmod":
                record.chmod(0o644)
            elif attack == "symlink":
                external = Path(self.tmp.name) / "external"
                record.rename(external)
                record.symlink_to(external)
            elif attack == "hardlink":
                os.link(record, Path(self.tmp.name) / "alias")
            elif attack == "tamper":
                value = json.loads(record.read_bytes())
                value["body"]["actor"] = "other-admin"
                record.write_text(json.dumps(value))
            elif attack == "duplicate":
                data = record.read_text()
                record.write_text(data[:-1] + ', "body": {}}')
            elif os.geteuid() == 0:
                os.chown(record, 65534, 65534)
            else:
                continue
            called = []
            with self.subTest(attack=attack):
                with self.assertRaises(gate.GateError):
                    self.service.execute(token, request(), called.append)
                self.assertFalse(called)

    def test_nonce_paths_missing_approval_and_failed_attempts(self):
        called = []
        for nonce in ("../escape", "/etc/passwd", "a" * 63, "a" * 64, "A" * 64):
            with self.assertRaises(gate.GateError):
                self.service.execute(nonce, request(), called.append)
        for bad in ("{", {**request(), "uid": True},
                    {**request(), "argv": ["/bin/false"]}):
            token = self.service.approve(request(), actor="admin", ttl_seconds=60)
            with self.assertRaises(gate.GateError):
                self.service.execute(token, bad, called.append)
            with self.assertRaises(gate.GateError):
                self.service.execute(token, request(), called.append)
        token = self.service.approve(request(), actor="admin", ttl_seconds=60)
        def broken(_):
            raise RuntimeError("fixture executor failed")
        with self.assertRaises(RuntimeError):
            self.service.execute(token, request(), broken)
        with self.assertRaises(gate.GateError):
            self.service.execute(token, request(), called.append)
        self.assertFalse(called)

    def test_root_only_production_and_explicit_fixture(self):
        from unittest.mock import patch
        with patch("os.geteuid", return_value=1234):
            with self.assertRaises(gate.GateError):
                gate.TrustedStore()
        self.assertEqual(gate.TRUSTED_ROOT, Path("/var/lib/xtos/action-gate"))
        for actor, ttl in (("", 60), ("yes\n", 60), ("admin", True),
                           ("admin", 0), ("admin", 3601)):
            with self.assertRaises(gate.GateError):
                self.service.approve(request(), actor=actor, ttl_seconds=ttl)

    def test_expiry_and_clock_rollback(self):
        for now in (1060, 999):
            token = self.service.approve(request(), actor="admin", ttl_seconds=60)
            with self.subTest(now=now):
                service = gate.ActionGate(self.store, clock=lambda: now)
                with self.assertRaises(gate.GateError):
                    service.execute(token, request(), lambda _: self.fail("executed"))

    def test_concurrent_nonce_runs_one_marker(self):
        import multiprocessing
        context = multiprocessing.get_context("fork")
        token = self.service.approve(request(), actor="admin", ttl_seconds=60)
        start = context.Event()
        output = context.Queue()
        def worker():
            start.wait(3)
            try:
                self.service.execute(token, request(), lambda _: output.put("executed"))
            except gate.GateError:
                output.put("denied")
        children = [context.Process(target=worker) for _ in range(8)]
        for child in children:
            child.start()
        start.set()
        for child in children:
            child.join(5)
            self.assertFalse(child.is_alive())
            self.assertEqual(child.exitcode, 0)
        outcomes = [output.get(timeout=1) for _ in children]
        self.assertEqual(outcomes.count("executed"), 1)


    def test_crash_after_consume_never_replays(self):
        import multiprocessing
        token = self.service.approve(request(), actor="admin", ttl_seconds=60)
        child = multiprocessing.get_context("fork").Process(target=lambda:
            self.service.execute(token, request(), lambda _: os._exit(73)))
        child.start()
        child.join(5)
        self.assertEqual(child.exitcode, 73)
        with self.assertRaises(gate.GateError):
            gate.ActionGate(self.store, clock=self.clock).execute(
                token, request(), lambda _: self.fail("crash replay"))

    def test_fsync_failure_prevents_callback(self):
        from unittest.mock import patch
        token = self.service.approve(request(), actor="admin", ttl_seconds=60)
        with patch("os.fsync", side_effect=OSError("fixture fsync failed")):
            with self.assertRaises(gate.GateError):
                self.service.execute(token, request(), lambda _: self.fail("executed"))
        with self.assertRaises(gate.GateError):
            self.service.execute(token, request(), lambda _: self.fail("replayed"))

    def test_symlink_directory_replacement_is_rejected(self):
        token = self.service.approve(request(), actor="admin", ttl_seconds=60)
        saved = self.root.with_name("saved")
        self.root.rename(saved)
        self.root.symlink_to(saved, target_is_directory=True)
        with self.assertRaises(gate.GateError):
            self.service.execute(token, request(), lambda _: self.fail("executed"))
        self.root.unlink()
        self.root.mkdir(mode=0o700)
        with self.assertRaises(gate.GateError):
            self.service.execute(token, request(), lambda _: self.fail("executed"))
        self.assertTrue((saved / (token + ".pending")).exists())

    def test_production_cli_refuses_store_override(self):
        import subprocess
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "tools"),
               "XTOS_ACTION_GATE_ROOT": str(self.root)}
        result = subprocess.run([sys.executable, "-m", "xtos_action_gate", "--help"],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("approve", result.stdout)
        result = subprocess.run([sys.executable, "-m", "xtos_action_gate", "--store",
                                 str(self.root), "init"], capture_output=True, env=env)
        self.assertNotEqual(result.returncode, 0)


    def test_non_ascii_mac_is_denied(self):
        token = self.service.approve(request(), actor="admin", ttl_seconds=60)
        path = self.root / (token + ".pending")
        record = json.loads(path.read_bytes())
        record["mac"] = "非法"
        path.write_text(json.dumps(record))
        with self.assertRaises(gate.GateError):
            self.service.execute(token, request(), lambda _: self.fail("executed"))

    def test_deadline_is_rechecked_after_snapshot(self):
        token = self.service.approve(request(), actor="admin", ttl_seconds=60)
        ticks = iter((1000, 1060))
        service = gate.ActionGate(self.store, clock=lambda: next(ticks))
        with self.assertRaises(gate.GateError):
            service.execute(token, request(), lambda _: self.fail("expired callback"))

    def test_root_management_cli_uses_fixed_store_and_audits_actor(self):
        if os.geteuid() != 0:
            self.skipTest("root fixture management CLI")
        from unittest.mock import patch
        from xtos_action_gate.__main__ import main
        import xtos_action_gate.store as storage
        fixed = Path(self.tmp.name) / "management" / "gate"
        raw = Path(self.tmp.name) / "request.json"
        raw.write_text(json.dumps(request()))
        with patch.object(storage, "TRUSTED_ROOT", fixed):
            self.assertEqual(main(["init"]), 0)
            self.assertEqual(main(["approve", "--request", str(raw), "--actor", "root-reviewer",
                                   "--ttl-seconds", "60"]), 0)
            pending = list(fixed.glob("*.pending"))
            self.assertEqual(len(pending), 1)
            record = json.loads(pending[0].read_bytes())
            self.assertEqual(record["body"]["actor"], "root-reviewer")
            self.assertEqual(record["body"]["issuer_uid"], 0)


    def test_management_initialization_never_writes_through_symlink(self):
        if os.geteuid() != 0:
            self.skipTest("root fixture management initialization")
        from unittest.mock import patch
        import xtos_action_gate.store as storage
        external = Path(self.tmp.name) / "outside"
        external.mkdir()
        alias = Path(self.tmp.name) / "alias"
        alias.symlink_to(external, target_is_directory=True)
        with patch.object(storage, "TRUSTED_ROOT", alias / "new-parent" / "gate"):
            with self.assertRaises((gate.GateError, OSError)):
                gate.TrustedStore(initialize=True)
        self.assertEqual(list(external.iterdir()), [])


class _CloseFault:
    """Track gate-owned descriptors and inject close-after-release EIO once."""
    def __init__(self, trigger):
        self.real_open = os.open
        self.real_close = os.close
        self.trigger = trigger
        self.opened = []
        self.active = {}
        self.closed = {}
        self.injected = False
        self.replacement = None
        self.replacement_close_count = 0

    def open(self, path, flags, *args, **kwargs):
        fd = self.real_open(path, flags, *args, **kwargs)
        owner = (len(self.opened), fd, os.fspath(path))
        self.opened.append(owner)
        self.active[fd] = owner
        return fd

    def close(self, fd):
        owner = self.active.pop(fd, None)
        if owner is not None:
            self.closed[owner[0]] = self.closed.get(owner[0], 0) + 1
        elif fd == self.replacement:
            self.replacement_close_count += 1
        self.real_close(fd)
        if owner is not None and not self.injected and self.trigger(owner[2]):
            self.injected = True
            self.replacement = self.real_open("/dev/null", os.O_RDONLY | os.O_CLOEXEC)
            if self.replacement != fd:
                self.real_close(self.replacement)
                raise AssertionError("descriptor number was not deterministically reused")
            raise OSError(errno.EIO, "injected EIO after real close and descriptor reuse")

    def assert_all_owned_closed_once(self, case):
        case.assertEqual(self.active, {}, "gate-owned descriptor leaked")
        case.assertEqual(self.closed, {owner[0]: 1 for owner in self.opened},
                         "gate-owned descriptor was not closed exactly once")

    def close_replacement(self):
        if self.replacement is not None:
            try:
                self.real_close(self.replacement)
            except OSError:
                pass
            self.replacement = None


def _assert_fd_fault_recovery(test, tracker, service):
    """The injected failure must deny callbacks without poisoning future use."""
    test.assertTrue(tracker.injected, "close fault was not reached")
    test.assertEqual(tracker.replacement_close_count, 0,
                     "cleanup closed an unrelated descriptor reusing the old number")
    os.fstat(tracker.replacement)
    tracker.assert_all_owned_closed_once(test)
    tracker.close_replacement()

    called = []
    token = service.approve(request(), actor="recovery", ttl_seconds=60)
    result = service.execute(token, request(), lambda frozen: called.append(frozen) or "recovered")
    test.assertEqual(result, "recovered")
    test.assertEqual(len(called), 1)


class ActionGateCloseLifecycleTests(unittest.TestCase):
    def setUp(self):
        GateTests.setUp(self)

    def test_directory_close_error_does_not_reclose_reused_fd_or_leak_next(self):
        from unittest.mock import patch

        token = self.service.approve(request(), actor="before-fault", ttl_seconds=60)
        tracker = _CloseFault(lambda path: path == "/")
        called = []
        try:
            with patch("os.open", side_effect=tracker.open), patch("os.close", side_effect=tracker.close):
                with self.assertRaises((gate.GateError, OSError)):
                    self.service.execute(token, request(), lambda frozen: called.append(frozen))
            self.assertFalse(called, "storage failure must not invoke the executor")
            _assert_fd_fault_recovery(self, tracker, self.service)
        finally:
            tracker.close_replacement()
            for fd in list(tracker.active):
                tracker.real_close(fd)

    def test_locked_lock_close_error_still_closes_directory(self):
        from unittest.mock import patch

        token = self.service.approve(request(), actor="before-fault", ttl_seconds=60)
        tracker = _CloseFault(lambda path: path == ".lock")
        called = []
        try:
            with patch("os.open", side_effect=tracker.open), patch("os.close", side_effect=tracker.close):
                with self.assertRaises((gate.GateError, OSError)):
                    self.service.execute(token, request(), lambda frozen: called.append(frozen))
            self.assertFalse(called, "lock cleanup failure must not invoke the executor")
            _assert_fd_fault_recovery(self, tracker, self.service)
        finally:
            tracker.close_replacement()
            for fd in list(tracker.active):
                tracker.real_close(fd)


if __name__ == "__main__":
    unittest.main(verbosity=2)
