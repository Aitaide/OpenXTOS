"""Observe a selected child runtime; synthetic profiles are labelled separately."""
from copy import deepcopy
from functools import lru_cache
import json
import subprocess

CPU = 'CPUExecutionProvider'
RUNTIME_PROBE = '''from pathlib import Path
import hashlib,json,os,sys
import onnxruntime as ort
p=Path(ort.__file__).absolute().parent
files={}
for q in sorted((p/'capi').iterdir()):
 if q.is_file() and not q.is_symlink() and (q.name.startswith('onnxruntime_pybind11_state') or q.name=='libonnxruntime_providers_shared.so') and '.so' in q.name:
  with q.open('rb') as f:files[str(q.relative_to(p))]=hashlib.file_digest(f,'sha256').hexdigest()
print(json.dumps({'version':ort.__version__,'abi':sys.implementation.cache_tag,'files':files,'cpus':sorted(os.sched_getaffinity(0)),'providers':ort.get_available_providers()}))
'''


@lru_cache(maxsize=4)
def _observe(python):
    result = subprocess.run([python, '-B', '-c', RUNTIME_PROBE],
                            capture_output=True, text=True, timeout=60)
    if result.returncode or result.stderr:
        raise RuntimeError('selected runtime probe failed: '+result.stderr[-2000:])
    record = json.loads(result.stdout)
    if not record['files'] or not record['cpus'] or CPU not in record['providers']:
        raise RuntimeError('selected runtime probe is incomplete')
    return record


def observe_runtime(python):
    # Callers must not mutate the cached observation or share vendor placeholders.
    return deepcopy(_observe(python))


def execution_profile(record, provider=CPU):
    files = dict(record['files'])
    if provider != CPU:
        if provider not in ('CUDAExecutionProvider', 'MIGraphXExecutionProvider'):
            raise ValueError('unknown test provider')
        suffix = 'cuda' if provider == 'CUDAExecutionProvider' else 'migraphx'
        # This is deliberately not a vendor artifact identity. It is used only
        # for structural/API fixtures or unavailable-provider-before-hash refusal.
        files[f'capi/libonnxruntime_providers_{suffix}.so'] = '0'*64
    return {'onnxruntime_version': record['version'], 'python_abi': record['abi'],
            'runtime_files': files, 'cpu_affinity': [record['cpus'][0]],
            'scheduler': 'SCHED_OTHER', 'device_id': 0,
            'memory_limit_bytes': 0 if provider == CPU else 1048576}


def synthetic_profile(provider=CPU):
    """Pure structural/API mock input; no claim of installed runtime identity."""
    return execution_profile({'version': '1.23.2', 'abi': 'cpython-311',
        'files': {'capi/onnxruntime_pybind11_state.fixture.so': 'a'*64}, 'cpus': [0]}, provider)
