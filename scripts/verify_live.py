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

MODES=('bios-cd','uefi-cd','bios-usb','uefi-usb')
info_version_marker='0.9.0-devpreview.1'


def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


class Guest:
    def __init__(self,candidate,evidence,key,mode,timeout):
        self.e=evidence;self.mode=mode;self.timeout=timeout;self.key=key
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
        args=['qemu-system-x86_64','-machine','q35','-accel','tcg,thread=multi','-cpu','max','-m','3072','-smp','2','-display','none','-monitor','none','-no-reboot',
              '-serial',f'unix:{path},server=on,wait=off','-drive',f'file={self.disk},format=raw,if=none,id=sentinel','-device','nvme,drive=sentinel,serial=XTOS-LIVE-INTERNAL-TEST',
              '-netdev',f'user,id=net,hostfwd=tcp:127.0.0.1:{self.port}-:22','-device','virtio-net-pci,netdev=net']
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


def validate_mode(candidate,evidence,key,mode,timeout):
    g=Guest(candidate,evidence,key,mode,timeout)
    rows={}
    try:
        command='set -e; uname -r; cat /sys/kernel/realtime; cat /etc/os-release; test ! -e /usr/sbin/xtos-install; ! grep -qw xtos.installer=1 /proc/cmdline; grep " / overlay " /proc/mounts; grep " /run/live-media " /proc/mounts; grep " /run/live-rw tmpfs " /proc/mounts; test "$(id -u)" = 0; test "$(cat /sys/kernel/realtime)" = 1; '+('test -d /sys/firmware/efi;' if mode.startswith('uefi') else 'test ! -d /sys/firmware/efi;')+' echo LIVE_MOUNTS=PASS'
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
        rows['clock_not_claimed_synchronized']=json.loads(g.ssh('xtos','clock status --json').stdout)['synchronized'] is False
        if not rows['clock_not_claimed_synchronized']:raise RuntimeError('clock falsely marked synchronized')
        rows['host_key_trusted_via_serial']=True
        # Prove medium is read-only and default write state is RAM.
        r=g.ssh('root','set -e; if touch /run/live-media/LIVE_MUST_BE_READONLY 2>/run/readonly-error; then exit 1; fi; test -s /run/readonly-error; echo LIVE_READONLY=PASS')
        rows['readonly_media']=r.stdout
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
