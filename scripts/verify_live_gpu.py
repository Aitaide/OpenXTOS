#!/usr/bin/env python3
"""Verify GPU quick-start paths in a file-backed XTOS QEMU guest, not physical acceleration."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time

import verify_live as V


def clean(text):
    return re.sub(r'XTOS Bate \\r\s+\\n\s+\\l\s*', '', text).strip()


def python_command(source):
    return 'python3 -c ' + shlex.quote(source)


def validate_tune_output(text):
    summary = re.search(r'^RT_TUNE housekeeping_cpus=0 verified=(\d+) changes=\d+ skipped=(\d+) failed=(\d+)$',
                        text, re.MULTILINE)
    status = re.search(r'^IRQ_TUNE_RC=(\d+)$', text, re.MULTILINE)
    if summary is None or status is None:
        raise RuntimeError('IRQ tuning omitted summary or exit status')
    verified, skipped, failed = map(int, summary.groups())
    readbacks = re.findall(r'^RT_TUNE irq \d+ requested="([^"]+)" effective="([^"]+)"$',
                           text, re.MULTILINE)
    if verified < 1 or len(readbacks) != verified:
        raise RuntimeError('IRQ tuning has no independently countable verified readbacks')
    if any(requested != '0' or effective != '0' for requested, effective in readbacks):
        raise RuntimeError('IRQ tuning claims success outside selected housekeeping CPUs')
    if int(status.group(1)) != (1 if failed else 0):
        raise RuntimeError('IRQ tuning exit status contradicts failed count')
    return {'verified': verified, 'failed': failed, 'skipped': skipped,
            'partial': bool(failed or skipped)}


def validate_gpu_irq_readback(before, after, tune_output):
    def irq_map(rows):
        return {irq['irq']: irq for row in rows for irq in row.get('irqs', [])}
    old = irq_map(before)
    current = irq_map(after)
    verified, unmoved, unavailable = [], [], []
    for number, initial in sorted(old.items()):
        new = current.get(number)
        if new is None:
            raise RuntimeError(f'GPU IRQ {number} disappeared from tuning readback')
        requested = new['requested_affinity']
        effective = new['effective_affinity']
        if requested == 'unknown' or effective == 'unknown':
            if initial['requested_affinity'] != 'unknown' and initial['effective_affinity'] != 'unknown':
                raise RuntimeError(f'GPU IRQ {number} lost affinity visibility after tuning')
            # A PCI legacy irq value can name an unregistered vector when the
            # device uses MSI-X instead. Retain it as unavailable; rt enumerates
            # /proc/irq and therefore cannot emit a row for an absent vector.
            unavailable.append(number)
        elif requested == '0' and effective == '0':
            if f'RT_TUNE irq {number} requested="0" effective="0"' not in tune_output:
                raise RuntimeError(f'GPU IRQ {number} lacks verified tuning readback')
            verified.append(number)
        else:
            if f'RT_TUNE irq {number} failed:' not in tune_output:
                raise RuntimeError(f'GPU IRQ {number} remained outside housekeeping without a failure')
            unmoved.append(number)
    if not verified:
        raise RuntimeError('No actual GPU interrupt was verified on housekeeping CPU 0')
    return {'verified_irqs': verified, 'unmoved_irqs': unmoved,
            'unavailable_irqs': unavailable}


def validate_rt_budget_readback(state):
    if state.get('rt_runtime_us') != -1:
        raise RuntimeError('RT budget readback did not confirm throttling disabled')


def verify(candidate, key, evidence, timeout):
    if evidence.exists():
        raise ValueError('use a fresh evidence directory')
    if not (candidate / 'images/xtos-developer-live-x86_64.iso').is_file():
        raise ValueError('candidate ISO missing')
    if not key.is_file():
        raise ValueError('verification private key missing')
    evidence.mkdir(parents=True)
    result: dict = {
        'status': 'fail',
        'iso_sha256': V.sha(candidate / 'images/xtos-developer-live-x86_64.iso'),
        'scope': 'QEMU virtio-gpu DRM, permissions, API software fallback and IRQ configuration only',
        'physical_gpu_acceleration': 'unverified',
        'physical_gpu_rt_latency': 'unverified',
    }
    guest = None
    try:
        guest = V.Guest(candidate, evidence, key, 'bios-cd', timeout,
                        smp=4, extra_qemu=[['-device', 'virtio-vga']])

        def ssh(user, command, command_timeout=180):
            # Cold TCG key exchange can briefly starve while a module initializes.
            last = None
            for attempt in range(3):
                try:
                    assert guest is not None
                    response = guest.ssh(user, command, timeout=command_timeout)
                    return clean(response.stdout)
                except RuntimeError as error:
                    last = error
                    if 'SSH command failed 255' not in str(error):
                        raise
                    time.sleep(3 * (attempt + 1))
            raise RuntimeError(str(last))

        result['identity'] = ssh('xtos', 'id; /usr/bin/xtos-rt-status')
        access_source = '''import json,os,stat
nodes=[]
for name,gid in [('card0',44),('renderD128',1001)]:
 p='/dev/dri/'+name
 s=os.stat(p)
 assert stat.S_ISCHR(s.st_mode),p
 assert stat.S_IMODE(s.st_mode)==0o660,(p,oct(s.st_mode))
 assert s.st_uid==0 and s.st_gid==gid,(p,s.st_uid,s.st_gid)
 fd=os.open(p,os.O_RDWR|os.O_CLOEXEC);os.close(fd)
 nodes.append({'node':p,'mode':oct(stat.S_IMODE(s.st_mode)),'uid':s.st_uid,'gid':s.st_gid,'opened':True})
assert os.geteuid()==1000
assert {20,44,1001} <= set(os.getgroups()),os.getgroups()
print(json.dumps({'uid':os.geteuid(),'groups':os.getgroups(),'nodes':nodes}))'''
        result['developer_access'] = json.loads(ssh('xtos', python_command(access_source)))
        result['hw_gpu'] = json.loads(ssh('xtos', 'hw --json gpu'))
        nodes = {row['node']: row for row in result['hw_gpu']}
        for node in ('/dev/dri/card0', '/dev/dri/renderD128'):
            row = nodes[node]
            if not row['character_device'] or not row['openable'] or row['drm_driver'] != 'virtio_gpu':
                raise RuntimeError('virtio DRM ioctl or developer access failed: '+repr(row))
        result['hw_gpu_text'] = ssh('xtos', 'hw gpu')

        # Exercise the already running uevent handler. This is an explicit add
        # uevent on a virtual device, NOT a physical unplug/replug claim.
        reset_source = '''import os
for name in ['card0','renderD128']:
 p='/dev/dri/'+name
 os.chown(p,0,0);os.chmod(p,0o600)
 with open('/sys/class/drm/'+name+'/uevent','w') as f:f.write('add')
print('VIRTUAL_DRM_ADD_UEVENT=sent')'''
        result['uevent_trigger'] = ssh('root', python_command(reset_source))
        settle_source = '''import os,stat,time
for attempt in range(100):
 states=[os.stat('/dev/dri/'+name) for name in ['card0','renderD128']]
 if all(stat.S_IMODE(s.st_mode)==0o660 and s.st_gid==gid for s,gid in zip(states,[44,1001])):break
 time.sleep(.1)
else:raise SystemExit('DRM permission event handler did not settle')
print('VIRTUAL_DRM_UEVENT_PERMISSIONS=PASS')'''
        result['uevent_readback'] = ssh('root', python_command(settle_source))
        result['developer_after_uevent'] = json.loads(ssh('xtos', python_command(access_source)))

        # Verify supplementary groups on the actual local console login too.
        assert guest.sock is not None
        guest.sock.sendall(b"printf '\\nCONSOLE_GPU_GROUPS='; id -G\n")
        guest.wait_for(rb'CONSOLE_GPU_GROUPS=([0-9 ]+)[\r\n]')
        match = re.search(rb'CONSOLE_GPU_GROUPS=([0-9 ]+)[\r\n]', guest.data)
        if match is None:
            raise RuntimeError('console group marker missing')
        groups = {int(x) for x in match.group(1).split()}
        if not {20,44,1001} <= groups:
            raise RuntimeError('console lost supplementary device groups: '+repr(groups))
        result['console_groups'] = sorted(groups)

        result['package_integrity'] = ssh('root',
            'set -e; for p in mesa-vulkan-drivers mesa-opencl-icd vulkan-tools clinfo; do pkg verify "$p"; done', 300)
        result['modules'] = ssh('root',
            'set -e; modprobe amdgpu; modprobe nouveau; test -d /sys/module/i915; test -d /sys/module/virtio_gpu; lsmod | grep -E "^(amdgpu|nouveau)"', 240)
        result['firmware'] = ssh('root',
            'set -e; for d in amdgpu nvidia i915; do test -d /usr/lib/firmware/$d; printf "%s=" "$d"; find /usr/lib/firmware/$d -type f | wc -l; done')

        # Force lavapipe to prove a functioning ICD loader while explicitly
        # keeping software Vulkan separate from hardware GPU acceleration.
        result['vulkan_software_probe'] = ssh('xtos',
            'set -e; mkdir -p /home/xtos/.cache/vulkan-runtime; chmod 700 /home/xtos/.cache/vulkan-runtime; '
            'XDG_RUNTIME_DIR=/home/xtos/.cache/vulkan-runtime '
            'VK_DRIVER_FILES=/usr/share/vulkan/icd.d/lvp_icd.json LP_NUM_THREADS=1 vulkaninfo --summary', 300)
        if not any(name in result['vulkan_software_probe'].lower() for name in ('llvmpipe','lavapipe')):
            raise RuntimeError('software Vulkan probe did not identify its software device')
        result['opencl_probe'] = ssh('xtos', 'clinfo --list', 180)
        result['opencl_hardware_device'] = 'not established by QEMU probe'

        before = ssh('root', 'cat /proc/sys/kernel/sched_rt_runtime_us')
        rejected = ssh('root',
            'set +e; rt tune --housekeeping 999999 > /run/gpu-rt-reject.log 2>&1; rc=$?; cat /run/gpu-rt-reject.log; test "$rc" -ne 0 || exit 1; echo INVALID_CPU_REFUSED=PASS')
        after = ssh('root', 'cat /proc/sys/kernel/sched_rt_runtime_us')
        if before != after:
            raise RuntimeError('invalid housekeeping changed RT budget')
        result['invalid_irq_selection'] = rejected
        result['irq_tune'] = ssh('root',
            'set +e; rt tune --housekeeping 0 > /run/gpu-rt-tune.log 2>&1; rc=$?; cat /run/gpu-rt-tune.log; echo IRQ_TUNE_RC=$rc')
        result['irq_tune_validation'] = validate_tune_output(result['irq_tune'])
        result['gpu_irq_after'] = json.loads(ssh('xtos', 'hw --json gpu'))
        result['gpu_irq_validation'] = validate_gpu_irq_readback(
            result['hw_gpu'], result['gpu_irq_after'], result['irq_tune'])
        result['rt_after'] = json.loads(ssh('root', 'rt --json status'))
        validate_rt_budget_readback(result['rt_after'])
        serial = guest.data.decode(errors='replace')
        for marker in ('XTOS_DEVICES manager=mdev hotplug=netlink', 'XTOS_MODULES '):
            if marker not in serial:
                raise RuntimeError('boot marker missing: '+marker)
        result['autoload_markers'] = [line.strip() for line in serial.splitlines()
                                      if line.startswith(('XTOS_DEVICES ', 'XTOS_MODULES '))]
        guest.poweroff()
        guest = None
        result['sentinel_unchanged'] = True
        result['status'] = 'pass'
    except Exception as error:
        result['error'] = str(error)
        raise
    finally:
        if guest is not None:
            try:
                guest.poweroff()
                result['sentinel_unchanged'] = True
            except Exception as error:
                result['shutdown_error'] = str(error)
                guest.close()
        (evidence / 'gpu-live.json').write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', required=True, type=Path)
    parser.add_argument('--key', required=True, type=Path)
    parser.add_argument('--evidence', required=True, type=Path)
    parser.add_argument('--timeout', default=600, type=int)
    args = parser.parse_args()
    try:
        result = verify(args.candidate.absolute(), args.key.absolute(), args.evidence.absolute(), args.timeout)
    except (ValueError, OSError, RuntimeError, KeyError) as error:
        parser.exit(1, 'GPU_LIVE=FAIL '+str(error)+'\n')
    print('GPU_LIVE=PASS '+str(args.evidence.absolute() / 'gpu-live.json'))
    print(json.dumps({k:result[k] for k in ('status','iso_sha256','sentinel_unchanged','physical_gpu_acceleration','physical_gpu_rt_latency')},sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
