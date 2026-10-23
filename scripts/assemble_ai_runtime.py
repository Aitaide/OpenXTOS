#!/usr/bin/env python3
"""Offline, fresh nine-wheel private CPU runtime assembly; no interpreter rebuild.

Inputs are a fixed acquisition manifest and original verified archive. All tar
reads/extraction use provision_ai_runtime's preflight. Source archives establish
versioned source availability, NOT correspondence to embedded native binaries.
"""
from __future__ import annotations
import argparse
import csv
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import provision_ai_runtime as p

ACQUISITION = 'a2fb0554c65b1daec82a22ad94d99d4c538ef069d1b827dc0a1cc28f27c057ab'
ORIGINAL = '2d5322a229875416717500c9b29609e204ff1c73e6fa35e9ba669313e849a58d'
LOCK = 'f79d69267e25fcbf20d98101886dc863814e110cdc6483b223b403498a2232e3'
VERSIONS = {'coloredlogs':'15.0.1','flatbuffers':'25.12.19','humanfriendly':'10.0',
            'mpmath':'1.3.0','numpy':'1.26.4','onnxruntime':'1.23.2',
            'packaging':'26.3','protobuf':'5.29.5','sympy':'1.14.0'}


def checked(path: Path, digest: str, size=None):
    if path.is_symlink() or not path.is_file() or p.sha256_file(path) != digest:
        raise p.BundleError(f'input digest/type mismatch: {path}')
    if size is not None and path.stat().st_size != size:
        raise p.BundleError(f'input size mismatch: {path}')


def pack(clean: Path, output: Path):
    """Sorted gzip/USTAR with fixed metadata; validate complete symlink graph."""
    if output.exists() or output.is_symlink():
        raise p.BundleError('archive output must be nonexistent')
    rows = [{'path':'.','type':'dir','mode':0o755}]
    for path in sorted(clean.rglob('*')):
        rel=path.relative_to(clean).as_posix()
        row={'path':rel, 'mode':0o755 if path.is_dir() or path.lstat().st_mode & 0o111 else 0o644}
        if path.is_symlink(): row.update(type='symlink',target=os.readlink(path),mode=0o777)
        elif path.is_dir(): row['type']='dir'
        elif path.is_file(): row.update(type='file',size=path.stat().st_size)
        else: raise p.BundleError('unsupported filesystem entry')
        rows.append(row)
    p._validate_symlink_graph(rows)
    with output.open('xb') as raw, gzip.GzipFile(filename='',fileobj=raw,mode='wb',mtime=0,compresslevel=6) as gz:
        with tarfile.open(fileobj=gz,mode='w',format=tarfile.PAX_FORMAT) as tf:
            for row in rows:
                ti=tarfile.TarInfo('clean' if row['path']=='.' else 'clean/'+row['path'])
                ti.mode=row['mode']; ti.uid=ti.gid=0; ti.uname=ti.gname=''; ti.mtime=0
                if row['type']=='dir': ti.type=tarfile.DIRTYPE; tf.addfile(ti)
                elif row['type']=='symlink': ti.type=tarfile.SYMTYPE; ti.linkname=row['target']; tf.addfile(ti)
                else:
                    ti.size=row['size']
                    with (clean/row['path']).open('rb') as stream: tf.addfile(ti,stream)
    p.inspect_archive(output)


