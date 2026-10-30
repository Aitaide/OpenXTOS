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
from live_ai import install_runtime, validate_runtime
from install_eeg_loop import install as install_eeg_loop

REQUIRED_CONFIG=['CONFIG_PREEMPT_RT=y','CONFIG_SQUASHFS=y','CONFIG_SQUASHFS_ZLIB=y','CONFIG_OVERLAY_FS=y','CONFIG_BLK_DEV_LOOP=y','CONFIG_ISO9660_FS=y','CONFIG_USB_STORAGE=y','CONFIG_DEVTMPFS=y']
REQUIRED_CONFIG += ['CONFIG_SECURITY=y','CONFIG_SECURITY_LANDLOCK=y','CONFIG_SECCOMP=y',
                    'CONFIG_SECCOMP_FILTER=y','CONFIG_SECURITYFS=y','CONFIG_NAMESPACES=y',
                    'CONFIG_UTS_NS=y','CONFIG_IPC_NS=y','CONFIG_PID_NS=y','CONFIG_NET_NS=y','CONFIG_USER_NS=y']
RUNTIME_FILES={'xtos-start':'sbin/xtos-start','xtos-dhcp':'usr/libexec/xtos/xtos-dhcp','xtos-ssh':'usr/libexec/xtos/xtos-ssh','xtos-console':'usr/libexec/xtos/xtos-console'}
NATIVE_TOOLS={'info':'usr/bin/info','net':'usr/bin/net','hw':'usr/bin/hw','clock':'usr/bin/clock','pkg':'usr/bin/pkg','fw':'usr/bin/fw','rt':'usr/bin/rt','persist':'usr/bin/persist','events':'usr/bin/events','update':'usr/bin/update','cgroup':'usr/bin/cgroup'}
NATIVE_TOOLS['runner']='usr/libexec/xtos/runner'
NATIVE_TOOLS['modelverify']='usr/libexec/xtos/modelverify'


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


def account_passwd():
    return ('root:x:0:0:root:/root:/bin/bash\n'
            'xtos:x:1000:1000:XTOS Developer:/home/xtos:/bin/bash\n'
            'runner:x:62000:62000:Confined action runner:/nonexistent:/usr/sbin/nologin\n')


def account_shadow(key_login):
    # NP is an invalid crypt hash, not a password. Dropbear rejects '!' accounts
    # before public-key auth; its -s flag rejects every password method regardless.
    marker = 'NP' if key_login else '!'
    return f'root:{marker}:0:0:99999:7:::\nxtos:{marker}:0:0:99999:7:::\nrunner:!:0:0:99999:7:::\n'


def account_groups():
    # render is explicitly allocated by XTOS, not copied from the host's GID.
    return ('root:x:0:\n'
            'tty:x:5:\n'
            'dialout:x:20:xtos\n'
            'video:x:44:xtos\n'
            'xtos:x:1000:\n'
            'render:x:1001:xtos\n'
            'runner:x:62000:\n')


