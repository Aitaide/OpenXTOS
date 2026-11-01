"""Current frame source, maintenance entry and shipping closure checks."""
from pathlib import Path
import json
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
FRAME_PAYLOADS = (
    'drivers/xtos/bci/xtos_bci_frame.c',
    'drivers/xtos/bci/xtos_bci_frame.h',
    'drivers/xtos/bci/xtos_bci_frame_snapshot.c',
    'drivers/xtos/bci/xtos_bci_frame_snapshot.h',
    'include/uapi/linux/xtos_bci_frame.h',
)


class FrameIntegrationTests(unittest.TestCase):
    def test_patch_embeds_exact_current_frame_sources(self):
        pin = json.loads((ROOT/'kernel/source-pin.json').read_bytes())
        sections = re.split(rb'(?=^diff --git )',
                            (ROOT/pin['xtos_patch']).read_bytes(), flags=re.M)
        payloads = {}
        for section in sections:
            if b'\n--- /dev/null\n' not in section:
                continue
            match = re.search(rb'^\+\+\+ b/(.+)$', section, re.M)
            self.assertIsNotNone(match)
            payloads[match.group(1).decode()] = b''.join(
                line[1:] for line in section.splitlines(keepends=True)
                if line.startswith(b'+') and not line.startswith(b'+++'))
        for path in FRAME_PAYLOADS:
            with self.subTest(path=path):
                self.assertIn(path, payloads)
                self.assertEqual(payloads[path], (ROOT/'kernel/linux'/path).read_bytes())

    def test_maintenance_executes_frame_closure_suites(self):
        text = (ROOT/'scripts/test.sh').read_text()
        self.assertIn('scripts/test_bci_frames.py', text)
        self.assertIn('test_bci_kbuild.py', text)
        runner = ROOT/'scripts/test_bci_frames.py'
        self.assertTrue(runner.is_file())
        program = runner.read_text()
        for name in ('test_bci_frame_uapi.py', 'test_bci_frame_ring.py',
                     'test_bci_frame_ring_runner.py', 'test_bci_frame_core.py',
                     'test_bci_frame_snapshot_kernel.py', 'test_bci_frame_sdk.py',
                     'test_bci_frame_install.py', 'test_bci_frame_guest_sources.py',
                     'test_bci_frame_integration.py'):
            with self.subTest(suite=name):
                self.assertIn(name, program)

    def test_actual_Live_install_provides_frame_header_library_closure(self):
        import importlib.util
        import os
        import shlex
        import subprocess
        import tempfile
        live_spec = importlib.util.spec_from_file_location('frame_live_install', ROOT/'scripts/build_live.py')
        live = importlib.util.module_from_spec(live_spec)
        live_spec.loader.exec_module(live)
        client_spec = importlib.util.spec_from_file_location('frame_install_client', ROOT/'tests/test_bci_frame_install.py')
        client = importlib.util.module_from_spec(client_spec)
        client_spec.loader.exec_module(client)
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as temporary:
            directory = Path(temporary)
            stage, work = directory/'root', directory/'work'
            (stage/'usr/bin').mkdir(parents=True)
            work.mkdir()
            live.install_sdk(stage, work)
            self.assertEqual((stage/'usr/bin/xtos-rt-status').read_bytes(),
                             (ROOT/'system/rootfs/usr/bin/xtos-rt-status').read_bytes())
            self.assertTrue((stage/'usr/bin/xtos-rt-status').stat().st_mode & 0o111)
            include = stage/'usr/include'
            for name in client.HEADERS:
                self.assertEqual((include/'xtos'/name).read_bytes(),
                                 (ROOT/'sdk/libxtos-bci/include/xtos'/name).read_bytes())
            source, binary, dependency = directory/'client.c', directory/'client', directory/'client.d'
            source.write_text(client.CLIENT)
            env = dict(os.environ)
            for name in ('CPATH', 'C_INCLUDE_PATH', 'CPLUS_INCLUDE_PATH', 'LIBRARY_PATH',
                         'LD_LIBRARY_PATH', 'LD_PRELOAD'):
                env.pop(name, None)
            compiled = subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-static',
                '-I'+str(include), '-MMD', '-MF', str(dependency), str(source),
                str(stage/'usr/lib/libxtos-bci.a'), '-o', str(binary)],
                env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(compiled.returncode, 0, compiled.stdout+compiled.stderr)
            result = subprocess.run([str(binary)], env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            self.assertEqual(result.stdout, 'installed_frame_client=pass null_api_cases=8\n')
            headers = [Path(p).resolve() for p in shlex.split(
                dependency.read_text().replace('\\\n', ' ').split(':', 1)[1]) if p.endswith('.h')]
            self.assertEqual({p.name for p in headers}, client.HEADERS-{'bci_uapi.h'})
            self.assertTrue(all(p.is_relative_to(include.resolve()) for p in headers))
        system = (ROOT/'scripts/build_system.sh').read_text()
        self.assertIn('"$STAGING/sdk/libxtos-$library/libxtos-$library.a"', system)
        self.assertIn('"$STAGING/sdk/libxtos-$library/include/xtos/."', system)


if __name__ == '__main__':
    unittest.main()
