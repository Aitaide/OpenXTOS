#!/usr/bin/env python3
import importlib.util
import json
from pathlib import Path
import tempfile
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import unittest
from unittest import mock
import urllib.error
from email.message import Message

HERE = Path(__file__).resolve().parent

def module(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + '.py'))
    if spec is None or spec.loader is None:
        raise RuntimeError('could not load release module')
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result

publisher = module('publish')
recipe = module('release')

class PublicationTests(unittest.TestCase):
    def fixture(self, root):
        for name in publisher.EXPECTED:
            (root / name).write_bytes(('test artifact ' + name).encode())

    def test_create_conflict_never_uploads_or_updates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root)
            api = mock.Mock()
            api.request.side_effect = urllib.error.HTTPError('https://api.github.com', 422, 'conflict', Message(), None)
            with self.assertRaises(urllib.error.HTTPError):
                publisher.publish(root, api, 'recipe-sha')
            self.assertEqual(api.request.call_count, 1)
            self.assertEqual(api.request.call_args.args[:2], ('POST', 'releases'))
            api.upload.assert_not_called()

    def test_missing_artifact_precedes_external_write(self):
        with tempfile.TemporaryDirectory() as temp:
            api = mock.Mock()
            with self.assertRaisesRegex(RuntimeError, 'missing artifact'):
                publisher.publish(Path(temp), api, 'recipe-sha')
            api.request.assert_not_called()

    def test_complete_upload_reads_back_inventory_before_publish(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root)
            class FakeAPI:
                def __init__(self):
                    self.calls = []
                    self.assets = []
                def request(self, method, path, payload=None):
                    self.calls.append((method, path, payload))
                    if method == 'POST':
                        return {'id': 42, 'upload_url': 'https://uploads.github.com/owned/42/assets{?name}'}
                    if method == 'GET':
                        return {'assets': self.assets}
                    if method == 'PATCH':
                        return {'draft': False, 'tag_name': publisher.TAG, 'html_url': 'https://github.com/Aitaide/OpenXTOS/releases/tag/v1.0.2'}
                    raise RuntimeError('unexpected API call')
                def upload(self, url, path):
                    if '/owned/42/' not in url:
                        raise RuntimeError('unowned upload destination')
                    result = {'name': path.name, 'size': path.stat().st_size, 'state': 'uploaded', 'digest': 'sha256:' + publisher.digest(path)}
                    self.assets.append(result)
                    return result
            api = FakeAPI()
            publisher.publish(root, api, 'recipe-sha')
            self.assertEqual([x[0] for x in api.calls], ['POST', 'GET', 'PATCH'])
            self.assertTrue(api.calls[0][2]['draft'])
            self.assertFalse(api.calls[-1][2]['draft'])
            self.assertEqual(len(api.assets), 6)
            manifest = json.loads((root / 'RELEASE-MANIFEST.json').read_text())
            self.assertEqual(manifest['source_commit'], recipe.SOURCE_SHA)
            overlay = manifest['applied_release_overlays']['x86_64'][0]
            self.assertEqual(overlay['sha256'], recipe.digest(HERE / 'xtos-install'))
            self.assertEqual(overlay['packaged_path'], '/usr/sbin/xtos-install')
            bounds = manifest['applied_release_overlays']['x86_64'][1]
            self.assertEqual(bounds['sha256'], recipe.digest(HERE / 'cyton-read-bounds.patch'))
            self.assertEqual(bounds['packaged_path'], '/usr/bin/bci')
            self.assertEqual(manifest['applied_release_overlays']['arm64'], [])

class SourceIdentityTests(unittest.TestCase):
    def test_wrong_commit_is_refused(self):
        with mock.patch.object(recipe, 'git', return_value='wrong'):
            with self.assertRaisesRegex(RuntimeError, 'wrong source'):
                recipe.require_source(HERE)

    def test_dirty_source_is_refused(self):
        with mock.patch.object(recipe, 'git', side_effect=[recipe.SOURCE_SHA, ' M source.c']):
            with self.assertRaisesRegex(RuntimeError, 'clean'):
                recipe.require_source(HERE)

    def test_packaged_payload_mismatch_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'images').mkdir()
            (root / 'images/xtos-bzImage').write_bytes(b'expected')
            def extract(args, **kwargs):
                (root / 'iso-readback/bzImage').write_bytes(b'stale image')
                (root / 'iso-readback/initramfs.gz').write_bytes(b'initramfs')
            with mock.patch.object(recipe, 'run', side_effect=extract):
                with self.assertRaisesRegex(RuntimeError, 'embedded payload mismatch'):
                    recipe.verify_iso(root)

class TrackedRestorationTests(unittest.TestCase):
    def fixture(self, root):
        import subprocess
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        subprocess.run(['git', '-C', str(root), 'config', 'user.email', 'release-test@example.invalid'], check=True)
        subprocess.run(['git', '-C', str(root), 'config', 'user.name', 'Release test'], check=True)
        path = root / 'sdk/libxtos-bci/tests/test_samples'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'tracked fixture executable')
        path.chmod(0o755)
        subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(root), 'commit', '-qm', 'fixture'], check=True)
        return path

    def test_cleanup_deletion_is_restored(self):
        from preserve_tracked import preserved
        import stat
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = self.fixture(root)
            with preserved(root):
                path.unlink()
            self.assertEqual(path.read_bytes(), b'tracked fixture executable')
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)

    def test_command_failure_still_restores_and_fails(self):
        from preserve_tracked import preserved
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = self.fixture(root)
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                with preserved(root):
                    path.unlink()
                    raise RuntimeError('injected command failure')
            self.assertEqual(path.read_bytes(), b'tracked fixture executable')

    def test_existing_change_is_not_overwritten(self):
        from preserve_tracked import preserved
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = self.fixture(root)
            path.write_bytes(b'caller change')
            with self.assertRaisesRegex(RuntimeError, 'preexisting'):
                with preserved(root):
                    pass
            self.assertEqual(path.read_bytes(), b'caller change')

if __name__ == '__main__':
    unittest.main()
