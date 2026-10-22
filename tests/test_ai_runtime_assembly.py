#!/usr/bin/env python3
"""Fresh-runtime descriptor contract and deterministic packaging regression."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('provision', ROOT/'scripts/provision_ai_runtime.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)

class FreshDescriptorTests(unittest.TestCase):
    def test_fresh_claim_requires_bound_evidence(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            lock = root/'lock'
            lock.write_text('fixture lock')
            evidence = {'schema': 'xtos-ai-fresh-assembly-v1',
                        'acquisition_sha256': 'a2fb0554c65b1daec82a22ad94d99d4c538ef069d1b827dc0a1cc28f27c057ab',
                        'lock_sha256': p.sha256_file(lock),
                        'interpreter': {'classification': 'reused_verified_artifact', 'fresh_build': False,
                                        'bundle_sha256': '2d5322a229875416717500c9b29609e204ff1c73e6fa35e9ba669313e849a58d'},
                        'fresh_wheel_install': True, 'install_rc': 0, 'no_index': True,
                        'require_hashes': True, 'no_deps': True, 'no_compile': True,
                        'console_scripts': 'omitted; RECORD relocation paths not executable inventory',
                        'wheels': [], 'source_manifest_path': 'notices/sources.json'}
            versions = {'coloredlogs':'15.0.1','flatbuffers':'25.12.19','humanfriendly':'10.0',
                        'mpmath':'1.3.0','numpy':'1.26.4','onnxruntime':'1.23.2',
                        'packaging':'26.3','protobuf':'5.29.5','sympy':'1.14.0'}
            files = {'notices/sources.json': p.canonical_json({'scope':'not compiled from these sources'})}
            for name,version in versions.items():
                prefix = f'python/lib/python3.11/site-packages/{name}-{version}.dist-info/'
                record = b'original wheel RECORD\n'
                files[prefix+'METADATA'] = f'Name: {name}\nVersion: {version}\n'.encode()
                files['notices/wheel-records/'+name+'.RECORD'] = record
                evidence['wheels'].append({'name':name,'version':version,'sha256':'1'*64,
                    'record_path':'notices/wheel-records/'+name+'.RECORD',
                    'record_sha256':__import__('hashlib').sha256(record).hexdigest(),
                    'verified_members':1,'bad':[],'missing':[]})
            # The selected hashes must actually be in the unchanged lock supplied.
            lock.write_text(''.join(f'{n}=={v} --hash=sha256:{"1"*64}\n' for n,v in versions.items()))
            evidence['lock_sha256'] = p.sha256_file(lock)
            files['notices/flatbuffers/source_notices/flatbuffers-25.12.19/LICENSE'] = b'Apache License fixture'
            files['notices/assembly.json'] = p.canonical_json(evidence)
            bundle = root/'runtime.tar.gz'
            with tarfile.open(bundle,'w:gz') as tf:
                dirs = {'clean'}
                for name in files:
                    dirs.update(str(x) for x in (Path('clean')/name).parents if str(x)!='.')
                for name in sorted(dirs,key=lambda x:(x.count('/'),x)):
                    ti=tarfile.TarInfo(name); ti.type=tarfile.DIRTYPE; ti.mode=0o755; tf.addfile(ti)
                for name,data in files.items():
                    ti=tarfile.TarInfo('clean/'+name); ti.size=len(data); ti.mode=0o644
                    tf.addfile(ti,io.BytesIO(data))
            entries=p.inspect_archive(bundle)
            with tarfile.open(bundle) as tf: licenses=p._metadata_from_archive(tf,entries)
            self.assertEqual(next(x for x in licenses['packages'] if x['name']=='flatbuffers')['license_status'], 'bundled')
            provenance={'classification':'reused_interpreter_fresh_locked_wheels','fresh_wheel_install':True,
                        'assembly_evidence_path':'notices/assembly.json',
                        'assembly_evidence_sha256':__import__('hashlib').sha256(files['notices/assembly.json']).hexdigest()}
            desc={'schema':p.SCHEMA,'architecture':'x86_64','runtime':p.REQUIRED_RUNTIME,
                  'elf_system_sonames':p.ELF_SYSTEM_SONAMES,'bundle':{'sha256':p.sha256_file(bundle),'bytes':bundle.stat().st_size},
                  'lock':{'sha256':p.sha256_file(lock)},'members':entries,'package_licenses':licenses,'provenance':provenance}
            path=root/'descriptor.json'; path.write_bytes(p.canonical_json(desc))
            self.assertEqual(p._verify_descriptor(bundle,path,lock)[0]['provenance'],provenance)
            desc['provenance']['assembly_evidence_sha256']='0'*64
            path.write_bytes(p.canonical_json(desc))
            with self.assertRaisesRegex(p.BundleError,'evidence'):
                p._verify_descriptor(bundle,path,lock)

class AssemblyPackagingTests(unittest.TestCase):
    def test_pack_is_reproducible_and_rejects_unsafe_links(self):
        spec = importlib.util.spec_from_file_location('assembly', ROOT/'scripts/assemble_ai_runtime.py')
        self.assertTrue((ROOT/'scripts/assemble_ai_runtime.py').is_file(), 'maintained assembler missing')
        a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); clean=root/'clean'; (clean/'python').mkdir(parents=True)
            (clean/'python/file').write_bytes(b'fixture')
            one=root/'one.tar.gz'; two=root/'two.tar.gz'
            a.pack(clean,one); __import__('os').utime(clean/'python/file',(100,200)); a.pack(clean,two)
            self.assertEqual(one.read_bytes(),two.read_bytes())
            self.assertEqual(len(p.inspect_archive(one)),3)
            (clean/'python/link').symlink_to('../../outside')
            with self.assertRaises(ValueError): a.pack(clean,root/'unsafe.tar.gz')

if __name__=='__main__': unittest.main(verbosity=2)
