"""Scoped approved-marker and fail-closed adapter integration fixtures."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
EVIDENCE = REPO / ".build/p1-action-executor-evidence"
sys.path.insert(0, str(REPO / "tools"))


class AdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import xtos_action_executor as adapter
        import xtos_action_gate as gate
        cls.adapter, cls.gate = adapter, gate
        if os.geteuid() != 0:
            raise unittest.SkipTest("native broker fixture requires root")
        EVIDENCE.mkdir(mode=0o700, parents=True, exist_ok=True)
        cls.base = EVIDENCE / ("native-action-" + uuid.uuid4().hex)
        cls.base.mkdir(mode=0o700)
        cls.root = cls.base / "root"
        for name in ("bin", "work", "out", "tmp"):
            (cls.root / name).mkdir(mode=0o755, parents=True, exist_ok=True)
        (cls.root / "tmp").chmod(0o777)
        os.chown(cls.root / "out", 62000, 62000)
        (cls.root / "out").chmod(0o700)
        cls.runner = cls.base / "runner"
        source = REPO / "tools/xtos-native/runner/runner.c"
        cls.runner_source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
        (cls.base / "runner-source.json").write_text(json.dumps({
            "source": str(source.relative_to(REPO)),
            "sha256": cls.runner_source_sha256}, indent=2) + "\n")
        subprocess.run(["gcc", "-static", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-DXTOS_RUNNER_UID=62000", "-DXTOS_RUNNER_GID=62000",
                        str(source), "-o", str(cls.runner)], check=True, capture_output=True)
        marker = cls.base / "marker.c"
        marker.write_text('''#include <unistd.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
int main(int argc,char **argv) {
 char cwd[128];
 if(argc!=3 || getuid()!=62000 || getgid()!=62000 || getgroups(0,0)!=0) return 31;
 if(getenv("XTOS_POISON") || getpid() <= 0) return 32;
 if(!getcwd(cwd,sizeof(cwd)) || strcmp(cwd,"/work")) return 35;
 if(socket(AF_INET,SOCK_STREAM,0)>=0) return 36;
 int fd=open("/tmp/escaped",O_WRONLY|O_CREAT|O_EXCL,0600);
 if(fd>=0) { close(fd); return 37; }
 fd=open(argv[1],O_WRONLY|O_CREAT|O_EXCL,0600);
 if(fd<0) return 33;
 if(write(fd,argv[2],1)!=1 || close(fd)) return 34;
 puts("NATIVE_MARKER_EXECUTED"); return 0;
}
''')
        subprocess.run(["gcc", "-static", "-O2", str(marker), "-o", str(cls.root / "bin/marker")],
                       check=True, capture_output=True)
        cls.runner.chmod(0o755)
        cls.root.chmod(0o755)

    def setUp(self):
        self.store = self.gate.FixtureStore(self.base / ("authority-" + uuid.uuid4().hex))
        self.now = 1000
        self.service = self.gate.ActionGate(self.store, clock=lambda: self.now)
        self.marker_path = "/out/marker-" + uuid.uuid4().hex
        self.marker = self.root / self.marker_path.lstrip("/")
        self.req = {
            "schema": "xtos.action.v1", "argv": ["/bin/marker", self.marker_path, "x"],
            "root": str(self.root), "cwd": str(self.root / "work"),
            "uid": 62000, "gid": 62000,
            "limits": {"cpu_sec": 2, "as_bytes": 67108864, "nofile": 32, "nproc": 4},
            "timeout_ms": 1000, "writable_dirs": ["/out"],
            "sandbox_policy": {"version": "xtos.runner.v1", "clear_groups": True,
                               "deny_socket": True, "deny_getpid": False}}

    def run_approved(self, req=None):
        req = self.req if req is None else req
        nonce = self.service.approve(req, actor="root-fixture-admin", ttl_seconds=60)
        with patch.object(self.adapter, "RUNNER", str(self.runner)):
            return self.service.execute(nonce, req, self.adapter.NativeRunnerExecutor())

    def test_production_identity_and_runner_are_fixed(self):
        self.assertEqual(self.adapter.RUNNER, "/usr/libexec/xtos/runner")
        self.assertEqual((self.adapter.RUNNER_UID, self.adapter.RUNNER_GID), (62000, 62000))

    def test_approved_request_reaches_real_static_runner_and_marker(self):
        with patch.dict(os.environ, {"XTOS_POISON": "must-not-reach-child"}):
            result = self.run_approved()
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(result["stdout"], "NATIVE_MARKER_EXECUTED\n")
        self.assertEqual(result["limits"], {"cpu_seconds": 2, "address_space_bytes": 67108864,
                                              "nofile": 32, "nproc": 4})
        self.assertFalse((self.root / "tmp/escaped").exists())
        self.assertEqual(self.marker.read_text(), "x")
        report = EVIDENCE / ("native-positive-" + uuid.uuid4().hex + ".json")
        report.write_text(json.dumps(result, indent=2) + "\n")

    def test_identity_resources_and_write_grants_reject_without_exec(self):
        cases = [("uid", 65534), ("gid", 65534), ("uid", True),
                 ("limits", {**self.req["limits"], "nofile": True}),
                 ("writable_dirs", ["/"]), ("writable_dirs", ["/bin"])]
        for key, value in cases:
            bad = copy.deepcopy(self.req)
            bad[key] = value
            with self.subTest(key=key, value=value):
                with self.assertRaises((self.gate.GateError, ValueError)):
                    self.run_approved(bad)
                self.assertFalse(self.marker.exists())

    def test_changed_expired_missing_and_replayed_approvals_do_not_reexecute(self):
        import xtos_action_gate as gate
        changed_token = self.service.approve(self.req, actor="fixture-admin", ttl_seconds=60)
        changed = copy.deepcopy(self.req)
        changed["argv"][-1] = "y"
        with self.assertRaises(gate.GateError):
            self.service.execute(changed_token, changed, self.adapter.NativeRunnerExecutor())
        self.assertFalse(self.marker.exists())

        expired_token = self.service.approve(self.req, actor="fixture-admin", ttl_seconds=60)
        self.now = 1060
        with self.assertRaises(gate.GateError):
            self.service.execute(expired_token, self.req, self.adapter.NativeRunnerExecutor())
        self.assertFalse(self.marker.exists())

        self.now = 1000
        for missing in ("0" * 64, "../escape"):
            with self.assertRaises(gate.GateError):
                self.service.execute(missing, self.req, self.adapter.NativeRunnerExecutor())
            self.assertFalse(self.marker.exists())

        replay_token = self.service.approve(self.req, actor="fixture-admin", ttl_seconds=60)
        with patch.object(self.adapter, "RUNNER", str(self.runner)):
            self.service.execute(replay_token, self.req, self.adapter.NativeRunnerExecutor())
        self.assertEqual(self.marker.read_text(), "x")
        with self.assertRaises(gate.GateError):
            self.service.execute(replay_token, self.req, self.adapter.NativeRunnerExecutor())
        self.assertEqual(self.marker.read_text(), "x")


    def test_direct_mutable_request_is_rejected_without_exec(self):
        with patch.object(self.adapter.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                self.adapter.NativeRunnerExecutor()(self.req)
            run.assert_not_called()
        self.assertFalse(self.marker.exists())

    def test_symlink_root_cwd_and_executable_reject_without_exec(self):
        root_link = self.base / ("root-link-" + uuid.uuid4().hex)
        root_link.symlink_to(self.root, target_is_directory=True)
        cwd_link = self.root / ("work-link-" + uuid.uuid4().hex)
        cwd_link.symlink_to(self.root / "work", target_is_directory=True)
        exe_link = self.root / "bin" / ("marker-link-" + uuid.uuid4().hex)
        exe_link.symlink_to("marker")
        cases = [("root", str(root_link)), ("cwd", str(cwd_link)),
                 ("argv", ["/bin/" + exe_link.name, self.marker_path, "x"])]
        for field, value in cases:
            request = copy.deepcopy(self.req)
            request[field] = value
            with self.subTest(field=field), patch.object(self.adapter.subprocess, "run") as run:
                # O_NOFOLLOW ELOOP/ENOTDIR is a successful refusal, not a test error.
                with self.assertRaises((self.gate.GateError, ValueError, OSError)):
                    self.run_approved(request)
                run.assert_not_called()
                self.assertFalse(self.marker.exists())

    def test_nonroot_and_unsafe_paths_reject_without_exec(self):
        with patch.object(self.adapter.os, "geteuid", return_value=65534), \
                patch.object(self.adapter.subprocess, "run") as run:
            with self.assertRaises(PermissionError):
                self.adapter.NativeRunnerExecutor()(self.req)
            run.assert_not_called()
        unsafe = self.root / ("unsafe-" + uuid.uuid4().hex)
        unsafe.mkdir(mode=0o777)
        unsafe.chmod(0o777)
        for field, value in (("cwd", str(unsafe)), ("root", str(unsafe)),
                             ("argv", ["/bin/../bin/marker", self.marker_path, "x"])):
            request = copy.deepcopy(self.req)
            request[field] = value
            with self.subTest(field=field), patch.object(self.adapter.subprocess, "run") as run:
                with self.assertRaises((self.gate.GateError, ValueError, OSError)):
                    self.run_approved(request)
                run.assert_not_called()
                self.assertFalse(self.marker.exists())

    def test_runner_inode_remains_pinned_after_path_replacement(self):
        import shutil
        pinned = self.base / ("pinned-runner-" + uuid.uuid4().hex)
        replacement = self.base / ("replacement-" + uuid.uuid4().hex)
        shutil.copyfile(self.runner, pinned)
        shutil.copyfile("/usr/bin/false", replacement)
        pinned.chmod(0o755)
        replacement.chmod(0o755)
        identity = pinned.stat()
        real_run = subprocess.run

        def replace_before_exec(argv, **kwargs):
            fd = kwargs["pass_fds"][0]
            info = os.fstat(fd)
            self.assertEqual((info.st_dev, info.st_ino), (identity.st_dev, identity.st_ino))
            os.replace(replacement, pinned)
            self.assertNotEqual(pinned.stat().st_ino, info.st_ino)
            self.assertEqual(kwargs["executable"], f"/proc/self/fd/{fd}")
            return real_run(argv, **kwargs)

        nonce = self.service.approve(self.req, actor="root-fixture-admin", ttl_seconds=60)
        with patch.object(self.adapter, "RUNNER", str(pinned)), \
                patch.object(self.adapter.subprocess, "run", side_effect=replace_before_exec):
            result = self.service.execute(nonce, self.req, self.adapter.NativeRunnerExecutor())
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(self.marker.read_text(), "x")


if __name__ == "__main__":
    unittest.main()
