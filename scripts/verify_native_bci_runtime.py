#!/usr/bin/env python3
"""Explicit trusted-root smoke for native /usr/bin/bci; no hardware claim."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(root):
    root=Path(os.path.abspath(root))
    binary=root/'usr/bin/bci'
    if not root.is_dir() or root.is_symlink() or not binary.is_file() or binary.is_symlink():
        raise ValueError('trusted real root and regular packaged bci required')
    if not os.access(binary,os.X_OK):
        raise ValueError('packaged bci is not executable')
    if os.geteuid()!=0:
        raise ValueError('explicit chroot smoke requires root')
    temporary=Path(tempfile.mkdtemp(prefix='bci-native-smoke-',dir=root/'run'))
    before=sha(binary)
    try:
        data=b''.join(bytes([0xa0,i])+bytes(30)+bytes([0xc0]) for i in range(12))
        (temporary/'input.bin').write_bytes(data)
        inside='/'+str(temporary.relative_to(root))
        result=subprocess.run(['chroot',str(root),'/usr/bin/bci','--input',inside+'/input.bin',
            '--output',inside+'/output.jsonl'],capture_output=True,text=True,timeout=15,
            env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C','LC_ALL':'C'})
        if result.returncode or result.stderr:
            raise ValueError('native smoke failed: '+result.stderr)
        summary=json.loads(result.stdout)
        frames=[json.loads(line) for line in (temporary/'output.jsonl').read_text().splitlines()]
        if (summary.get('status')!='ok' or summary.get('processed')!=12 or summary.get('source_mode')!='fixture'
            or summary.get('physical_evidence') is not False or len(frames)!=12 or
            any(len(f.get('filtered_nv',[]))!=8 or f.get('sequence')!=i for i,f in enumerate(frames))):
            raise ValueError('invalid native smoke semantics')
        context=['--session-file',inside+'/session','--firmware-file',inside+'/firmware',
                 '--reference-id','synthetic-smoke','--source','fixture']
        (temporary/'session').write_bytes(b'synthetic offline calibration smoke\n')
        (temporary/'firmware').write_bytes(b'fixture firmware description\n')
        points=b''.join((','.join(map(str,[x]*8+[2*x+7]*8))+'\n').encode()
                        for x in (-100,0,100))
        raw=b'-10,-3,-1,0,1,3,10,100\n'
        (temporary/'pairs.csv').write_bytes(points)
        (temporary/'raw.csv').write_bytes(raw)
        source_hashes={p.name:sha(p) for p in (temporary/'session',temporary/'firmware',
                                             temporary/'pairs.csv',temporary/'raw.csv')}
        def invoke(arguments):
            return subprocess.run(['chroot',str(root),'/usr/bin/bci',*arguments],
                capture_output=True,text=True,timeout=5,
                env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C','LC_ALL':'C'})
        result=invoke(['calibrate','--points',inside+'/pairs.csv',*context,'--mask','255',
                       '--min-span-nv','1','--max-residual-nv','0','--output',inside+'/new.model'])
        if result.returncode or result.stderr or json.loads(result.stdout).get('physical_acceptance') is not False:
            raise ValueError('packaged calibration fit failed: '+result.stderr)
        model=(temporary/'new.model').read_bytes()
        prefix,digest=model.rsplit(b'sha256=',1)
        if digest!=hashlib.sha256(prefix).hexdigest().encode()+b'\n':
            raise ValueError('packaged calibration model digest mismatch')
        for field,name in [('session_sha256','session'),('firmware_sha256','firmware'),('pairs_sha256','pairs.csv')]:
            if (field+'='+source_hashes[name]+'\n').encode() not in model:
                raise ValueError('packaged calibration input hash mismatch')
        result=invoke(['apply-calibration','--model',inside+'/new.model','--input',inside+'/raw.csv',
                       *context,'--output',inside+'/corrected.csv'])
        if result.returncode or result.stderr or json.loads(result.stdout).get('frame_count')!=1:
            raise ValueError('packaged calibration apply failed: '+result.stderr)
        if (temporary/'corrected.csv').read_bytes()!=b'-13,1,5,7,9,13,27,207\n':
            raise ValueError('packaged calibration output mismatch')
        if any(sha(temporary/name)!=expected for name,expected in source_hashes.items()):
            raise ValueError('calibration smoke changed caller inputs')
        result=invoke(['config','--device','/missing-synthetic-device'])
        if result.returncode!=3 or result.stdout or 'config: open' not in result.stderr:
            raise ValueError('packaged configuration error path mismatch')
        if sha(binary)!=before:
            raise ValueError('packaged binary changed during smoke')
        return {'schema':'xtos-native-bci-runtime-v1','status':'pass','scope':'chroot_host_kernel_synthetic',
                'binary_sha256':before,'frames':12,'eight_channel_filter':True,
                'offline_calibration_fit_apply':True,'configuration_query_error_path':True,
                'calibration_model_sha256':hashlib.sha256(model).hexdigest(),'physical_evidence':False}
    finally:
        shutil.rmtree(temporary)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rootfs',type=Path,required=True)
    args=parser.parse_args()
    try:
        print(json.dumps(verify(args.rootfs)))
        return 0
    except (OSError,ValueError,subprocess.SubprocessError) as error:
        print(json.dumps({'status':'fail','error':str(error),'physical_evidence':False}))
        return 1
if __name__=='__main__':raise SystemExit(main())
