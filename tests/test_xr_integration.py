"""XR maintenance/source dependency wiring; not headset evidence."""
from pathlib import Path
import hashlib
import importlib.util
import json
import unittest

ROOT=Path(__file__).resolve().parents[1]

class XrIntegrationTests(unittest.TestCase):
    def test_xr_suites_are_maintained_exactly_once(self):
        text=(ROOT/'scripts/test.sh').read_text()
        for name in ('test_xr_native.py','test_xr_multimodal.py','test_xr_feedback.py','test_xr_runtime.py',
                     'test_xr_install.py','test_xr_integration.py'):
            self.assertEqual(text.count(name),1,name)

    def test_pinned_headers_have_upstream_bytes_not_runtime_claim(self):
        base=ROOT/'tools/xtos-xr/vendor/openxr'
        provenance=json.loads((base/'provenance.json').read_bytes())
        self.assertEqual(provenance['tag'],'release-1.1.57')
        self.assertEqual(set(provenance['files']),{'include/openxr/openxr.h',
            'include/openxr/openxr_platform.h','include/openxr/openxr_platform_defines.h',
            'COPYING.adoc','LICENSES/Apache-2.0.txt','LICENSES/MIT.txt'})
        for name,binding in provenance['files'].items():
            p=base/name
            self.assertFalse(p.is_symlink())
            self.assertEqual(p.stat().st_size,binding['bytes'])
            self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(),binding['sha256'])
            self.assertTrue(binding['url'].startswith(
                'https://raw.githubusercontent.com/KhronosGroup/OpenXR-SDK/release-1.1.57/'))
        self.assertIn('not loader/runtime',provenance['scope'])

    def test_sbom_includes_pinned_header_and_license_inputs(self):
        spec=importlib.util.spec_from_file_location('xr_sbom_integration',ROOT/'tools/xtos-sbom/gen_sbom.py')
        if spec is None or spec.loader is None:raise RuntimeError('SBOM unavailable')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        entry=next(row for row in module.COMPONENTS if row['name']=='openxr-headers')
        self.assertEqual(entry['version'],'1.1.57')
        self.assertEqual(entry['scope'],'required')
        self.assertEqual(entry['license'],'Apache-2.0')
        expected={'include/openxr/openxr.h','include/openxr/openxr_platform.h',
            'include/openxr/openxr_platform_defines.h','COPYING.adoc',
            'LICENSES/Apache-2.0.txt','LICENSES/MIT.txt','provenance.json'}
        self.assertEqual(set(entry['hash_globs']),expected)
        built=module.build_component(entry)
        self.assertEqual(len(built['hashes']),len(expected))

    def test_application_sbom_declares_runtime_inputs_and_gate_dependencies(self):
        spec=importlib.util.spec_from_file_location('xr_application_sbom',ROOT/'tools/xtos-sbom/gen_sbom.py')
        if spec is None or spec.loader is None:raise RuntimeError('SBOM unavailable')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        entry=next(row for row in module.COMPONENTS if row['name']=='xtos-xr')
        self.assertEqual(entry['scope'],'required')
        self.assertEqual(entry['version'],'0.1.0')
        self.assertEqual(set(entry['hash_globs']),{'Makefile',
            'include/xtos_xr/runtime_adapter.h','src/runtime_adapter.c','src/xr_probe.c',
            'include/xtos_xr/eye_session.h','src/eye_session.c','src/xr_eye_probe.c',
            'xtos_xr/__init__.py','xtos_xr/__main__.py','xtos_xr/runtime_events.py'})
        self.assertEqual(entry['requires'],['openxr-headers','xtos-action-gate'])

if __name__=='__main__':unittest.main()
