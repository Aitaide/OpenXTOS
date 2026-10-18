#!/usr/bin/env python3
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import importlib.util
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/fetch_userspace.py'

class FetchTests(unittest.TestCase):
    def test_profile_resolution_includes_tools_without_network(self):
        result = subprocess.run([sys.executable, str(SCRIPT), '--profile', 'developer', '--arch', 'x86_64', '--describe'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        for name in ['nano', 'wget', 'python3-venv', 'python3-pip', 'golang-go', 'gcc', 'ncurses-base']:
            self.assertIn(name, data['requested_packages'])
        self.assertEqual(data['defaults']['GOTOOLCHAIN'], 'local')
        self.assertEqual(len(data['requested_packages']), len(set(data['requested_packages'])))

    def test_live_profile_has_gpu_api_providers_and_diagnostics(self):
        result = subprocess.run([sys.executable, str(SCRIPT), '--profile', 'live-developer', '--arch', 'x86_64', '--describe'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        for name in ['mesa-vulkan-drivers', 'vulkan-tools', 'mesa-opencl-icd', 'clinfo']:
            self.assertIn(name, data['requested_packages'])
        for command in ['vulkaninfo --help', 'clinfo --help']:
            self.assertIn(command, data['required_commands'])

    def test_unsupported_architecture_is_rejected_without_work_dir(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            work = Path(tmp) / 'download'
            result = subprocess.run([sys.executable, str(SCRIPT), '--profile', 'developer', '--arch', 'arm64', '--work-dir', str(work), '--lock-output', str(Path(tmp)/'lock.json')], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('not supported', result.stderr)
            self.assertFalse(work.exists())

    def test_apt_config_path_characters_are_rejected_before_creating_work_dir(self):
        work = ROOT / '.build/userspace' / 'unsafe"directory'
        result = subprocess.run([sys.executable, str(SCRIPT), '--work-dir', str(work), '--lock-output', str(work/'lock.json')], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unsafe work-dir', result.stderr)
        self.assertFalse(work.exists())

    def test_fetch_refuses_to_reuse_existing_download_state(self):
        import uuid
        work = ROOT / '.build/userspace' / ('fetch-fixture-'+uuid.uuid4().hex)
        work.mkdir(parents=True)
        (work/'sentinel').write_text('keep')
        try:
            result = subprocess.run([sys.executable, str(SCRIPT), '--work-dir', str(work), '--lock-output', str(work/'lock.json')], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('fresh', result.stderr)
            self.assertEqual((work/'sentinel').read_text(),'keep')
        finally:
            (work/'sentinel').unlink();work.rmdir()

    def test_apt_initialization_ignores_inherited_host_hook_config(self):
        spec=importlib.util.spec_from_file_location('fetch',SCRIPT)
        assert spec is not None and spec.loader is not None
        fetch=importlib.util.module_from_spec(spec);spec.loader.exec_module(fetch)
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            base=Path(tmp);parts=base/'apt.conf.d';parts.mkdir()
            config=base/'isolated.conf'
            config.write_text(f'Dir::Etc "{base}";\nDir::Etc::parts "apt.conf.d";\nDir::Etc::main "-";\nXTOS::Sandbox "true";\n')
            host=base/'host.conf'
            host.write_text('APT::Update::Pre-Invoke {"XTOS_HOST_HOOK";};\n')
            with mock.patch.dict(os.environ,{'APT_CONFIG':str(host),'PYTHONPATH':'host-value'}):
                output=fetch.run(['apt-config','dump'],base,'isolated-config',apt_config=config)
            self.assertIn('XTOS::Sandbox "true"',output)
            self.assertNotIn('XTOS_HOST_HOOK',output)
            self.assertNotIn('Pre-Invoke',output)

if __name__ == '__main__':
    unittest.main()
