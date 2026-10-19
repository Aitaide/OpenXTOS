#!/usr/bin/env python3
"""Bind Live acceptance to one clean source and exact on-disk bytes."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/verify_live_acceptance.py'


class LiveAcceptanceTests(unittest.TestCase):
    def verifier(self):
        self.assertTrue(SCRIPT.is_file(), 'Live acceptance provenance gate is missing')
        spec = importlib.util.spec_from_file_location('live_acceptance', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_total_entrypoint_includes_provenance_gate_regression(self):
        script = (ROOT / 'scripts/test.sh').read_text()
        self.assertIn('"$ROOT/tests/test_live_acceptance.py"', script,
                      'Live provenance regressions are not part of maintained tests')

    def test_boot_gate_rejects_stale_duplicate_and_partial_modes(self):
        module = self.verifier()
        self.assertTrue(hasattr(module, 'verify_boot_evidence'),
                        'Four-mode evidence is not validated against artifact identity')
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as temporary:
            evidence = Path(temporary)
            rows = []
            for mode in ('bios-cd', 'uefi-cd', 'bios-usb', 'uefi-usb'):
                row = {'mode': mode, 'status': 'pass', 'checks': {
                    'internal_disk_sha256_unchanged': True,
                    'host_key_trusted_via_serial': True,
                    'authentication_offers': ['publickey'],
                    'root_mounts': 'LIVE_MOUNTS=PASS',
                    'sdk_selftest': 'guest_userspace=pass',
                    'developer_tools': 'DEVELOPER_USER=PASS',
                    'readonly_media': 'LIVE_READONLY=PASS',
                }}
                rows.append(row)
                (evidence / f'{mode}.result.json').write_text(json.dumps(row))
                (evidence / f'{mode}.serial.log').write_text(
                    'XTOS_DHCP interface=eth0 address=192.0.2.10\nXTOS_READY mode=live\n')
            result = {'schema_version': 1, 'status': 'pass',
                      'iso_sha256': 'c' * 64, 'modes': rows}
            aggregate = evidence / 'results.json'
            aggregate.write_text(json.dumps(result))
            self.assertEqual(set(module.verify_boot_evidence(evidence, {'iso_sha256': 'c' * 64})),
                             {'bios-cd', 'uefi-cd', 'bios-usb', 'uefi-usb'})
            for invalid in [dict(result, iso_sha256='d' * 64),
                            dict(result, modes=rows[:3]),
                            dict(result, modes=[rows[0]] * 4),
                            dict(result, status='fail')]:
                aggregate.write_text(json.dumps(invalid))
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    module.verify_boot_evidence(evidence, {'iso_sha256': 'c' * 64})
            aggregate.write_text(json.dumps(result))
            (evidence / 'bios-cd.serial.log').write_text('shell exists but startup failed\n')
            with self.assertRaises(ValueError):
                module.verify_boot_evidence(evidence, {'iso_sha256': 'c' * 64})

    def test_iso_embedded_payload_is_verified_against_manifest(self):
        module = self.verifier()
        self.assertTrue(hasattr(module, 'verify_iso_payloads'),
                        'ISO embedded payloads are not bound to staging hashes')
        import shutil
        import subprocess
        self.assertIsNotNone(shutil.which('xorriso'), 'xorriso is required for Live acceptance')
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as temporary:
            root = Path(temporary)
            image_root = root / 'iso-root'
            hashes = {}
            for key, relative in module.PAYLOADS.items():
                if key == 'iso_sha256':
                    continue
                target = image_root / Path(relative).relative_to('iso')
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(('embedded:' + key).encode())
                hashes[key] = hashlib.sha256(target.read_bytes()).hexdigest()
            iso = root / 'fixture.iso'
            subprocess.run(['xorriso', '-as', 'mkisofs', '-quiet', '-R', '-o', str(iso),
                            str(image_root)], check=True, capture_output=True)
            self.assertEqual(module.verify_iso_payloads(iso, hashes), hashes)
            invalid = dict(hashes, kernel_sha256='0' * 64)
            with self.assertRaises(ValueError):
                module.verify_iso_payloads(iso, invalid)

    def test_gate_verifies_all_payload_hashes_and_clean_source(self):
        module = self.verifier()
        with (tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as temporary,
              tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as external_temporary):
            candidate = Path(temporary)
            external = Path(external_temporary)
            paths = {
                'iso_sha256': 'images/xtos-developer-live-x86_64.iso',
                'kernel_sha256': 'iso/boot/bzImage',
                'initramfs_sha256': 'iso/boot/initramfs.gz',
                'rootfs_squashfs_sha256': 'iso/live/rootfs.squashfs',
                'kernel_config_sha256': 'images/kernel.config',
            }
            document = {'schema_version': 1, 'architecture': 'x86_64',
                        'source_git_head': 'a' * 40, 'source_state': 'clean',
                        'private_key_included': False, 'iso_bytes': 0,
                        'status': 'built-unverified-live-candidate'}
            for key, relative in paths.items():
                path = candidate / relative
                if key == 'iso_sha256':
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(('fixture:' + key).encode())
                document[key] = hashlib.sha256(path.read_bytes()).hexdigest()
            iso = candidate / paths['iso_sha256']
            subprocess.run(['xorriso', '-as', 'mkisofs', '-quiet', '-R', '-o', str(iso),
                            str(candidate / 'iso')], check=True, capture_output=True)
            document['iso_sha256'] = hashlib.sha256(iso.read_bytes()).hexdigest()
            config = candidate / paths['kernel_config_sha256']
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_bytes(b'CONFIG_PREEMPT_RT=y\n')
            document['kernel_config_sha256'] = hashlib.sha256(config.read_bytes()).hexdigest()
            document['iso_bytes'] = iso.stat().st_size
            manifest = candidate / 'images/build-manifest.json'
            manifest.write_text(json.dumps(document))
            accepted = module.verify_artifacts(candidate, expected_source='a' * 40)
            self.assertEqual(accepted['source_commit'], 'a' * 40)
            self.assertEqual(accepted['iso_sha256'], document['iso_sha256'])

            # This tiny, real ISO is integrity-valid but is not boot evidence.
            # The complete acceptance CLI must not approve it without --evidence.
            missing = subprocess.run(
                [__import__('sys').executable, str(SCRIPT), '--candidate', str(candidate),
                 '--expected-source', 'a' * 40], capture_output=True, text=True)
            self.assertNotEqual(missing.returncode, 0,
                                'missing boot evidence bypasses full Live acceptance')
            self.assertNotIn('"status": "pass"', missing.stdout)
            self.assertIn('--evidence', missing.stderr)

            evidence = candidate / 'evidence'
            evidence.mkdir()
            rows = []
            for mode in module.MODES:
                row = {'mode': mode, 'status': 'pass', 'checks': {
                    'internal_disk_sha256_unchanged': True,
                    'host_key_trusted_via_serial': True,
                    'authentication_offers': ['publickey'],
                    'root_mounts': 'LIVE_MOUNTS=PASS',
                    'sdk_selftest': 'guest_userspace=pass',
                    'developer_tools': 'DEVELOPER_USER=PASS',
                    'readonly_media': 'LIVE_READONLY=PASS',
                }}
                rows.append(row)
                (evidence / f'{mode}.result.json').write_text(json.dumps(row))
                (evidence / f'{mode}.serial.log').write_text(
                    'XTOS_DHCP interface=eth0\nXTOS_READY mode=live\n')
            (evidence / 'results.json').write_text(json.dumps({
                'schema_version': 1, 'status': 'pass',
                'iso_sha256': document['iso_sha256'], 'modes': rows}))
            complete = subprocess.run(
                [__import__('sys').executable, str(SCRIPT), '--candidate', str(candidate),
                 '--expected-source', 'a' * 40, '--evidence', str(evidence)],
                capture_output=True, text=True)
            self.assertEqual(complete.returncode, 0, complete.stderr)
            self.assertEqual(json.loads(complete.stdout)['status'], 'pass')
            self.assertEqual(set(json.loads(complete.stdout)['boot_modes']), set(module.MODES))

            def run_with_report(report, kernel_config=None):
                command = [__import__('sys').executable, str(SCRIPT),
                           '--candidate', str(candidate), '--expected-source', 'a' * 40,
                           '--evidence', str(evidence)]
                if kernel_config is not None:
                    command += ['--kernel-config', str(kernel_config)]
                command += ['--report', str(report)]
                return subprocess.run(command, capture_output=True, text=True)

            def assert_report_refused(report, protected, kernel_config=None):
                original = protected.read_bytes()
                with self.subTest(report_target=str(report)):
                    try:
                        rejected = run_with_report(report, kernel_config)
                        self.assertNotEqual(
                            rejected.returncode, 0,
                            f'report overwrote input {protected}; '
                            f'input_unchanged={protected.read_bytes() == original}; '
                            f'stdout={rejected.stdout}')
                        self.assertEqual(protected.read_bytes(), original)
                        self.assertNotIn('"status": "pass"', rejected.stdout)
                        self.assertIn('LIVE_ACCEPTANCE=FAIL', rejected.stderr)
                    finally:
                        # Keep later cases independent when the unfixed CLI corrupts an input.
                        if protected.read_bytes() != original:
                            protected.write_bytes(original)

            protected_inputs = [iso, manifest, evidence / 'results.json',
                                evidence / 'bios-cd.result.json',
                                evidence / 'bios-cd.serial.log', config]
            for protected in protected_inputs:
                assert_report_refused(protected, protected)

            external_config = external / 'external-kernel.config'
            external_config.write_bytes(config.read_bytes())
            assert_report_refused(external_config, external_config, external_config)

            for index, protected in enumerate(protected_inputs + [external_config]):
                for link_type in ('symlink', 'hardlink'):
                    report_alias = external / f'report-{index}-{link_type}.json'
                    if link_type == 'symlink':
                        report_alias.symlink_to(protected)
                    else:
                        os.link(protected, report_alias)
                    assert_report_refused(report_alias, protected, external_config)

            images_alias = external / 'candidate-images'
            images_alias.symlink_to(iso.parent, target_is_directory=True)
            assert_report_refused(images_alias / iso.name, iso)

            for fresh_report in (evidence / 'acceptance.json', images_alias / 'acceptance.json'):
                with self.subTest(fresh_report=str(fresh_report)):
                    written = run_with_report(fresh_report)
                    self.assertEqual(written.returncode, 0, written.stderr)
                    self.assertEqual(json.loads(written.stdout)['status'], 'pass')
                    first_report_bytes = fresh_report.read_bytes()
                    self.assertEqual(json.loads(first_report_bytes), json.loads(written.stdout))
                    repeated = run_with_report(fresh_report)
                    self.assertNotEqual(repeated.returncode, 0,
                                        'an existing report must not be overwritten')
                    self.assertEqual(fresh_report.read_bytes(), first_report_bytes)

            for key, relative in paths.items():
                path = candidate / relative
                original = path.read_bytes()
                path.write_bytes(original + b'tamper')
                with self.subTest(artifact=relative), self.assertRaises(ValueError):
                    module.verify_artifacts(candidate, expected_source='a' * 40)
                path.write_bytes(original)
            for field, value in [('source_git_head', 'b' * 40),
                                 ('source_state', 'dirty'),
                                 ('private_key_included', True),
                                 ('iso_bytes', document['iso_bytes'] + 1)]:
                invalid = dict(document, **{field: value})
                manifest.write_text(json.dumps(invalid))
                with self.subTest(field=field), self.assertRaises(ValueError):
                    module.verify_artifacts(candidate, expected_source='a' * 40)


if __name__ == '__main__':
    unittest.main(verbosity=2)