def assemble(acquisition: Path, lock: Path, output: Path, qemu: Path, cpython_source: Path | None = None):
    if output.exists() or output.is_symlink(): raise p.BundleError('assembly output must be fresh')
    if output.absolute().parent != output.absolute().parent.resolve(strict=True):
        raise p.BundleError('assembly parent must be real and existing')
    checked(acquisition,ACQUISITION); checked(lock,LOCK)
    a=p._read_json(acquisition); packages=a['packages']
    if len(packages)!=9 or {x['name']:x['version'] for x in packages} != VERSIONS:
        raise p.BundleError('exact nine acquired packages required')
    original=Path(a['original_bundle']['path']); checked(original,ORIGINAL)
    old_entries=p.inspect_archive(original)
    lock_text=lock.read_text()
    subset=[]; owned=set()
    for x in packages:
        w=x['wheel']; checked(Path(w['path']),w['sha256'],w['bytes'])
        source=x['source']; checked(Path(source['path']),source['sha256'],source['bytes'])
        match=re.search(r'^'+re.escape(x['name'])+'=='+re.escape(x['version'])+r'[^\n]*(?:\n[ \t]+[^\n]*)*',lock_text,re.M)
        if not match or '--hash=sha256:'+w['sha256'] not in match.group(): raise p.BundleError('wheel is not locked')
        subset.append(f"{x['name']}=={x['version']} --hash=sha256:{w['sha256']}\n")
        with zipfile.ZipFile(w['path']) as z:
            for n in z.namelist():
                parts=PurePosixPath(n).parts
                if n.startswith('/') or '..' in parts: raise p.BundleError('unsafe wheel member')
                top=parts[0]
                if not top.endswith('.data'): owned.add(top)
    parent=output.absolute().parent
    stage=Path(tempfile.mkdtemp(prefix='.ai-assembly-',dir=parent))
    try:
        extracted=stage/'extracted'; p._extract(original,old_entries,extracted)
        clean=extracted/'clean'; site=clean/'python/lib/python3.11/site-packages'
        # Remove only known exact wheel-owned top-level leaves in our fresh root.
        for leaf in sorted(owned):
            path=site/leaf
            if path.is_symlink() or path.is_file(): path.unlink()
            elif path.is_dir(): shutil.rmtree(path)
        (stage/'requirements-runtime-nine.lock').write_text(''.join(subset))
        target=stage/'nine-wheel-target'
        python=clean/'python/bin/python3.11'
        env={'PATH':'/usr/bin:/bin','PYTHONNOUSERSITE':'1','PYTHONDONTWRITEBYTECODE':'1',
             'OMP_NUM_THREADS':'1','OPENBLAS_NUM_THREADS':'1','PIP_CONFIG_FILE':'/dev/null'}
        # Stable executable identity normalizes pip-generated RECORD script hashes.
        # Console scripts themselves are omitted, not shipped with staging paths.
        argv=[str(qemu),'-cpu','max',str(python),'-I','-B','-c',
              "import sys;sys.executable='/opt/xtos-ai/clean/python/bin/python3.11';from pip._internal.cli.main import main;raise SystemExit(main(sys.argv[1:]))",
              '--isolated','install','--no-index','--find-links',str(Path(packages[0]['wheel']['path']).parent),
              '--require-hashes','--no-deps','--no-compile','--only-binary=:all:',
              '--ignore-installed','--disable-pip-version-check','--no-warn-script-location',
              '--root-user-action=ignore','--target',str(target),'-r',str(stage/'requirements-runtime-nine.lock')]
        result=subprocess.run(argv,env=env,capture_output=True,timeout=180)
        (stage/'pip.stdout').write_bytes(result.stdout); (stage/'pip.stderr').write_bytes(result.stderr)
        if result.returncode: raise p.BundleError(f'offline pip failed: {result.stderr.decode(errors="replace")}')
        for leaf in sorted(target.iterdir()):
            if leaf.name in {'bin','share'}: continue
            dest=site/leaf.name
            if dest.exists() or dest.is_symlink(): raise p.BundleError(f'unexpected wheel collision: {dest.name}')
            shutil.move(str(leaf),dest)
        # Reused interpreter's historical console scripts are not distribution inputs.
        for path in (clean/'python/bin').iterdir():
            if not path.name.startswith('python'): path.unlink()
        notices=clean/'notices'; notices.mkdir()
        records=notices/'wheel-records'; records.mkdir()
        wheels=[]; sources=[]
        for x in packages:
            w=x['wheel']; count=0; relocations=[]
            with zipfile.ZipFile(w['path']) as z:
                record=next(n for n in z.namelist() if n.endswith('.dist-info/RECORD'))
                original_record=z.read(record); (records/(x['name']+'.RECORD')).write_bytes(original_record)
                for n in z.namelist():
                    if n.endswith('/') or n==record: continue
                    parts=PurePosixPath(n).parts
                    if parts[0].endswith('.data'):
                        if parts[1] not in {'purelib','platlib'}:
                            relocations.append(n); continue
                        rel=Path(*parts[2:])
                    else: rel=Path(*parts)
                    installed=site/rel
                    if not installed.is_file() or installed.read_bytes()!=z.read(n):
                        raise p.BundleError(f'installed wheel member mismatch: {x["name"]}: {n}')
                    count+=1
            wheels.append({'name':x['name'],'version':x['version'],'sha256':w['sha256'],
                'record_path':'notices/wheel-records/'+x['name']+'.RECORD',
                'record_sha256':hashlib.sha256(original_record).hexdigest(),
                'installed_record_sha256':p.sha256_file(site/record),'verified_members':count,
                'bad':[],'missing':[],'omitted_data_members':relocations})
            s=x['source']; row={'name':x['name'],'version':x['version'],
                'wheel':{k:w[k] for k in ('filename','sha256','bytes','requested_url')},
                'source':{k:s[k] for k in ('sha256','bytes','requested_url')},'notices':[]}
            for kind in ('wheel_notices','source_notices'):
                for notice in x['license_coverage'].get(kind,[]):
                    src=Path(notice['path']); checked(src,notice['sha256'],notice['bytes'])
                    rel=Path(x['name'])/kind/notice['archive_member']
                    dest=notices/rel; dest.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(src,dest)
                    row['notices'].append({'path':'notices/'+rel.as_posix(),'sha256':notice['sha256'],
                                           'bytes':notice['bytes'],'archive_member':notice['archive_member'],
                                           'archive_sha256':notice['archive_sha256']})
            if not row['notices']: raise p.BundleError('package has no license supplement')
            sources.append(row)
        if cpython_source:
            cp=p._read_json(cpython_source); checked(Path(cp['path']),cp['sha256'],cp['bytes'])
            with tarfile.open(cp['path']) as tf:
                license_bytes=tf.extractfile('Python-3.11.15/LICENSE').read()
            (notices/'CPython-3.11.15-LICENSE').write_bytes(license_bytes)
            sources.append({'name':'CPython','version':'3.11.15','source':{k:cp[k] for k in ('sha256','bytes','requested_url')},
                            'binary_correspondence':'version-matched source only; reused interpreter not compiled here',
                            'notices':[{'path':'notices/CPython-3.11.15-LICENSE','sha256':hashlib.sha256(license_bytes).hexdigest()}]})
        for path in list(clean.rglob('*.pyc')): path.unlink()
        for path in sorted(clean.rglob('__pycache__'),reverse=True):
            if path.is_dir(): shutil.rmtree(path)
        source_doc={'schema':'xtos-ai-source-notices-v1','packages':sources,
                    'scope':'upstream version-matched sources; NOT proof binaries were compiled from these sources',
                    'remaining_gates':['embedded native dependency/corresponding-source audit','final image dependency resolution'],
                    'record_policy':'original wheel RECORDs preserved under notices/wheel-records; installed pip RECORDs untouched; omitted console/manpage relocation references are not runnable inventory'}
        (notices/'sources.json').write_bytes(p.canonical_json(source_doc))
        evidence={'schema':'xtos-ai-fresh-assembly-v1','acquisition_sha256':ACQUISITION,'lock_sha256':LOCK,
            'interpreter':{'classification':'reused_verified_artifact','fresh_build':False,'bundle_sha256':ORIGINAL},
            'fresh_wheel_install':True,'install_rc':0,'no_index':True,'require_hashes':True,'no_deps':True,'no_compile':True,
            'console_scripts':'omitted; RECORD relocation paths not executable inventory',
            'source_manifest_path':'notices/sources.json','wheels':wheels,
            'subset_lock_sha256':p.sha256_file(stage/'requirements-runtime-nine.lock')}
        (notices/'assembly.json').write_bytes(p.canonical_json(evidence))
        bundle=stage/'clean-runtime.tar.gz'; pack(clean,bundle)
        # Repack the complete real tree, not merely a fixture.
        repeat=stage/'repeat.tar.gz'; pack(clean,repeat)
        if p.sha256_file(bundle)!=p.sha256_file(repeat): raise p.BundleError('non-reproducible bundle')
        entries=p.inspect_archive(bundle)
        with tarfile.open(bundle) as tf: licenses=p._metadata_from_archive(tf,entries)
        desc={'schema':p.SCHEMA,'name':'xtos-private-ai-cpu-runtime','architecture':'x86_64',
            'runtime':p.REQUIRED_RUNTIME,'elf_system_sonames':p.ELF_SYSTEM_SONAMES,
            'bundle':{'file':bundle.name,'bytes':bundle.stat().st_size,'sha256':p.sha256_file(bundle)},
            'lock':{'file':lock.name,'sha256':LOCK,'kind':'tools/xtos-onnx-baseline/requirements-ai.lock'},
            'members':entries,'package_licenses':licenses,
            'provenance':{'classification':'reused_interpreter_fresh_locked_wheels','fresh_wheel_install':True,
                'assembly_evidence_path':'notices/assembly.json','assembly_evidence_sha256':p.sha256_file(notices/'assembly.json'),
                'limitations':['reused CPython; not freshly compiled','native embedded dependency source audit remains open',
                    'not a full TensorFlow/PyTorch stack','QEMU functional evidence, not latency/RT/accuracy evidence',
                    source_doc['record_policy']]}}
        descriptor=stage/'descriptor.json'; descriptor.write_bytes(p.canonical_json(desc))
        p._verify_descriptor(bundle,descriptor,lock)
        ready=stage/'ready'; ready.mkdir()
        shutil.move(bundle,ready/bundle.name); shutil.copyfile(descriptor,ready/'descriptor.json')
        shutil.copyfile(stage/'pip.stdout',ready/'pip.stdout'); shutil.copyfile(stage/'pip.stderr',ready/'pip.stderr')
        shutil.copyfile(stage/'requirements-runtime-nine.lock',ready/'requirements-runtime-nine.lock')
        p._publish_noreplace(ready,output)
        return desc
    finally: shutil.rmtree(stage)


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--acquisition',type=Path,required=True); ap.add_argument('--lock',type=Path,required=True)
    ap.add_argument('--output-dir',type=Path,required=True); ap.add_argument('--qemu-x86-64',type=Path,required=True)
    ap.add_argument('--cpython-source',type=Path)
    args=ap.parse_args()
    desc=assemble(args.acquisition.resolve(),args.lock.resolve(),args.output_dir.absolute(),args.qemu_x86_64,args.cpython_source)
    print(json.dumps({'status':'assembled','bundle':desc['bundle'],'members':len(desc['members'])},sort_keys=True))

if __name__=='__main__': main()
