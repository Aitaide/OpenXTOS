#!/usr/bin/env python3
"""Build a private non-installing x86_64 Live ISO in a fresh output directory."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from userspace import resolve_root, verify_rootfs
from userspace_root import open_owned_root

REQUIRED_CONFIG=['CONFIG_PREEMPT_RT=y','CONFIG_SQUASHFS=y','CONFIG_SQUASHFS_ZLIB=y','CONFIG_OVERLAY_FS=y','CONFIG_BLK_DEV_LOOP=y','CONFIG_ISO9660_FS=y','CONFIG_USB_STORAGE=y','CONFIG_DEVTMPFS=y']
RUNTIME_FILES={'xtos-start':'sbin/xtos-start','xtos-dhcp':'usr/libexec/xtos/xtos-dhcp','xtos-ssh':'usr/libexec/xtos/xtos-ssh','xtos-console':'usr/libexec/xtos/xtos-console'}
NATIVE_TOOLS={'info':'usr/bin/info','net':'usr/bin/net','hw':'usr/bin/hw','clock':'usr/bin/clock','pkg':'usr/bin/pkg','fw':'usr/bin/fw','rt':'usr/bin/rt','persist':'usr/bin/persist','events':'usr/bin/events'}


def run(command, **kwargs):
    subprocess.run([str(x) for x in command], check=True, **kwargs)


def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for data in iter(lambda:stream.read(1024*1024),b''):h.update(data)
    return h.hexdigest()


def validate_public_key(path):
    text=path.read_text().strip()
    if '\n' in text or 'PRIVATE KEY' in text:raise ValueError('only one public SSH key line is allowed')
    fields=text.split()
    if len(fields)<2 or fields[0] not in ('ssh-ed25519','ssh-rsa','ecdsa-sha2-nistp256'):raise ValueError('unsupported public SSH key')
    try:
        raw=base64.b64decode(fields[1],validate=True)
        length=struct.unpack('>I',raw[:4])[0]
        algorithm=raw[4:4+length].decode('ascii')
        if algorithm!=fields[0] or len(raw)<=4+length:raise ValueError('key algorithm/blob mismatch')
    except (ValueError,struct.error,UnicodeError) as exc:raise ValueError('invalid SSH public key') from exc
    return fields[0]+' '+fields[1]+'\n'


def account_shadow(key_login):
    # NP is an invalid crypt hash, not a password. Dropbear rejects '!' accounts
    # before public-key auth; its -s flag rejects every password method regardless.
    marker = 'NP' if key_login else '!'
    return f'root:{marker}:0:0:99999:7:::\nxtos:{marker}:0:0:99999:7:::\n'


def write(root, relative, text, mode=0o644):
    target=root/relative
    if target.is_symlink():target.unlink()
    if not target.parent.resolve().is_relative_to(root):raise ValueError('staging path escapes root')
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(text);target.chmod(mode)


def tool_from_root(root, relative):
    binary=resolve_root(root,relative)
    loader=resolve_root(root,'/lib64/ld-linux-x86-64.so.2')
    libraries=':'.join(str(resolve_root(root,p)) for p in ['/lib/x86_64-linux-gnu','/usr/lib/x86_64-linux-gnu','/usr/lib'])
    return [loader,'--library-path',libraries,binary]


def pack_initramfs(stage, output, epoch):
    entries=['.']
    for current,dirs,files in os.walk(stage,followlinks=False):
        entries.extend(str((Path(current)/n).relative_to(stage)) for n in dirs+files)
    for name in entries:os.utime(stage/name,(epoch,epoch),follow_symlinks=False)
    with output.open('wb') as stream:
        process=subprocess.Popen(['cpio','--null','-o','-H','newc','--owner=0:0','--reproducible'],cwd=stage,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        gzip=subprocess.Popen(['gzip','-n','-6'],stdin=process.stdout,stdout=stream)
        assert process.stdout is not None and process.stdin is not None and process.stderr is not None
        process.stdout.close()
        process.stdin.write(('\0'.join(sorted(entries))+'\0').encode());process.stdin.close()
        error=process.stderr.read();code=process.wait();gzip.wait()
        if code or gzip.returncode:raise ValueError('initramfs packing failed: '+error.decode(errors='replace'))


def build_native_tools(stage):
    """Compile XTOS-native C tools as static binaries into the staging rootfs."""
    for name,target in NATIVE_TOOLS.items():
        directory=ROOT/'tools'/'xtos-native'/name
        source=directory/(name+'.c')
        if not source.is_file():raise ValueError('native C tool source missing: '+name)
        output=stage/target
        output.parent.mkdir(parents=True,exist_ok=True)
        run([os.environ.get('CC','gcc'),'-O2','-Wall','-Wextra','-Werror','-static',
             '-o',str(output),str(source)])
        output.chmod(0o755)


def install_sdk(stage, work):
    source=work/'sdk-source'
    for name in ['sdk']:
        shutil.copytree(ROOT/name,source/name,symlinks=True)
    include=source/'kernel/linux/include/linux';include.mkdir(parents=True)
    shutil.copy2(ROOT/'kernel/linux/include/linux/xtos_queue.h',include/'xtos_queue.h')
    env=dict(os.environ,CC='gcc',AR='ar')
    for library in ['rt','signal','bci']:
        directory=source/'sdk'/('libxtos-'+library)
        run(['make','-C',directory,'clean','all'],env=env)
        (stage/'usr/lib').mkdir(parents=True,exist_ok=True)
        shutil.copy2(directory/('libxtos-'+library+'.a'),stage/'usr/lib')
        headers=stage/'usr/include/xtos';headers.mkdir(parents=True,exist_ok=True)
        for h in (directory/'include/xtos').glob('*.h'):shutil.copy2(h,headers/h.name)
    tests=[('rt','test_rt'),('rt','test_spsc'),('signal','test_signal')]
    dest=stage/'usr/libexec/xtos';dest.mkdir(parents=True,exist_ok=True)
    for library,name in tests:
        directory=source/'sdk'/('libxtos-'+library)
        run(['make','-C',directory,name,'CFLAGS=-O2 -Wall -Wextra -Werror -static'],env=env)
        shutil.copy2(directory/name,dest/name)
    # Data pipeline and benchmark are built from current private sources in isolation.
    for tool in ['xtos-eeg-pipeline','xtos-signal-benchmark']:
        directory=source/'tools'/tool
        shutil.copytree(ROOT/'tools'/tool,directory,symlinks=True)
        run(['make','-C',directory,'clean','all','LDFLAGS=-static -lm','LDLIBS=-static -lm'],env=env)
        shutil.copy2(directory/tool,stage/'usr/bin'/tool)
    fixture=stage/'usr/share/xtos/fixtures';fixture.mkdir(parents=True,exist_ok=True)
    shutil.copy2(ROOT/'tools/xtos-eeg-pipeline/fixtures/valid.csv',fixture/'valid.csv')
    shutil.copy2(ROOT/'system/rootfs/usr/bin/xtos-selftest',stage/'usr/bin/xtos-selftest')


def build(args):
    output=args.output_dir.absolute()
    if os.path.lexists(output):raise ValueError('output already exists; use a fresh directory')
    permitted=(ROOT/'.build/live').resolve()
    scratch=Path(os.environ.get('TMPDIR','/root/.hermes/cache/scratch')).resolve()
    if not ((output.is_relative_to(permitted) and output!=permitted) or (output.is_relative_to(scratch) and output!=scratch)):
        raise ValueError('output must be a fresh .build/live/ or scratch child')
    if output.parent.resolve()!=output.parent:raise ValueError('symlinked output parent')
    root=args.userspace_root.absolute();fd,manifest=open_owned_root(root);os.close(fd)
    if manifest['architecture']!='x86_64':raise ValueError('Live builder currently supports x86_64 only')
    kernel=args.kernel_build.absolute()
    config=(kernel/'.config').read_text()
    for setting in REQUIRED_CONFIG:
        if setting not in config.splitlines():raise ValueError('Live kernel missing '+setting)
    release=(kernel/'include/config/kernel.release').read_text().strip()
    if not re.fullmatch(r'[A-Za-z0-9._+-]+',release):raise ValueError('unsafe kernel release')
    image=kernel/'arch/x86/boot/bzImage'
    modules=kernel/'module-root/lib/modules'/release
    if not image.is_file() or not modules.is_dir():raise ValueError('Live kernel image/modules missing')
    if not args.busybox.is_file():raise ValueError('static BusyBox missing')
    detail=subprocess.check_output(['readelf','-l',str(args.busybox)],text=True)
    if 'INTERP' in detail:raise ValueError('initramfs BusyBox must be static')
    for name in ['init','xtos-persist-check',*RUNTIME_FILES]:
        if not (ROOT/'system/live'/name).is_file():raise ValueError('Live runtime source missing: '+name)
    key=validate_public_key(args.authorized_key) if args.authorized_key else None
    for tool in ['grub-mkrescue','xorriso','cpio','gzip']:
        if not shutil.which(tool):raise ValueError('host build tool missing: '+tool)
    for path in ['/usr/bin/mksquashfs','/usr/sbin/dropbear','/usr/bin/dropbearkey','/usr/sbin/chronyd','/usr/bin/sudo']:
        if not resolve_root(root,path).is_file():raise ValueError('Live package missing: '+path)
    epoch=args.epoch
    if epoch is None:epoch=int(subprocess.check_output(['git','-C',str(ROOT),'log','-1','--pretty=%ct'],text=True).strip())
    output.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix='.'+output.name+'-',dir=output.parent));stage.chmod(0o755)
    try:
        runtime=stage/'rootfs'
        # A locked rootfs must never contain device nodes; one stray node breaks
        # copytree on some filesystems and would ship a bogus /dev into the ISO.
        stray=[]
        for current,dirs,files in os.walk(root,followlinks=False):
            for name in files:
                candidate=Path(current)/name
                if candidate.is_socket() or (candidate.exists() and not candidate.is_file() and not candidate.is_symlink()):
                    stray.append(str(candidate))
        if stray:raise ValueError('device/socket nodes in userspace root: '+', '.join(stray[:5]))
        shutil.copytree(root,runtime,symlinks=True)
        run([sys.executable,ROOT/'scripts/configure_userspace.py','--rootfs',runtime])
        persistent_variant = bool(args.kernel_extra and 'xtos.persist=' in args.kernel_extra)
        live_storage = ('read-only Live medium plus optional labeled ext4 persistence for /home and /root; '
                        'tmpfs overlay elsewhere' if persistent_variant else
                        'read-only Live medium plus nonpersistent tmpfs overlay')
        live_motd = ('optional /home+/root persistence' if persistent_variant else 'no persistence')
        # Base package /etc/os-release is regenerated from honest VERSION/build metadata.
        buildid='live-'+sha(image)[:12]+'-'+manifest['lock_sha256'][:12]
        run([sys.executable,ROOT/'scripts/gen_system_identity.py','--output-dir',runtime/'etc','--image-type','developer','--arch','x86_64','--kernel-release',release,'--source-state','dirty','--build-id',buildid])
        for name,target in RUNTIME_FILES.items():
            dest=runtime/target;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT/'system/live'/name,dest);dest.chmod(0o755)
        shutil.copy2(args.busybox,runtime/'usr/libexec/xtos/busybox')
        shutil.copytree(modules,runtime/'usr/lib/modules'/release,symlinks=True)
        for name in ['build','source']:(runtime/'usr/lib/modules'/release/name).unlink(missing_ok=True)
        run(['depmod','-b',runtime,release])
        build_native_tools(runtime)
        install_sdk(runtime,stage)
        # liblsl (Lab Streaming Layer) from the pinned upstream build; the lock
        # covers the .so hash (see tools/lsl/build_lsl.sh + tests/test_lsl_build.py).
        lsl_dir=Path(os.environ.get('XTOS_LSL_DIR',ROOT/'.build/userspace/live-developer-lsl'))
        lsl_lib=lsl_dir/'install/lib/liblsl.so.1.17.7'
        if lsl_lib.is_file():
            expected='75c0aeb9df2992e19cc7034f6f9a51f45dd61bba6dc3d29b5a38917d575090c1'
            if sha(lsl_lib)!=expected:raise ValueError('liblsl.so hash mismatch; rerun tools/lsl/build_lsl.sh')
            (runtime/'usr/lib/x86_64-linux-gnu').mkdir(parents=True,exist_ok=True)
            for name in ('liblsl.so.1.17.7','liblsl.so.2','liblsl.so'):
                shutil.copy2(lsl_dir/'install/lib'/name,runtime/'usr/lib/x86_64-linux-gnu'/name)
            (runtime/'usr/include/lsl').mkdir(parents=True,exist_ok=True)
            for header in sorted((lsl_dir/'install/include/lsl').glob('*.h')):
                shutil.copy2(header,runtime/'usr/include/lsl'/header.name)
            for header in ('lsl_c.h','lsl_cpp.h'):
                shutil.copy2(lsl_dir/'install/include'/header,runtime/'usr/include'/header)
            write(runtime,'usr/share/xtos/lsl-provision.json',
                '{"component":"liblsl","version":"v1.17.7","license":"MIT",'
                '"upstream":"https://github.com/sccn/liblsl",'
                '"lib_sha256":"'+expected+'"}\n')
            # In-guest acceptance source used by scripts/verify_live.py (compiles
            # against the shipped headers inside the live system).
            shutil.copy2(ROOT/'scripts/guest_lsl_pair.cpp',runtime/'usr/share/xtos/lsl-pair.cpp')
        elif os.environ.get('XTOS_LSL_REQUIRED')=='1':
            raise ValueError('liblsl required but not built (run tools/lsl/build_lsl.sh)')
        write(runtime,'etc/passwd','root:x:0:0:root:/root:/bin/bash\nxtos:x:1000:1000:XTOS Developer:/home/xtos:/bin/bash\n')
        write(runtime,'etc/group','root:x:0:\nxtos:x:1000:\n')
        write(runtime,'etc/shadow',account_shadow(bool(key)),0o600)
        write(runtime,'etc/shells','/bin/sh\n/bin/bash\n/usr/bin/sh\n/usr/bin/bash\n')
        logo = ' _____ _  _____  ___ \n|_   _/ \\\\|_   _|/ __|\n  | |/ _ \\\\ | | | (__ \n  |_/_/ \\\\\\\\_|_|  \\\\___|\n'
        write(runtime,'etc/issue','XTOS Bate \\\\r  \\\\n \\\\l\n\n')
        write(runtime,'etc/motd',logo+'\n XTOS Bate Developer Live  |  '+live_motd+'\n Commands: info, net show, hw, clock, pkg, fw, rt status, events\n\n')
        write(runtime,'etc/profile','# XTOS system profile\nexport LANG=C.UTF-8 LC_ALL=C.UTF-8\nif [ "$EUID" = 0 ]; then PS1="\\\\[\\\\e[1;32m\\\\]xtos#\\\\[\\\\e[0m\\\\] "; else PS1="\\\\[\\\\e[1;36m\\\\]xtos$\\\\[\\\\e[0m\\\\] "; fi\n[ -f /etc/motd ] && [ -n "${PS1:-}" ] && cat /etc/motd\n')
        home=runtime/'home/xtos';shutil.copytree(runtime/'etc/skel',home,symlinks=True)
        os.chown(home,1000,1000)
        for current,dirs,files in os.walk(home,followlinks=False):
            for name in dirs+files:os.chown(Path(current)/name,1000,1000,follow_symlinks=False)
        write(runtime,'etc/sudoers','Defaults env_reset\nroot ALL=(ALL:ALL) ALL\n',0o440)
        # Record build-intentional replacements so `pkg verify` can attribute them
        write(runtime,'usr/share/xtos/pkg-overrides.txt','/etc/issue\n/etc/sudoers\n',0o644)
        write(runtime,'etc/nsswitch.conf','passwd: files\ngroup: files\nshadow: files\nhosts: files dns\nnetworks: files\n')
        write(runtime,'etc/hostname','xtos-live\n')
        write(runtime,'etc/hosts','127.0.0.1 localhost xtos-live\n::1 localhost\n')
        sshline=''
        if key:
            for base,uid in [('root',0),('home/xtos',1000)]:
                directory=runtime/base/'.ssh';directory.mkdir(parents=True,exist_ok=True);directory.chmod(0o700)
                write(runtime,base+'/.ssh/authorized_keys',key,0o600)
                os.chown(directory,uid,uid);os.chown(directory/'authorized_keys',uid,uid)
            sshline='::respawn:/usr/libexec/xtos/xtos-ssh\n'
        write(runtime,'etc/inittab','::sysinit:/sbin/xtos-start\n'+sshline+'ttyS0::respawn:/usr/libexec/xtos/busybox getty -L 115200 ttyS0 -n -l /usr/libexec/xtos/xtos-console\ntty1::respawn:/usr/libexec/xtos/busybox getty -L 38400 tty1 -n -l /usr/libexec/xtos/xtos-console\n::ctrlaltdel:/usr/libexec/xtos/busybox reboot\n::shutdown:/usr/libexec/xtos/busybox umount -a -r\n')
        write(runtime,'sbin/xtos-init','#!/bin/sh\nexec /usr/libexec/xtos/busybox init\n',0o755)
        # Source root remains immutable: only this candidate staging is normalized.
        for current,dirs,files in os.walk(runtime,followlinks=False):
            for name in dirs+files:os.utime(Path(current)/name,(epoch,epoch),follow_symlinks=False)
        os.utime(runtime,(epoch,epoch))
        iso=stage/'iso';live=iso/'live';(iso/'boot/grub').mkdir(parents=True);live.mkdir()
        squash=live/'rootfs.squashfs'
        run([*tool_from_root(runtime,'/usr/bin/mksquashfs'),runtime,squash,'-noappend','-comp','gzip','-processors','2','-mkfs-time',str(epoch),'-all-time',str(epoch),'-no-xattrs','-quiet'])
        write(iso,'live/XTOS_LIVE_ID','XTOS-LIVE-DEVELOPER-1\n')
        write(iso,'live/rootfs.sha256',sha(squash)+'  rootfs.squashfs\n')
        early=stage/'initramfs';(early/'bin').mkdir(parents=True);(early/'dev').mkdir()
        shutil.copy2(args.busybox,early/'bin/busybox')
        apps=subprocess.check_output([str(args.busybox),'--list'],text=True).splitlines()
        for app in apps:
            if app!='busybox':(early/'bin'/app).symlink_to('busybox')
        shutil.copy2(ROOT/'system/live/init',early/'init');(early/'init').chmod(0o755)
        shutil.copy2(ROOT/'system/live/xtos-persist-check',early/'xtos-persist-check');(early/'xtos-persist-check').chmod(0o755)
        (early/'newroot').mkdir()
        pack_initramfs(early,iso/'boot/initramfs.gz',epoch)
        shutil.copy2(image,iso/'boot/bzImage')
        grub='set timeout=3\nset default=0\nserial --unit=0 --speed=115200\nterminal_input console serial\nterminal_output console serial\nmenuentry "XTOS Bate Developer Live (RAM writes, no installation)" {\n linux /boot/bzImage console=tty0 console=ttyS0,115200 xtos.live=1'+((' '+args.kernel_extra) if args.kernel_extra else '')+'\n initrd /boot/initramfs.gz\n}\n'
        write(iso,'boot/grub/grub.cfg',grub)
        images=stage/'images';images.mkdir()
        product=images/'xtos-developer-live-x86_64.iso'
        env=dict(os.environ,SOURCE_DATE_EPOCH=str(epoch),LC_ALL='C',TZ='UTC')
        run(['grub-mkrescue','-o',product,iso],env=env)
        provenance={'schema_version':1,'status':'built-unverified-live-candidate','architecture':'x86_64','kernel_release':release,'kernel_sha256':sha(image),'kernel_config_sha256':sha(kernel/'.config'),'userspace_lock_sha256':manifest['lock_sha256'],'source_git_head':subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD'],text=True).strip(),'source_state':'dirty','authorized_public_key_sha256':hashlib.sha256(key.encode()).hexdigest() if key else None,'rootfs_squashfs_sha256':sha(squash),'initramfs_sha256':sha(iso/'boot/initramfs.gz'),'iso_sha256':sha(product),'iso_bytes':product.stat().st_size,'epoch':epoch,'storage':live_storage,'ssh':'publickey only when provisioned','firewall':'implemented for private QEMU validation; physical policy deployment unverified','secure_boot':'unverified/unsupported','private_key_included':False}
        write(stage,'images/build-manifest.json',json.dumps(provenance,sort_keys=True,indent=2)+'\n')
        write(stage,'images/SHA256SUMS',sha(product)+'  '+product.name+'\n'+sha(images/'build-manifest.json')+'  build-manifest.json\n')
        if output.exists():raise ValueError('output appeared during build')
        os.rename(stage,output)
        print(json.dumps({'status':'built','output':str(output),'iso':str(output/'images'/product.name),'kernel_release':release}))
    finally:
        if stage.exists():shutil.rmtree(stage)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--userspace-root',type=Path,required=True)
    p.add_argument('--kernel-build',type=Path,required=True)
    p.add_argument('--busybox',type=Path,required=True)
    p.add_argument('--output-dir',type=Path,required=True)
    p.add_argument('--kernel-extra',default='',help='extra kernel cmdline for test/persistence variants')
    p.add_argument('--authorized-key',type=Path,help='explicit public key only; never a private key')
    p.add_argument('--epoch',type=int)
    args=p.parse_args()
    try:build(args)
    except (ValueError,OSError,subprocess.CalledProcessError) as exc:p.error(str(exc))
    return 0

if __name__=='__main__':raise SystemExit(main())