def source_provenance(repo=ROOT):
    """Bind an image to its exact source revision and actual worktree state."""
    repo = Path(repo)
    try:
        head = subprocess.check_output(
            ['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
        status = subprocess.check_output(
            ['git', '-C', str(repo), 'status', '--porcelain', '--untracked-files=all'],
            text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f'cannot determine source provenance: {exc}') from exc
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', head):
        raise ValueError('git returned an invalid source revision')
    return {'source_git_head': head,
            'source_state': 'dirty' if status.strip() else 'clean'}


def validate_source_provenance(provenance, expected_source=None, require_clean=False):
    if expected_source is not None:
        if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', expected_source):
            raise ValueError('expected source must be a complete Git SHA')
        if provenance['source_git_head'] != expected_source:
            raise ValueError('source does not match expected commit')
    if require_clean and provenance['source_state'] != 'clean':
        raise ValueError('release build requires clean source')


def verify_source_unchanged(initial, repo=ROOT):
    current = source_provenance(repo)
    if current != initial or (initial['source_state'] == 'clean' and current['source_state'] != 'clean'):
        raise ValueError('source changed while building candidate')
    return current


def write(root, relative, text, mode=0o644):
    target=root/relative
    if target.is_symlink():target.unlink()
    if not target.parent.resolve().is_relative_to(root):raise ValueError('staging path escapes root')
    target.parent.mkdir(parents=True,exist_ok=True)
    # Explicit UTF-8: the locale of the calling environment must never decide
    # how motd/logo bytes are encoded (LC_ALL=C would default to ASCII).
    target.write_text(text,encoding='utf-8');target.chmod(mode)


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


def native_identity_flags(name):
    # No runtime uid/gid override or shared nobody identity in production.
    return ['-DXTOS_RUNNER_UID=62000','-DXTOS_RUNNER_GID=62000'] if name == 'runner' else []


def build_native_tools(stage):
    """Compile XTOS-native C tools as static binaries into the staging rootfs."""
    include=ROOT/'tools'/'xtos-native'/'common'
    vendor=ROOT/'tools'/'xtos-native'/'vendor'/'monocypher'
    mbvendor=ROOT/'tools'/'xtos-native'/'vendor'/'mbedtls'
    mbdefines=['-DMBEDTLS_CONFIG_FILE="xtos_mbedtls_config.h"','-I',str(mbvendor)]
    for name,target in NATIVE_TOOLS.items():
        directory=ROOT/'tools'/'xtos-native'/name
        source=directory/(name+'.c')
        if not source.is_file():raise ValueError('native C tool source missing: '+name)
        output=stage/target
        output.parent.mkdir(parents=True,exist_ok=True)
        sources=[source]
        includes=['-I',str(include)]
        if name in ('pkg','modelverify'):
            # Both helpers verify Ed25519 with vendored Monocypher; runtime uses
            # no openssl process or host-dependent shared crypto library.
            sources+=[vendor/'monocypher.c',vendor/'monocypher-ed25519.c']
            includes+=['-I',str(vendor)]
        if name=='update':
            # update links the vendored Monocypher verifier and the vendored
            # mbedTLS transport (built once into .build/mbedtls).
            run(['bash',str(ROOT/'scripts'/'build_mbedtls.sh')])
            sources+=[vendor/'monocypher.c',vendor/'monocypher-ed25519.c',
                      ROOT/'.build'/'mbedtls'/'libmbedtls_xtos.a']
            includes+=['-I',str(vendor),'-I',str(mbvendor/'include'),*mbdefines]
        run([os.environ.get('CC','gcc'),'-O2','-Wall','-Wextra','-Werror','-static',
             *includes,*native_identity_flags(name),'-o',str(output),*[str(s) for s in sources]])
        output.chmod(0o755)


def install_eeg_stream(stage, work, repo=ROOT):
    """Build the native producer against the staged SDK; never need target Python."""
    stage, work, repo = Path(stage), Path(work), Path(repo)
    work.mkdir(parents=True,exist_ok=True)
    # A reused system build root must never reuse stale objects or toolchain state.
    work=Path(tempfile.mkdtemp(prefix='eeg-stream-',dir=work))
    directory=work/'eeg-stream-source'
    directory.mkdir()
    native=repo/'tools/xtos-eeg-stream'
    sources=sorted(p for p in native.iterdir() if p.suffix in ('.c','.h') or p.name=='Makefile')
    expected={str(p.relative_to(repo)):sha(p) for p in sources}
    for path in sources:
        shutil.copy2(path,directory/path.name)
        if sha(directory/path.name)!=expected[str(path.relative_to(repo))]:
            raise ValueError('EEG stream source changed while staging')
    env=dict(os.environ,CC=os.environ.get('CC','gcc'),AR=os.environ.get('AR','ar'))
    run(['make','-C',directory,'install',f'CC={env["CC"]}',f'AR={env["AR"]}',
         f'BUILD_DIR={work/"eeg-stream-build"}',f'DESTDIR={stage}','PREFIX=/usr',
         f'CPPFLAGS=-I{stage/"usr/include"}',
         f'SDK_LIBS={stage/"usr/lib/libxtos-bci.a"} {stage/"usr/lib/libxtos-signal.a"}',
         'LDFLAGS=-static'],env=env)
    if {str(p.relative_to(repo)):sha(p) for p in sources}!=expected:
        raise ValueError('EEG stream source changed during build')
    binary=stage/'usr/bin/eeg-stream'
    if binary.read_bytes()[:4]!=b'\x7fELF':
        raise ValueError('EEG stream producer is not ELF')
    record={'schema':'xtos-eeg-stream-source-inventory-v1','status':'installed_native',
            'source_hashes':expected,'binary_hashes':{'usr/bin/eeg-stream':sha(binary)},
            'physical_device_evidence':False}
    write(stage,'usr/share/xtos/eeg-stream-sources.json',json.dumps(record,sort_keys=True,indent=2)+'\n')
    return record


def install_sdk(stage, work):
    source=work/'sdk-source'
    for name in ['sdk']:
        shutil.copytree(ROOT/name,source/name,symlinks=True)
    include=source/'kernel/linux/include/linux';include.mkdir(parents=True)
    shutil.copy2(ROOT/'kernel/linux/include/linux/xtos_queue.h',include/'xtos_queue.h')
    parser=source/'kernel/linux/drivers/xtos/bci';parser.mkdir(parents=True)
    shutil.copy2(ROOT/'kernel/linux/drivers/xtos/bci/xtos_cyton_packet.h',parser/'xtos_cyton_packet.h')
    shutil.copy2(ROOT/'kernel/linux/drivers/xtos/bci/xtos_cyton_config.h',parser/'xtos_cyton_config.h')
    env=dict(os.environ,CC=os.environ.get('CC','gcc'),AR=os.environ.get('AR','ar'))
    for library in ['rt','signal','bci']:
        directory=source/'sdk'/('libxtos-'+library)
        run(['make','-C',directory,'clean','all'],env=env)
        (stage/'usr/lib').mkdir(parents=True,exist_ok=True)
        shutil.copy2(directory/('libxtos-'+library+'.a'),stage/'usr/lib')
        headers=stage/'usr/include/xtos';headers.mkdir(parents=True,exist_ok=True)
        for h in (directory/'include/xtos').glob('*.h'):shutil.copy2(h,headers/h.name)
    tests=[('rt','test_rt'),('rt','test_spsc'),('signal','test_signal'),('signal','test_eeg_window')]
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
    native_bci=source/'tools/xtos-cyton'
    shutil.copytree(ROOT/'tools/xtos-cyton',native_bci,symlinks=True)
    run(['make','-C',native_bci,'clean','all','LDFLAGS=-static'],env=env)
    shutil.copy2(native_bci/'bci',stage/'usr/bin/bci')
    shutil.copy2(ROOT/'tools/xtos-eeg-pipeline/fixtures/valid.csv',fixture/'valid.csv')
    cyton=stage/'usr/share/xtos/eeg-pipeline';cyton.mkdir(parents=True,exist_ok=True)
    for name in ['openbci_cyton.py','cyton_capture.py','cyton_replay.py','device_contract.py']:
        shutil.copy2(ROOT/'tools/xtos-eeg-pipeline'/name,cyton/name)
    shutil.copy2(ROOT/'system/rootfs/usr/bin/xtos-selftest',stage/'usr/bin/xtos-selftest')
    return install_eeg_stream(stage,work,ROOT)


def grub_config(kernel_extra=''):
    """Validate plain kernel arguments and render the complete GRUB config."""
    if not isinstance(kernel_extra,str):
        raise ValueError('kernel-extra must be a string of plain kernel arguments')
    if not re.fullmatch(r'[A-Za-z0-9._+,:=/@ -]*',kernel_extra):
        raise ValueError('kernel-extra allows only safe ASCII kernel arguments separated by ordinary spaces')
    tokens=kernel_extra.split(' ')
    if any(token.startswith('lsm=') for token in tokens if token):
        raise ValueError('kernel-extra lsm= is forbidden because it overrides resolved CONFIG_LSM')
    cmdline='console=tty0 console=ttyS0,115200 xtos.live=1'
    if kernel_extra:
        cmdline+=' '+kernel_extra
    newline=chr(10)
    return (newline.join([
        'set timeout=3','set default=0','serial --unit=0 --speed=115200',
        'terminal_input console serial','terminal_output console serial',
        'menuentry "XTOS Bate Developer Live (RAM writes, no installation)" {',
        ' linux /boot/bzImage '+cmdline,' initrd /boot/initramfs.gz','}'
    ])+newline)


def build(args):
    grub=grub_config(getattr(args,'kernel_extra',''))
    output=args.output_dir.absolute()
    if os.path.lexists(output):raise ValueError('output already exists; use a fresh directory')
    permitted=(ROOT/'.build/live').resolve()
    scratch=Path(os.environ.get('TMPDIR','/root/.hermes/cache/scratch')).resolve()
    if not ((output.is_relative_to(permitted) and output!=permitted) or (output.is_relative_to(scratch) and output!=scratch)):
        raise ValueError('output must be a fresh .build/live/ or scratch child')
    if output.parent.resolve()!=output.parent:raise ValueError('symlinked output parent')
    ai_root=getattr(args,'ai_runtime_root',None)
    ai_descriptor=getattr(args,'ai_runtime_descriptor',None)
    if (ai_root is None)!=(ai_descriptor is None):
        raise ValueError('ai-runtime-root and ai-runtime-descriptor must be selected together')
    ai_lock=sha(ROOT/'tools/xtos-onnx-baseline/requirements-ai.lock') if ai_root else None
    if ai_root:
        validate_runtime(ai_root,ai_descriptor,ai_lock)
    root=args.userspace_root.absolute();fd,manifest=open_owned_root(root);os.close(fd)
    if manifest['architecture']!='x86_64':raise ValueError('Live builder currently supports x86_64 only')
    kernel=args.kernel_build.absolute()
    config=(kernel/'.config').read_text()
    for setting in REQUIRED_CONFIG:
        if setting not in config.splitlines():raise ValueError('Live kernel missing '+setting)
    lsm_values=[line[len('CONFIG_LSM='):] for line in config.splitlines() if line.startswith('CONFIG_LSM=')]
    if len(lsm_values)!=1 or not re.fullmatch(r'"[^"\\\r\n]*"',lsm_values[0]) or 'landlock' not in lsm_values[0][1:-1].split(','):
        raise ValueError('Live kernel CONFIG_LSM must enable exact landlock entry')
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
    provenance = source_provenance(ROOT)
    validate_source_provenance(provenance, args.expected_source, args.require_clean)
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
        persistent_variant = 'xtos.persist=' in grub
        live_storage = ('read-only Live medium plus optional labeled ext4 persistence for /home and /root; '
                        'tmpfs overlay elsewhere' if persistent_variant else
                        'read-only Live medium plus nonpersistent tmpfs overlay')
        live_motd = ('optional /home+/root persistence' if persistent_variant else 'no persistence')
        # Base package /etc/os-release is regenerated from honest VERSION/build metadata.
        buildid='live-'+sha(image)[:12]+'-'+manifest['lock_sha256'][:12]
        run([sys.executable,ROOT/'scripts/gen_system_identity.py','--output-dir',runtime/'etc','--image-type','developer','--arch','x86_64','--kernel-release',release,'--source-state',provenance['source_state'],'--build-id',buildid])
        for name,target in RUNTIME_FILES.items():
            dest=runtime/target;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT/'system/live'/name,dest);dest.chmod(0o755)
        shutil.copy2(args.busybox,runtime/'usr/libexec/xtos/busybox')
        shutil.copytree(modules,runtime/'usr/lib/modules'/release,symlinks=True)
        for name in ['build','source']:(runtime/'usr/lib/modules'/release/name).unlink(missing_ok=True)
        run(['depmod','-b',runtime,release])
        build_native_tools(runtime)
        eeg_stream_sources=install_sdk(runtime,stage)
        native_bci_result=subprocess.run([sys.executable,str(ROOT/'scripts/verify_native_bci_runtime.py'),'--rootfs',runtime],
                                        capture_output=True,text=True,check=True,timeout=20)
        native_bci_runtime=json.loads(native_bci_result.stdout)
        if native_bci_runtime.get('status')!='pass' or native_bci_runtime.get('eight_channel_filter') is not True:
            raise ValueError('Native BCI runtime smoke did not pass')
        cyton_result=subprocess.run([sys.executable,str(ROOT/'scripts/verify_cyton_runtime.py'),'--rootfs',runtime,'--smoke'],
                                   capture_output=True,text=True,check=True,timeout=40)
        cyton_runtime=json.loads(cyton_result.stdout)
        if cyton_runtime.get('status')!='pass' or cyton_runtime.get('interpreter_runtime_smoke') is not True:
            raise ValueError('Cyton runtime smoke did not pass')
        cyton_runtime['profile_sha256']=sha(ROOT/'system/profiles/live-developer.json')
        eeg_loop_sources = install_eeg_loop(runtime, ROOT)
        ai_runtime=install_runtime(runtime,ai_root,ai_descriptor,ROOT,ai_lock) if ai_root else None
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
        write(runtime,'etc/passwd',account_passwd())
        write(runtime,'etc/group',account_groups())
        write(runtime,'etc/mdev.conf',(ROOT/'system/live/mdev.conf').read_text())
        write(runtime,'etc/shadow',account_shadow(bool(key)),0o600)
        write(runtime,'etc/shells','/bin/sh\n/bin/bash\n/usr/bin/sh\n/usr/bin/bash\n')
        # UTF-8 block-letter logo; motd colors are interactive-login cosmetics
        # (8-color SGR, real login consoles only; SSH/script paths never render
        # motd). The console is LANG=C.UTF-8 so the blocks decode correctly.
        logo = (
            '██╗  ██╗████████╗ ██████╗ ███████╗\n'
            '╚██╗██╔╝╚══██╔══╝██╔═══██╗██╔════╝\n'
            ' ╚███╔╝    ██║   ██║   ██║███████╗\n'
            ' ██╔██╗    ██║   ██║   ██║╚════██║\n'
            '██╔╝ ██╗   ██║   ╚██████╔╝███████║\n'
            '╚═╝  ╚═╝   ╚═╝    ╚═════╝ ╚══════╝\n')
        write(runtime,'etc/issue','XTOS Bate \\\\r  \\\\n \\\\l\n\n')
        motd=('\033[1;96m'+logo+'\033[0m'
              '\n \033[1mXTOS Bate Developer Live\033[0m  |  '+live_motd+'\n'
              ' \033[2mCommands: info, net show, hw, clock, pkg, fw, rt status, events, cgroup status\033[0m\n\n')
        write(runtime,'etc/motd',motd)
        write(runtime,'etc/profile','# XTOS system profile\nexport LANG=C.UTF-8 LC_ALL=C.UTF-8\nif [ "$EUID" = 0 ]; then PS1="\\\\[\\\\e[1;32m\\\\]xtos#\\\\[\\\\e[0m\\\\] "; else PS1="\\\\[\\\\e[1;36m\\\\]xtos$\\\\[\\\\e[0m\\\\] "; fi\n[ -f /etc/motd ] && [ -n "${PS1:-}" ] && cat /etc/motd\n')
        home=runtime/'home/xtos';shutil.copytree(runtime/'etc/skel',home,symlinks=True)
        os.chown(home,1000,1000)
        for current,dirs,files in os.walk(home,followlinks=False):
            for name in dirs+files:os.chown(Path(current)/name,1000,1000,follow_symlinks=False)
        write(runtime,'etc/sudoers','Defaults env_reset\nroot ALL=(ALL:ALL) ALL\n',0o440)
        # Record build-intentional replacements so `pkg verify` can attribute them
        write(runtime,'usr/share/xtos/pkg-overrides.txt','/etc/group\n/etc/issue\n/etc/mdev.conf\n/etc/sudoers\n/etc/chrony/chrony.conf\n',0o644)
        # Pinned source-signature key for `update` (default XTOS_UPDATE_PUBKEY path).
        shutil.copy2(ROOT/'system/packages/repo-public.pem',runtime/'usr/share/xtos/repo-public.pem')
        shutil.copy2(ROOT/'system/packages/update-ca-bundle.pem',runtime/'usr/share/xtos/update-ca-bundle.pem')
        write(runtime,'etc/nsswitch.conf','passwd: files\ngroup: files\nshadow: files\nhosts: files dns\nnetworks: files\n')
        write(runtime,'etc/hostname','xtos-live\n')
        write(runtime,'etc/hosts','127.0.0.1 localhost xtos-live\n::1 localhost\n')
        # FHS dirs that no locked deb owns; /opt is the parent of /opt/xtos-apps
        # (the xpkg install root). pkg creates missing parents itself, but the
        # system tree should still carry the standard directory.
        for d in ('opt','srv','mnt','media'):
            (runtime/d).mkdir(exist_ok=True)
        # Resident NTP (xtos-start runs chronyd with this config). QEMU/isolated
        # networks leave the clock honestly unsynchronized; the `clock` tool only
        # claims synchronized when `chronyc tracking` confirms it. Replaces the
        # deb-shipped /etc/chrony/chrony.conf — recorded in pkg-overrides.
        write(runtime,'etc/chrony/chrony.conf',
              '# XTOS resident NTP daemon (started by xtos-start).\n'
              '# user root: the live system is single-user root; the Debian default\n'
              '# (drop to the chrony user) fails because the runtime dirs are root-owned.\n'
              'user root\n'
              'driftfile /run/chrony/drift\n'
              'pidfile /run/chrony/chronyd.pid\n'
              'logdir /run/log\n'
              'server ntp.aliyun.com iburst\n'
              'server time.cloudflare.com iburst\n'
              'server cn.pool.ntp.org iburst\n'
              'makestep 1.0 3\n'
              'rtcsync\n')
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
        write(iso,'boot/grub/grub.cfg',grub)
        images=stage/'images';images.mkdir()
        product=images/'xtos-developer-live-x86_64.iso'
        env=dict(os.environ,SOURCE_DATE_EPOCH=str(epoch),LC_ALL='C',TZ='UTC')
        run(['grub-mkrescue','-o',product,iso],env=env)
        build_manifest={'schema_version':1,'status':'built-unverified-live-candidate','architecture':'x86_64','kernel_release':release,'kernel_sha256':sha(image),'kernel_config_sha256':sha(kernel/'.config'),'userspace_lock_sha256':manifest['lock_sha256'],'source_git_head':provenance['source_git_head'],'source_state':provenance['source_state'],'authorized_public_key_sha256':hashlib.sha256(key.encode()).hexdigest() if key else None,'rootfs_squashfs_sha256':sha(squash),'initramfs_sha256':sha(iso/'boot/initramfs.gz'),'iso_sha256':sha(product),'iso_bytes':product.stat().st_size,'epoch':epoch,'storage':live_storage,'ssh':'publickey only when provisioned','firewall':'implemented for private QEMU validation; physical policy deployment unverified','secure_boot':'unverified/unsupported','private_key_included':False}
        build_manifest['ai_runtime']=ai_runtime
        build_manifest['cyton_runtime']=cyton_runtime
        build_manifest['native_bci_runtime']=native_bci_runtime
        build_manifest['eeg_loop_sources']=eeg_loop_sources
        build_manifest['eeg_stream_sources']=eeg_stream_sources
        shutil.copy2(kernel/'.config', images/'kernel.config')
        write(stage,'images/build-manifest.json',json.dumps(build_manifest,sort_keys=True,indent=2)+'\n')
        write(stage,'images/SHA256SUMS',sha(product)+'  '+product.name+'\n'+sha(images/'build-manifest.json')+'  build-manifest.json\n')
        if output.exists():raise ValueError('output appeared during build')
        verify_source_unchanged(provenance, ROOT)
        os.rename(stage,output)
        print(json.dumps({'status':'built','output':str(output),'iso':str(output/'images'/product.name),'kernel_release':release}))
    finally:
        if stage.exists():shutil.rmtree(stage)


def main():
    if sys.argv[1:2]==['install-eeg-stream']:
        p=argparse.ArgumentParser(description='Install only the native producer using the staged SDK')
        p.add_argument('--rootfs',type=Path,required=True)
        p.add_argument('--work-dir',type=Path,required=True)
        p.add_argument('--source-root',type=Path,default=ROOT)
        args=p.parse_args(sys.argv[2:])
        print(json.dumps(install_eeg_stream(args.rootfs,args.work_dir,args.source_root),sort_keys=True))
        return 0
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--userspace-root',type=Path,required=True)
    p.add_argument('--kernel-build',type=Path,required=True)
    p.add_argument('--busybox',type=Path,required=True)
    p.add_argument('--output-dir',type=Path,required=True)
    p.add_argument('--kernel-extra',default='',help='plain ASCII kernel args separated by ordinary spaces; safe characters A-Z a-z 0-9 . _ + , : = / @ - only; no lsm= overrides or GRUB quoting/expansion')
    p.add_argument('--authorized-key',type=Path,help='explicit public key only; never a private key')
    p.add_argument('--epoch',type=int)
    p.add_argument('--require-clean',action='store_true',help='reject uncommitted or untracked source')
    p.add_argument('--expected-source',help='require this complete source commit SHA')
    p.add_argument('--ai-runtime-root',type=Path,help='verified private CPU runtime root from provision_ai_runtime.py')
    p.add_argument('--ai-runtime-descriptor',type=Path,help='exact descriptor bound by the private runtime manifest')
    args=p.parse_args()
    try:build(args)
    except (ValueError,OSError,subprocess.CalledProcessError) as exc:p.error(str(exc))
    return 0

if __name__=='__main__':raise SystemExit(main())
