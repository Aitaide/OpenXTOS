#!/usr/bin/env python3
"""Run maintained validation on the declared release patch in an isolated clone."""
from pathlib import Path
import os
import subprocess
import sys
from preserve_tracked import preserved

TOOLS = Path(__file__).resolve().parent
source = Path(sys.argv[1]).resolve()
target = Path(sys.argv[2]).resolve()

def run(args, **kwargs):
    subprocess.run([str(x) for x in args], check=True, **kwargs)

run(['git', 'clone', '--shared', '--no-hardlinks', str(source), str(target)])
run(['git', '-C', target, 'config', 'user.name', 'OpenXTOS release validation'])
run(['git', '-C', target, 'config', 'user.email', 'release-validation@example.invalid'])
run(['patch', '--batch', '--fuzz=0', '-d', target, '-p1', '-i', TOOLS / 'cyton-read-bounds.patch'])
# Validation-only compatibility: the added branch is excluded without TEST_MODE.
run(['patch', '--batch', '--fuzz=0', '-d', target, '-p1', '-i', TOOLS / 'cgroup-fixture-write.patch'])
run(['patch', '--batch', '--fuzz=0', '-d', target, '-p1', '-i', TOOLS / 'runner-fixture-write.patch'])
run(['patch', '--batch', '--fuzz=0', '-d', target, '-p1', '-i', TOOLS / 'update-fixture-fortify.patch'])
# This nonblocking diagnostic repair is validated here but is not shipped by the
# retained core images built by the independently pinned original recipe.
run(['patch', '--batch', '--fuzz=0', '-d', target, '-p1', '-i', TOOLS / 'xr-diagnostic-write.patch'])
run(['python3', target / 'tools/xtos-sbom/gen_sbom.py'], cwd=target)
run(['git', '-C', target, 'add', 'tools/xtos-cyton/cyton_diagnostic_serial.c', 'tools/xtos-native/cgroup/cgroup.c', 'tools/xtos-xr/src/xr_eye_probe.c', 'tests/test_native_tools_runner.py', 'tests/test_update_apply_security.py', 'tests/test_update_recovery.py', 'docs/sbom.json'])
run(['git', '-C', target, 'commit', '-m', 'Apply disclosed release bounds patch and validation-only compiler fixture repairs'])
with preserved(target):
    run(['bash', target / 'scripts/build_mbedtls.sh'], cwd=target)
    run(['bash', target / 'scripts/test.sh'], cwd=target)
status = subprocess.check_output(['git', '-C', target, 'status', '--porcelain', '--untracked-files=all'], text=True)
if status:
    raise RuntimeError('release validation changed source: ' + status)
print('release_maintained_tests=pass')
