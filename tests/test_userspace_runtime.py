#!/usr/bin/env python3
"""Runtime preparation tests only touch temporary target directories."""
import importlib.util
import os
from pathlib import Path
import stat
import tempfile
import unittest
import json

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('xtos_runtime',ROOT/'scripts/verify_userspace_runtime.py')
assert spec is not None and spec.loader is not None
runtime=importlib.util.module_from_spec(spec);spec.loader.exec_module(runtime)

def fixture_manifest():
    package={'name':'fixture','sha256':'0'*64}
    return json.dumps({'schema_version':1,'architecture':'x86_64','lock_sha256':'0'*64,
      'lock':{'schema_version':1,'architecture':'x86_64','package_format':'deb','packages':[package]},
      'packages':[{'package':package,'files':[]}]})+'\n'

class RuntimePreparationTests(unittest.TestCase):
    def test_devnull_is_created_only_in_owned_root_and_then_removed(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            root=Path(tmp);(root/'dev').mkdir()
            (root/'usr/share/xtos').mkdir(parents=True)
            (root/'usr/share/xtos/userspace-manifest.json').write_text(fixture_manifest())
            created=runtime.prepare_runtime_devnull(root)
            self.assertTrue(created)
            info=(root/'dev/null').stat()
            self.assertTrue(stat.S_ISCHR(info.st_mode))
            self.assertEqual((os.major(info.st_rdev),os.minor(info.st_rdev)),(1,3))
            runtime.cleanup_runtime_devnull(root,created)
            self.assertFalse((root/'dev/null').exists())

    def test_symlink_devnull_cannot_escape_root(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            root=Path(tmp);(root/'dev').mkdir()
            (root/'usr/share/xtos').mkdir(parents=True)
            (root/'usr/share/xtos/userspace-manifest.json').write_text(fixture_manifest())
            (root/'dev/null').symlink_to('/dev/null')
            with self.assertRaises(ValueError):runtime.prepare_runtime_devnull(root)
            self.assertTrue((root/'dev/null').is_symlink())

    def test_bounded_command_timeout_stops_the_process(self):
        import sys
        import subprocess
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            runtime.run_bounded([sys.executable, '-c', 'import os,time; print(os.getpid(), flush=True); time.sleep(20)'], env=os.environ.copy(), timeout=0.2)
        pid=int(caught.exception.output.decode().strip())
        self.assertFalse(Path('/proc') .joinpath(str(pid)).exists())

    def test_external_manifest_symlink_is_rejected_before_mutation(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            base=Path(tmp);root=base/'root';root.mkdir()
            external=base/'external';(external/'share/xtos').mkdir(parents=True)
            (external/'share/xtos/userspace-manifest.json').write_text('{}\n')
            (root/'usr').symlink_to(external)
            with self.assertRaises((ValueError,OSError)):
                runtime.prepare_runtime_devnull(root)
            self.assertFalse((root/'dev').exists())

    def test_empty_manifest_is_rejected_before_mutation(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            root=Path(tmp);(root/'usr/share/xtos').mkdir(parents=True)
            (root/'usr/share/xtos/userspace-manifest.json').write_text('{}\n')
            with self.assertRaises(ValueError):runtime.prepare_runtime_devnull(root)
            self.assertFalse((root/'dev').exists())

    def test_cleanup_does_not_follow_replaced_dev_directory(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            root=Path(tmp);(root/'dev').mkdir()
            (root/'usr/share/xtos').mkdir(parents=True)
            (root/'usr/share/xtos/userspace-manifest.json').write_text(fixture_manifest())
            created=runtime.prepare_runtime_devnull(root)
            old=root/'saved-dev';(root/'dev').rename(old)
            external=root/'external';external.mkdir()
            os.mknod(external/'null',stat.S_IFCHR|0o600,os.makedev(1,3))
            (root/'dev').symlink_to(external)
            runtime.cleanup_runtime_devnull(root,created)
            self.assertTrue((external/'null').exists())
            self.assertFalse((old/'null').exists())

    def test_cleanup_preserves_replacement_node_with_same_device_numbers(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            root=Path(tmp);(root/'dev').mkdir()
            (root/'usr/share/xtos').mkdir(parents=True)
            (root/'usr/share/xtos/userspace-manifest.json').write_text(fixture_manifest())
            state=runtime.prepare_runtime_devnull(root)
            (root/'dev/null').rename(root/'dev/original')
            os.mknod(root/'dev/null',stat.S_IFCHR|0o600,os.makedev(1,3))
            replacement=(root/'dev/null').stat().st_ino
            runtime.cleanup_runtime_devnull(root,state)
            self.assertEqual((root/'dev/null').stat().st_ino,replacement)

if __name__=='__main__':unittest.main()
