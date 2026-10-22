"""No-runner resource/response regressions; no approval, truncation or deletion."""
import errno
import json
import os
from pathlib import Path
import sys
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
import xtos_action_executor as adapter


class ResourceTests(unittest.TestCase):
    def test_path_close_then_error_does_not_close_reused_fd_or_leak_next(self):
        real_open, real_close = os.open, os.close
        opened, closed, owned = [], [], set()
        replacement = None
        replacement_live = False
        failure = OSError(errno.EIO, "injected after actual close and reuse")

        def open_spy(path, flags, **kwargs):
            fd = real_open(path, flags, **kwargs)
            opened.append(fd)
            owned.add(fd)
            return fd

        def close_then_error(fd):
            nonlocal replacement, replacement_live
            closed.append(fd)
            owned.discard(fd)
            real_close(fd)
            if replacement is None:
                replacement = real_open("/dev/null", os.O_RDONLY | os.O_CLOEXEC)
                replacement_live = True
                self.assertEqual(replacement, fd, "FD reuse prerequisite")
                raise failure
            if fd == replacement:
                replacement_live = False

        fake_os = SimpleNamespace(open=open_spy, close=close_then_error, fstat=os.fstat)
        try:
            with patch.object(adapter, "os", fake_os):
                with self.assertRaises(OSError) as caught:
                    adapter._open_path("/usr", directory=True)
            self.assertIs(caught.exception, failure)
            self.assertEqual(closed, opened, "each owned handle must close once")
            self.assertEqual(owned, set(), "next directory descriptor leaked")
            self.assertTrue(replacement_live, "unrelated reused FD closed")
            os.fstat(replacement)
        finally:
            # Only handles still owned by this probe, never a released old number.
            for fd in owned:
                real_close(fd)
            if replacement_live:
                real_close(replacement)


    def test_executor_cleanup_closes_all_handles_once_preserving_primary_error(self):
        real_open_path, real_close = adapter._open_path, os.close
        opened, closed, owned = [], [], set()
        replacement = None
        primary = RuntimeError("fixture refuses to execute any runner")

        def open_path(*args, **kwargs):
            fd = real_open_path(*args, **kwargs)
            opened.append(fd)
            owned.add(fd)
            return fd

        def close_then_error(fd):
            nonlocal replacement
            real_close(fd)
            if len(opened) == 4:
                closed.append(fd)
                owned.discard(fd)
                if replacement is None:
                    temporary = os.open("/dev/null", os.O_RDONLY | os.O_CLOEXEC)
                    replacement = os.dup2(temporary, fd) if temporary != fd else temporary
                    if temporary != fd:
                        real_close(temporary)
                    self.assertEqual(replacement, fd)
                    raise OSError(errno.EIO, "injected cleanup close failure")

        request = MappingProxyType({
            "schema": "xtos.action.v1", "root": "/usr", "cwd": "/usr",
            "argv": ("/bin/true",), "writable_dirs": (), "uid": 62000, "gid": 62000,
            "limits": MappingProxyType({"cpu_sec": 1, "as_bytes": 67108864,
                                        "nofile": 32, "nproc": 4}),
            "timeout_ms": 1000,
            "sandbox_policy": MappingProxyType({"clear_groups": True,
                                                "deny_socket": True, "deny_getpid": False})})
        fake_os = SimpleNamespace(open=os.open, close=close_then_error,
                                  fstat=os.fstat, geteuid=lambda: 0)
        try:
            with patch.object(adapter, "os", fake_os), \
                    patch.object(adapter, "_open_path", side_effect=open_path), \
                    patch.object(adapter, "RUNNER", "/usr/bin/true"), \
                    patch.object(adapter.subprocess, "run", side_effect=primary) as run:
                with self.assertRaises(BaseException) as caught:
                    adapter.NativeRunnerExecutor()(request)
            run.assert_called_once()
            self.assertIs(caught.exception, primary)
            self.assertEqual(closed, [opened[2], opened[3], opened[1], opened[0]])
            self.assertEqual(owned, set())
            os.fstat(replacement)
        finally:
            for fd in owned:
                real_close(fd)
            if replacement is not None:
                real_close(replacement)


    def test_path_cleanup_preserves_first_close_error_when_cleanup_also_errors(self):
        real_close = os.close
        calls = []
        first = OSError(errno.EIO, "first close failure")

        def close_error(fd):
            calls.append(fd)
            real_close(fd)
            raise first if len(calls) == 1 else OSError(errno.EINTR, "cleanup failure")

        with patch.object(adapter, "os", SimpleNamespace(
                open=os.open, close=close_error, fstat=os.fstat)):
            with self.assertRaises(OSError) as caught:
                adapter._open_path("/usr", directory=True)
        self.assertIs(caught.exception, first)
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(*calls)


    def test_path_rejects_noncanonical_segments_before_open(self):
        for path in ("/usr/.", "/usr/../usr", "/usr//bin", "/usr/", "//usr"):
            with self.subTest(path=path), patch.object(
                    adapter.os, "open", side_effect=AssertionError("noncanonical reached open")) as opened:
                with self.assertRaises(ValueError):
                    adapter._open_path(path, directory=True)
                opened.assert_not_called()


class SourceDependencyTests(unittest.TestCase):
    def test_integration_runner_uses_maintained_source_and_records_hash(self):
        # Read only: never import the integration module or invoke setUpClass.
        source = (REPO / "tests/test_action_executor.py").read_text()
        self.assertNotIn(".build/p1-p14b-evidence/runner.c", source)
        self.assertIn('"tools/xtos-native/runner/runner.c"', source)
        self.assertIn("hashlib.sha256(source.read_bytes()).hexdigest()", source)
        self.assertTrue((REPO / "tools/xtos-native/runner/runner.c").is_file())


class ResponseTests(unittest.TestCase):
    def test_response_contract_matches_process_returncode(self):
        self.assertTrue(callable(getattr(adapter, "_parse_runner_result", None)),
                        "pure runner-result validation boundary missing")
        base = {"status": "exit", "exit_code": 0, "signal": None, "timed_out": False}
        valid = [base, {**base, "exit_code": 7},
                 {**base, "status": "signal", "signal": 9, "exit_code": 137},
                 {**base, "status": "timeout", "timed_out": True, "exit_code": 124},
                 {**base, "status": "setup_error", "exit_code": 125},
                 {**base, "status": "exec_error", "exit_code": 127},
                 {**base, "status": "wait_error", "exit_code": 125}]
        for response in valid:
            self.assertEqual(adapter._parse_runner_result(
                json.dumps(response), response["exit_code"]), response)
        invalid = ["", "{", "[]", '{"status":"exit"}',
                   json.dumps({**base, "exit_code": True}),
                   json.dumps({**base, "status": "unknown"}),
                   json.dumps({**base, "signal": 9}),
                   json.dumps({**base, "timed_out": True}),
                   json.dumps({**base, "status": "timeout"}),
                   json.dumps({**base, "status": "signal", "signal": 0}),
                   json.dumps({**base, "status": "setup_error"})]
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(RuntimeError):
                adapter._parse_runner_result(text, 0)
        for code in (1, -9, True):
            with self.subTest(returncode=code), self.assertRaises(RuntimeError):
                adapter._parse_runner_result(json.dumps(base), code)


if __name__ == "__main__":
    unittest.main()
