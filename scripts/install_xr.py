#!/usr/bin/env python3
"""Install only source-bound XR applications into an owned staging root."""
import argparse
import hashlib
import json
import os
from pathlib import Path

import subprocess
import tempfile

from install_eeg_loop import _regular_file_snapshot, _rename_noreplace

PYTHON_NAMES=('__init__.py','__main__.py','runtime_events.py')
GATE_NAMES=('__init__.py','__main__.py','schema.py','store.py')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def source_bytes(repo,relative):
    p=repo/relative
    if p.is_symlink():raise ValueError('source symlink refused: '+relative)
    return _regular_file_snapshot(p)[1]


def install(stage,repo):
    stage,repo=Path(stage).absolute(),Path(repo).resolve(strict=True)
    if stage.is_symlink() or not stage.is_dir() or stage.resolve(strict=True)!=stage:
        raise ValueError('XR staging root must be a real directory')
    inputs={}
    for name in PYTHON_NAMES:
        inputs['tools/xtos-xr/xtos_xr/'+name]=source_bytes(repo,'tools/xtos-xr/xtos_xr/'+name)
    for name in GATE_NAMES:
        inputs['tools/xtos_action_gate/'+name]=source_bytes(repo,'tools/xtos_action_gate/'+name)
    native=['tools/xtos-xr/Makefile','tools/xtos-xr/src/runtime_adapter.c',
        'tools/xtos-xr/src/xr_probe.c','tools/xtos-xr/include/xtos_xr/runtime_adapter.h',
        'tools/xtos-xr/src/eye_session.c','tools/xtos-xr/src/xr_eye_probe.c',
        'tools/xtos-xr/include/xtos_xr/eye_session.h',
        'tools/xtos-xr/vendor/openxr/include/openxr/openxr.h',
        'tools/xtos-xr/vendor/openxr/include/openxr/openxr_platform.h',
        'tools/xtos-xr/vendor/openxr/include/openxr/openxr_platform_defines.h']
    for name in native:inputs[name]=source_bytes(repo,name)
    with tempfile.TemporaryDirectory(prefix='.xtos-xr-install-',dir=stage) as directory:
        tmp=Path(directory)
        result=subprocess.run(['make','-C',str(repo/'tools/xtos-xr'),
            'BUILD_DIR='+str(tmp/'build'),'all'],capture_output=True,text=True,timeout=90)
        if result.returncode:raise ValueError('XR native compile failed: '+result.stderr)
        payload={}
        for name in PYTHON_NAMES:
            payload['usr/lib/xtos/xtos_xr/'+name]=(0o644,inputs['tools/xtos-xr/xtos_xr/'+name])
        for name in GATE_NAMES:
            payload['usr/lib/xtos/xtos_action_gate/'+name]=(0o644,inputs['tools/xtos_action_gate/'+name])
        payload['usr/bin/xr-probe']=(0o755,(tmp/'build/xr-probe').read_bytes())
        payload['usr/bin/xr-eye-probe']=(0o755,(tmp/'build/xr-eye-probe').read_bytes())
        payload['usr/libexec/xtos/xr-feedback-entry.py']=(0o755,
            b'#!/usr/bin/python3\nfrom pathlib import Path\nimport runpy,sys\nsys.path.insert(0,str(Path(__file__).resolve().parents[2]/"lib/xtos"))\nrunpy.run_module("xtos_xr",run_name="__main__")\n')
        payload['usr/bin/xr-feedback-demo']=(0o755,
            b'#!/bin/sh\nset -eu\nexec /usr/bin/python3 /usr/libexec/xtos/xr-feedback-entry.py "$@"\n')
        record={'schema':'xtos.xr.source-inventory.v1','status':'installed_source_only',
            'physical_acceptance':False,'vendor_runtime_bundled':False,
            'source_hashes':{n:digest(data) for n,data in inputs.items()},
            'payload_hashes':{n:digest(data) for n,(_,data) in payload.items()},
            'python_interpreter':'/usr/bin/python3','native_linkage':'host-built dynamic ELF; target closure requires separate validation',
            'authority':'No approval store initialized; demo uses temporary FixtureStore only'}
        payload['usr/share/xtos/xr-sources.json']=(0o644,
            (json.dumps(record,sort_keys=True,indent=2)+'\n').encode())
        # Validate every destination before creating target parents or publishing.
        expected_packages={'usr/lib/xtos/xtos_xr':set(PYTHON_NAMES),
            'usr/lib/xtos/xtos_action_gate':set(GATE_NAMES)}
        existing_packages=set()
        for relative,names in expected_packages.items():
            target=stage/relative
            if os.path.lexists(target):
                if target.is_symlink() or not target.is_dir() or {p.name for p in target.iterdir()}!=names:
                    raise ValueError('XR package inventory conflict: '+relative)
                existing_packages.add(relative)
        existing=set()
        for relative,(mode,data) in payload.items():
            target=stage/relative
            for ancestor in reversed(target.parents):
                if ancestor==stage or ancestor.is_relative_to(stage):
                    if os.path.lexists(ancestor) and (ancestor.is_symlink() or not ancestor.is_dir()):
                        raise ValueError('XR destination ancestor conflict')
            if os.path.lexists(target):
                if _regular_file_snapshot(target)!=(mode,data):
                    raise ValueError('XR destination bytes/mode conflict: '+relative)
                existing.add(relative)
            copied=tmp/'payload'/relative;copied.parent.mkdir(parents=True,exist_ok=True)
            with copied.open('xb') as output:output.write(data)
            copied.chmod(mode)
        for relative,data in inputs.items():
            if source_bytes(repo,relative)!=data:raise ValueError('XR source changed during install')
        # New packages publish as complete directories so a partial later
        # failure can be retried without an incomplete package inventory.
        for relative in expected_packages:
            target=stage/relative
            if relative not in existing_packages:
                target.parent.mkdir(parents=True,exist_ok=True)
                _rename_noreplace(tmp/'payload'/relative,target)
        for relative in payload:
            if relative in existing or any(relative.startswith(package+'/') for package in expected_packages):
                continue
            target=stage/relative;target.parent.mkdir(parents=True,exist_ok=True)
            _rename_noreplace(tmp/'payload'/relative,target)
        return record


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rootfs',type=Path,required=True)
    parser.add_argument('--source-root',type=Path,default=Path(__file__).resolve().parents[1])
    args=parser.parse_args()
    try:record=install(args.rootfs,args.source_root)
    except (ValueError,OSError,subprocess.SubprocessError) as exc:parser.error(str(exc))
    print(json.dumps(record,sort_keys=True))
    return 0

if __name__=='__main__':raise SystemExit(main())
