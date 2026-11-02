#!/usr/bin/env python3
"""Validate a private Live candidate with QEMU file-backed CD/USB only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
import socket
import subprocess
import time
import shlex

MODES=('bios-cd','uefi-cd','bios-usb','uefi-usb')
info_version_marker='1.0.1'


def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


class Guest:
    def __init__(self,candidate,evidence,key,mode,timeout,extra_drives=(),smp=2,extra_qemu=()):
        self.e=evidence;self.mode=mode;self.timeout=timeout;self.key=key;smp=int(smp)
        self.data=bytearray();self.process=None;self.sock=None
        self.log=(evidence/(mode+'.serial.log')).open('wb')
        self.err=(evidence/(mode+'.qemu.log')).open('wb')
        path=evidence/(mode+'.sock')
        if path.exists():raise ValueError('serial socket exists')
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0));self.port=listener.getsockname()[1]
        self.disk=evidence/(mode+'-sentinel.raw')
        with self.disk.open('xb') as disk:
            disk.write(b'XTOS_INTERNAL_DISK_MUST_NOT_BE_WRITTEN\n');disk.truncate(32*1024*1024)
            disk.seek(32*1024*1024-32);disk.write(b'XTOS_LAST_BLOCK_DO_NOT_OVERWRITE!')
        self.before=sha(self.disk)
        args=['qemu-system-x86_64','-machine','q35','-accel','tcg,thread=multi','-cpu','max','-m','3072','-smp',str(smp),'-display','none','-monitor','none','-no-reboot',
              '-serial',f'unix:{path},server=on,wait=off','-drive',f'file={self.disk},format=raw,if=none,id=sentinel','-device','nvme,drive=sentinel,serial=XTOS-LIVE-INTERNAL-TEST',
              '-netdev',f'user,id=net,hostfwd=tcp:127.0.0.1:{self.port}-:22','-device','virtio-net-pci,netdev=net']
        # optional extra drives: each entry is a list of raw qemu arguments
        for spec in extra_drives:args+=list(spec)
        # optional extra qemu devices: same shape, appended after the base machine
        for spec in extra_qemu:args+=list(spec)
        iso=candidate/'images/xtos-developer-live-x86_64.iso'
        if mode.endswith('-cd'):args+=['-cdrom',str(iso),'-boot','d']
        else:args+=['-device','qemu-xhci,id=xhci','-drive',f'file={iso},format=raw,if=none,id=usb,readonly=on','-device','usb-storage,drive=usb,removable=on,bootindex=1']
        if mode.startswith('uefi'):
            variables=evidence/(mode+'.vars.fd');shutil.copy2('/usr/share/OVMF/OVMF_VARS_4M.fd',variables)
            args+=['-drive','if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd','-drive',f'if=pflash,format=raw,file={variables}']
        try:
            self.process=subprocess.Popen(args,stdout=self.err,stderr=self.err)
            self.sock=socket.socket(socket.AF_UNIX);deadline=time.monotonic()+20
            while True:
                if self.process.poll() is not None:raise RuntimeError('QEMU exited before console')
                try:self.sock.connect(str(path));break
                except (FileNotFoundError,ConnectionRefusedError):
                    if time.monotonic()>deadline:raise RuntimeError('serial unavailable')
                    time.sleep(.1)
            self.wait_for(rb'XTOS_READY mode=live[^\r\n]*[\r\n]')
            self.wait_for(rb'XTOS_DHCP interface=[^ ]+ address=[^\r\n]*[\r\n]')
            matches=re.findall(rb'ssh-ed25519 ([A-Za-z0-9+/=]+)',self.data)
            if not matches:raise RuntimeError('trusted serial did not advertise SSH public key')
            self.known=evidence/(mode+'.known_hosts')
            self.known.write_text(f'[127.0.0.1]:{self.port} ssh-ed25519 '+matches[0].decode()+'\n')
        except BaseException:
            self.close();raise

    def wait_for(self,pattern):
        deadline=time.monotonic()+self.timeout
        while not re.search(pattern,self.data):
            if b'XTOS_LIVE_ERROR' in self.data or b'Kernel panic' in self.data:
                raise RuntimeError(self.data[-3000:].decode(errors='replace'))
            if self.process.poll() is not None:raise RuntimeError('guest stopped before marker')
            if time.monotonic()>deadline:raise RuntimeError('guest marker timeout; '+str(self.log.name))
            if select.select([self.sock],[],[],.5)[0]:
                chunk=self.sock.recv(65536)
                if not chunk:raise RuntimeError('guest serial disconnected')
                self.log.write(chunk);self.log.flush();self.data.extend(chunk)

    def has_ever_matched(self,pattern):
        return re.search(pattern,self.data) is not None

    def ssh(self,user,command,timeout=90,extra=(),expect=0):
        args=['ssh','-F','/dev/null','-p',str(self.port),'-i',str(self.key),'-o','BatchMode=yes','-o','IdentitiesOnly=yes','-o','IdentityAgent=none','-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile='+str(self.known),'-o','ConnectTimeout=5','-o','ServerAliveInterval=10','-o','ServerAliveCountMax=2',*extra,user+'@127.0.0.1',command]
        deadline=time.monotonic()+30
        while True:
            result=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
            if result.returncode==expect:break
            if 'Connection refused' not in result.stderr or time.monotonic()>deadline:break
            time.sleep(1)
        if result.returncode!=expect:raise RuntimeError('SSH command failed '+str(result.returncode)+'\n'+result.stdout+result.stderr)
        return result

    def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait()
        if self.sock:self.sock.close()
        self.log.close();self.err.close()

    def poweroff(self):
        self.ssh('root','sync; /usr/libexec/xtos/busybox poweroff -f',expect=255)
        self.process.wait(timeout=30)
        self.close()
        if sha(self.disk)!=self.before:raise RuntimeError('internal file disk changed')


def validate_xr_guest_report(report):
    """Accept installed software smoke only, not runtime_unverified device data."""
    if not isinstance(report, dict) or report.get('authority_store_created') is not False:
        raise ValueError('XR guest authority boundary mismatch')
    python = report.get('python')
    if (not isinstance(python, dict) or python.get('executable') != '/usr/bin/python3'
            or python.get('implementation') != 'cpython'):
        raise ValueError('XR guest did not execute the target interpreter')
    commands = report.get('commands')
    if not isinstance(commands, dict) or set(commands) != {'native_missing_loader', 'eye_missing_loader', 'launcher_argv', 'demo'}:
        raise ValueError('XR guest command inventory mismatch')
    expected = {
        'native_missing_loader': (['/usr/bin/xr-probe', '--loader', '/__xtos_live_xr_missing_loader__.so'], 3),
        'eye_missing_loader': (['/usr/bin/xr-eye-probe', '--loader', '/__xtos_live_xr_missing_loader__.so', '--accept-eye-data'], 1),
        'launcher_argv': (['/usr/bin/xr-feedback-demo', '--not-a-demo'], 2),
        'demo': (['/usr/bin/xr-feedback-demo', '--demo'], 0),
    }
    for name, (argv, exit_code) in expected.items():
        row = commands[name]
        if (not isinstance(row, dict) or row.get('argv') != argv
                or type(row.get('exit')) is not int or row['exit'] != exit_code
                or type(row.get('stdout')) is not str or type(row.get('stderr')) is not str
                or len(row['stdout']) + len(row['stderr']) > 16384):
            raise ValueError('XR guest execution mismatch: ' + name)
    if 'usage:' not in commands['launcher_argv']['stderr']:
        raise ValueError('XR guest launcher did not forward argv')
    native, demo = (json.loads(commands[name]['stdout']) for name in ('native_missing_loader', 'demo'))
    if (commands['native_missing_loader']['stderr'] or commands['demo']['stderr']
            or not isinstance(native, dict) or native.get('physical_acceptance') is not False
            or native != {'status': 'unavailable', 'safe_action': 'no_action', 'physical_acceptance': False}
            or not isinstance(demo, dict) or demo.get('safe_action') != 'no_action'
            or demo.get('physical_acceptance') is not False
            or demo.get('runtime_mode') != 'simulated'):
        raise ValueError('XR guest crossed the unavailable/no-action boundary')
    eye = json.loads(commands['eye_missing_loader']['stdout'])
    if (commands['eye_missing_loader']['stderr'] or not isinstance(eye, dict)
            or eye.get('status') != 'runtime_unavailable'
            or eye.get('source_kind') != 'runtime_unverified'
            or eye.get('safe_action') != 'no_action' or eye.get('physical_acceptance') is not False
            or type(eye.get('sample_count')) is not int or eye['sample_count'] != 0
            or eye.get('samples') != []):
        raise ValueError('XR guest eye sampler refusal mismatch')
    return report


def validate_xr_installed_inventory(report, build):
    """Bind actual ordinary-user file readbacks to the installed-build receipt."""
    if (not isinstance(report, dict) or not isinstance(build, dict)
            or build.get('schema') != 'xtos.xr.live-install.v1'
            or report.get('physical_acceptance') is not False
            or build.get('physical_acceptance') is not False):
        raise ValueError('XR installed inventory boundary mismatch')
    for inventory in (report.get('payload_inventory'), build.get('payload_inventory')):
        if not isinstance(inventory, dict) or not 1 <= len(inventory) <= 65:
            raise ValueError('XR installed payload inventory missing')
        for name, row in inventory.items():
            if (type(name) is not str or not isinstance(row, dict)
                    or set(row) != {'sha256', 'bytes', 'mode'}
                    or type(row['sha256']) is not str or re.fullmatch('[a-f0-9]{64}', row['sha256']) is None
                    or type(row['bytes']) is not int or not 0 <= row['bytes'] <= 4 * 1024 * 1024
                    or type(row['mode']) is not int or row['mode'] not in (0o644, 0o755)):
                raise ValueError('XR installed payload binding is malformed')
    if report['payload_inventory'] != build['payload_inventory']:
        raise ValueError('XR installed payload differs from build receipt')
    sources = build.get('source_inventory')
    observed = report.get('source_hashes')
    if not isinstance(sources, dict) or not sources or not isinstance(observed, dict) or not observed:
        raise ValueError('XR installed source binding is missing')
    for name, row in sources.items():
        if (type(name) is not str or not isinstance(row, dict) or set(row) != {'sha256', 'bytes'}
                or type(row['sha256']) is not str or re.fullmatch('[a-f0-9]{64}', row['sha256']) is None
                or type(row['bytes']) is not int or row['bytes'] < 0):
            raise ValueError('XR installed source binding is malformed')
    if observed != {name: row['sha256'] for name, row in sources.items()}:
        raise ValueError('XR installed source differs from build receipt')
    return report


def validate_mode(candidate,evidence,key,mode,timeout):
    g=Guest(candidate,evidence,key,mode,timeout)
    rows={}
    try:
        command='set -e; uname -r; /usr/bin/xtos-rt-status; cat /etc/os-release; test ! -e /usr/sbin/xtos-install; ! grep -qw xtos.installer=1 /proc/cmdline; grep " / overlay " /proc/mounts; grep " /run/live-media " /proc/mounts; grep " /run/live-rw tmpfs " /proc/mounts; test "$(id -u)" = 0; test "$(/usr/bin/xtos-rt-status)" = 1; '+('test -d /sys/firmware/efi;' if mode.startswith('uefi') else 'test ! -d /sys/firmware/efi;')+' echo LIVE_MOUNTS=PASS'
        rows['root_mounts']=g.ssh('root',command).stdout
        r=g.ssh('root','/usr/bin/xtos-selftest',timeout=180)
        if 'guest_userspace=pass' not in r.stdout:raise RuntimeError('SDK selftest missing final marker')
        rows['sdk_selftest']=r.stdout
        r=g.ssh('xtos','set -e; test "$(id -u)" = 1000; test "$HOME" = /home/xtos; echo developer-write >/home/xtos/live-write; test "$(cat /home/xtos/live-write)" = developer-write; python3 -m venv /home/xtos/venv; /home/xtos/venv/bin/python -m pip check; python3 -c \'import ssl,sqlite3,ctypes,lzma; print("DEVELOPER_PYTHON=PASS")\'; go version; /usr/bin/clock status --json; echo DEVELOPER_USER=PASS',timeout=240)
        rows['developer_tools']=r.stdout
        if 'DEVELOPER_USER=PASS' not in r.stdout:raise RuntimeError('developer user marker missing')
        denied=g.ssh('root','true',extra=['-v','-o','PubkeyAuthentication=no','-o','PreferredAuthentications=password,keyboard-interactive'],expect=255)
        offers=[line.split('Authentications that can continue:',1)[1].strip() for line in denied.stderr.splitlines() if 'Authentications that can continue:' in line]
        if not offers or any(o!='publickey' for o in offers):raise RuntimeError('server offered authentication other than publickey')
        rows['authentication_offers']=offers
        clock=json.loads(g.ssh('xtos','clock status --json').stdout)
        if clock['synchronized'] and clock.get('source')!='chronyd-resident':raise RuntimeError('clock claims synchronization without resident chronyd tracking')
        rows['clock_status']=clock
        rows['host_key_trusted_via_serial']=True
        # Prove medium is read-only and default write state is RAM.
        r=g.ssh('root','set -e; if touch /run/live-media/LIVE_MUST_BE_READONLY 2>/run/readonly-error; then exit 1; fi; test -s /run/readonly-error; echo LIVE_READONLY=PASS')
        rows['readonly_media']=r.stdout
        xr_script = (Path(__file__).resolve().parents[1] / 'tests/fixtures/live_xr_guest_check.py').read_text()
        xr_result = g.ssh('xtos', '/usr/bin/python3 -I -B -c ' + shlex.quote(xr_script), timeout=90)
        rows['xr_in_guest'] = validate_xr_guest_report(json.loads(xr_result.stdout))
        xr_inventory_script = (Path(__file__).resolve().parents[1] / 'tests/fixtures/live_xr_inventory.py').read_text()
        xr_inventory_result = g.ssh('xtos', '/usr/bin/python3 -I -B -c ' + shlex.quote(xr_inventory_script), timeout=90)
        build_manifest = json.loads((candidate / 'images/build-manifest.json').read_text())
        rows['xr_installed_inventory'] = validate_xr_installed_inventory(
            json.loads(xr_inventory_result.stdout), build_manifest.get('xr_runtime'))
        if mode=='bios-cd':
            # Interactive console checks: arrow-key history + XTOS branding on ttyS0.
            # The console prompt may have appeared before earlier SSH matches consumed it.
            if not g.has_ever_matched(rb'xtos@xtos-live'):
                g.wait_for(rb'xtos@xtos-live')
            g.sock.sendall(b'echo CONSOLE_HISTORY_PROBE\n')
            g.wait_for(rb'CONSOLE_HISTORY_PROBE')
            g.sock.sendall(b'\x1b[A')  # up arrow recalls the last command
            g.wait_for(rb'CONSOLE_HISTORY_PROBE')
            rows['serial_console_arrow_history']=True
            # XTOS-native tool check: xtos-info must show version+realtime+interface.
            info=g.ssh('xtos','info').stdout
            for marker in [info_version_marker, 'PREEMPT_RT active', 'eth0']:
                if marker not in info:
                    raise RuntimeError('xtos-info missing '+marker+' in:\n'+info)
            rows['info_native_tool']=info
            j=g.ssh('xtos','info --json').stdout
            jdata=json.loads(j)
            if not jdata.get('realtime_active'):
                raise RuntimeError('info --json realtime_active should be true')
            if jdata.get('storage') != 'Live overlay (RAM), persistence=0':
                raise RuntimeError('default RAM-only storage status incorrect: '+repr(jdata.get('storage')))
            rows['info_json_keys']=sorted(jdata.keys())
            # XTOS-native network tool: net show must list eth0.
            net=g.ssh('xtos','net show').stdout
            if 'eth0' not in net:
                raise RuntimeError('xtos-net missing eth0 in:\n'+net)
            rows['net_native_tool']=net
            # XTOS-native hardware tool: xtos-hw must list PCI and block devices.
            hw=g.ssh('xtos','hw summary').stdout
            for marker in ['Block devices:','Network:','Kernel:']:
                if marker not in hw:
                    raise RuntimeError('hw summary missing '+marker+' in:\n'+hw)
            pcilist=g.ssh('xtos','hw pci').stdout
            if 'PCI_ADDRESS' not in pcilist or 'network' not in pcilist:
                raise RuntimeError('hw pci missing expected columns:\n'+pcilist)
            rows['hw_native_tool']=hw
            rows['hw_pci_sample']=pcilist[:500]
            # XTOS-native package tool: pkg summary must show lock identity.
            pkgs=g.ssh('xtos','pkg summary').stdout
            for marker in ['XTOS Package Summary','Packages:','Lock SHA256:']:
                if marker not in pkgs:
                    raise RuntimeError('pkg summary missing '+marker+' in:\n'+pkgs)
            rows['pkg_native_tool']=pkgs
            # XTOS-native firewall tool: test status + enable in the 6.6 kernel guest.
            fwstatus=g.ssh('root','fw status').stdout
            if 'XTOS_FW_STATUS' not in fwstatus:
                raise RuntimeError('fw status missing marker:\n'+fwstatus)
            rows['fw_status']=fwstatus
            # Enable: creates table + chains via two netlink batches.
            fwenable=g.ssh('root','fw enable',timeout=30).stdout
            if 'XTOS_FW_ENABLE' not in fwenable:
                raise RuntimeError('fw enable failed:\n'+fwenable)
            fwstatus2=g.ssh('root','fw status').stdout
            if 'enabled=true' not in fwstatus2:
                raise RuntimeError('fw status after enable:\n'+fwstatus2)
            # Disable: removes only the xtos_fw table.
            fwdisable=g.ssh('root','fw disable --confirm',timeout=30).stdout
            if 'XTOS_FW_DISABLE' not in fwdisable:
                raise RuntimeError('fw disable failed:\n'+fwdisable)
            fwstatus3=g.ssh('root','fw status').stdout
            if 'enabled=false' not in fwstatus3:
                raise RuntimeError('fw status after disable:\n'+fwstatus3)
            rows['fw_full_cycle']='pass'
            rows['fw_enable']=fwenable
            rows['fw_disable']=fwdisable
            # liblsl (Lab Streaming Layer) shipped in the image: compile and run
            # an outlet->resolve->inlet localhost round-trip inside the guest
            # using the image's own headers and library.
            lsl_cmd=('set -e; '
                     'test -f /usr/include/lsl_cpp.h; '
                     'ls /usr/lib/x86_64-linux-gnu/liblsl.so.2 >/dev/null; '
                     'printf "[lab]\\nKnownPeers={127.0.0.1}\\n" > /root/lsl_api.cfg; '
                     'g++ -O2 -I /usr/include /usr/share/xtos/lsl-pair.cpp -o /root/lsl-pair -L /usr/lib/x86_64-linux-gnu -llsl; '
                     'LSLAPICFG=/root/lsl_api.cfg LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu /root/lsl-pair 2>/root/lsl-stderr.log; '
                     'echo GUEST_LSL=PASS')
            r=g.ssh('root',lsl_cmd,timeout=300)
            if 'GUEST_LSL=PASS' not in r.stdout or 'GUEST_LSL_PAIR=PASS' not in r.stdout:
                raise RuntimeError('in-guest LSL round-trip failed:\n'+r.stdout+r.stderr[-500:])
            rows['lsl_in_guest']=r.stdout.strip().splitlines()[-2:]
            # events tool: deterministic AR/VR event-source simulation.
            r=g.ssh('root','events --seed 7 --count 20 --interval-us 0 | wc -l; '
                          'events --seed 7 --count 20 --interval-us 0 > /root/ev1.jsonl; '
                          'events --seed 7 --count 20 --interval-us 0 > /root/ev2.jsonl; '
                          'cmp /root/ev1.jsonl /root/ev2.jsonl; '
                          'grep -c \'"src":"sim"\' /root/ev1.jsonl; '
                          'head -1 /root/ev1.jsonl; echo GUEST_EVENTS=PASS',timeout=120)
            out=r.stdout
            if 'GUEST_EVENTS=PASS' not in out:
                raise RuntimeError('events tool failed in guest:\n'+out+r.stderr[-300:])
            lines=[l for l in out.splitlines() if l.strip().isdigit()]
            if not lines or int(lines[0])!=20:
                raise RuntimeError('events line count wrong: '+repr(lines[:2]))
            if not lines[1:] or int(lines[1])!=20:
                raise RuntimeError('events sim-src count wrong: '+repr(lines[:3]))
            rows['events_in_guest']=out.strip().splitlines()[-2:]
            text=g.data.decode(errors='replace')
            if 'XTOS Bate' not in text and 'xtos' not in text:
                raise RuntimeError('XTOS branding missing from console')
            rows['xtos_branding_visible']=True
        g.poweroff()
        rows['internal_disk_sha256_unchanged']=True
        report={'mode':mode,'status':'pass','checks':rows,'scope':'QEMU file-backed ISO and sentinel disk only; no hardware deployment','ssh_host_forward':'127.0.0.1:'+str(g.port)}
        (evidence/(mode+'.result.json')).write_text(json.dumps(report,indent=2,sort_keys=True)+'\n')
        print('LIVE_MODE_PASS='+mode,flush=True)
        return report
    finally:g.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--candidate',required=True,type=Path)
    p.add_argument('--key',required=True,type=Path)
    p.add_argument('--evidence',required=True,type=Path)
    p.add_argument('--modes',default=','.join(MODES))
    p.add_argument('--timeout',type=int,default=240)
    args=p.parse_args()
    try:
        candidate=args.candidate.absolute();iso=candidate/'images/xtos-developer-live-x86_64.iso'
        if not iso.is_file():raise ValueError('candidate ISO missing')
        if not args.key.is_file():raise ValueError('control-host verification key missing')
        modes=args.modes.split(',')
        if not modes or any(m not in MODES for m in modes) or len(set(modes))!=len(modes):raise ValueError('invalid modes')
        evidence=args.evidence.absolute()
        if evidence.exists():raise ValueError('use fresh evidence directory')
        if evidence==Path('/') or evidence.parent.resolve()!=evidence.parent:raise ValueError('unsafe evidence directory')
        evidence.mkdir(parents=True)
        before=sha(iso);rows=[]
        for mode in modes:
            print('LIVE_MODE_START='+mode,flush=True)
            rows.append(validate_mode(candidate,evidence,args.key.absolute(),mode,args.timeout))
        if sha(iso)!=before:raise ValueError('candidate ISO was modified')
        report={'schema_version':1,'status':'pass','modes':rows,'iso_sha256':before,'scope':'QEMU BIOS/UEFI and CD/USB file media; not physical hardware'}
        (evidence/'results.json').write_text(json.dumps(report,indent=2,sort_keys=True)+'\n')
        print('LIVE_VERIFICATION=PASS '+str(evidence/'results.json'),flush=True)
    except (ValueError,OSError,RuntimeError,subprocess.SubprocessError) as exc:
        print('LIVE_VERIFICATION=FAIL '+str(exc),file=__import__('sys').stderr);return 1
    return 0

if __name__=='__main__':raise SystemExit(main())
