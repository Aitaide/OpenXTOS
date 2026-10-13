#!/usr/bin/env python3
"""Provision a verified XTOS package layer without executing package scripts."""
import argparse
import hashlib
import json
from pathlib import Path
import os
import shutil
import subprocess
from userspace_root import open_owned_root


def safe_path(root, relative):
    path=root/relative
    parent=path.parent.resolve()
    if not parent.is_relative_to(root):raise ValueError(f'parent escapes rootfs: {relative}')
    return path


def put(root, relative, text, mode=0o644, expect_symlink_target=None):
    path=safe_path(root,relative)
    if path.is_symlink():
        if expect_symlink_target is None:raise ValueError(f'refusing symlink output: {relative}')
        # Intentional wrapper replacement: only for the exact packaged target.
        if os.readlink(path)!=expect_symlink_target:raise ValueError(f'unexpected symlink target for {relative}')
        path.unlink()
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(text,encoding='utf-8');path.chmod(mode)


def link(root, relative, target):
    path=safe_path(root,relative)
    # Only relative aliases into the same packaged executable directory.
    if '/' in target or target in ('.','..'):raise ValueError('unsafe alias')
    candidate=path.parent/target
    if not candidate.resolve().is_relative_to(root) or not candidate.is_file():raise ValueError(f'packaged alias target missing: {target}')
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.is_symlink():
        if os.readlink(path)==target:return
        path.unlink()
    elif path.exists():return
    path.symlink_to(target)


def library_soname_link(root, relative, subdir_target):
    """Create a SONAME symlink whose target sits one directory below (e.g.
    libblas.so.3 -> blas/libblas.so.3). Only for packaged library dirs."""
    path=safe_path(root,relative)
    parts=subdir_target.split('/')
    if len(parts)!=2 or parts[0] in ('','..','.') or parts[1] in ('','..','.') or not parts[1]:
        raise ValueError('unsafe library soname target')
    candidate=path.parent/parts[0]/parts[1]
    if not candidate.resolve().is_relative_to(root) or not candidate.is_file():
        raise ValueError(f'packaged library target missing: {subdir_target}')
    if path.is_symlink():
        if os.readlink(path)==subdir_target:return
        path.unlink()
    elif path.exists():
        raise ValueError(f'refusing to replace real file: {relative}')
    path.symlink_to(subdir_target)


def configure(root):
    if root.is_symlink() or root.absolute().resolve()!=root.absolute():raise ValueError('rootfs path must not contain symlinks')
    root=root.absolute()
    if root==Path('/'):raise ValueError('refusing host root')
    root_fd,_=open_owned_root(root)
    os.close(root_fd)
    for rel in ['tmp','run','dev','proc','sys','etc/ssl/certs','etc/profile.d','etc/alternatives']:
        path=safe_path(root,rel)
        if path.is_symlink():raise ValueError(f'refusing symlink directory: {rel}')
        path.mkdir(parents=True,exist_ok=True)
    (root/'tmp').chmod(0o1777)
    for alias,target in [('sh','dash'),('vi','vim.tiny'),('awk','gawk')]:
        if (root/'usr/bin'/target).is_file():link(root,'usr/bin/'+alias,target)
    # BLAS/LAPACK SONAME links: Debian ships real libs under blas/ and lapack/
    # subdirectories and relies on update-alternatives (maintainer scripts never
    # run here). numpy/scipy link against the plain SONAME — provide it.
    for soname,subdir in [('libblas.so.3','blas'),('liblapack.so.3','lapack')]:
        real=root/'usr/lib/x86_64-linux-gnu'/subdir/soname
        if real.is_file():library_soname_link(root,'usr/lib/x86_64-linux-gnu/'+soname,subdir+'/'+soname)
    # Compiled locale data: the locked pipeline never runs 'locales' postinst,
    # so C.UTF-8 (used by every login shell) has no compiled definition and bash
    # prints 'setlocale: cannot change locale (C.UTF-8)' on every invocation.
    # Generate the minimal C.utf8 locale into the rootfs using localedef from
    # the packaged glibc; only C.utf8 — no per-language archives.
    localedef=shutil.which('localedef')
    if localedef is None:raise ValueError('host localedef missing (needs glibc >= 2.41 host toolchain)')
    dest=root/'usr/lib/locale'
    if dest.is_symlink():raise ValueError('refusing symlink locale dir')
    dest.mkdir(parents=True,exist_ok=True)
    subprocess.run([localedef,'--no-archive','-i','C','-f','UTF-8',str(dest/'C.utf8')],env=dict(os.environ,LC_ALL='C'),check=True)
    put(root,'usr/share/xtos/locale-provision.json',json.dumps({'schema_version':1,'locales':['C.utf8'],'source':'host localedef (glibc)','reason':'bash setlocale warning without compiled C.UTF-8'},indent=2)+'\n')
    put(root,'etc/profile.d/xtos-developer.sh','export LANG=C.UTF-8\nexport LC_ALL=C.UTF-8\nexport GOTOOLCHAIN=local\nexport GOROOT=/usr/lib/go\n')
    # Non-login shells (dropbear ssh exec) never source profile.d; wrap go/gofmt
    # so the toolchain resolves GOROOT itself. Real binaries live under
    # /usr/lib/go-1.24/bin; /usr/lib/go is a symlink created by the package.
    for wrapper,target in [('go','/usr/lib/go/bin/go'),('gofmt','/usr/lib/go/bin/gofmt')]:
        real=safe_path(root,'usr/lib/go/bin/'+wrapper)
        if real.is_file():
            script='#!/bin/sh\n'+('if [ -z "$GOROOT" ]; then export GOROOT=/usr/lib/go; fi\n' if wrapper=='go' else '')+'exec %s "$@"\n'%target
            expected_link=None
            link_path=root/('usr/bin/'+wrapper)
            if link_path.is_symlink():
                resolved=(link_path.parent/os.readlink(link_path)).resolve()
                real_resolved=real.resolve()
                if resolved!=real_resolved:raise ValueError(f'packaged {wrapper} symlink does not point at the wrapped binary')
                expected_link=os.readlink(link_path)
            put(root,'usr/bin/'+wrapper,script,0o755,expect_symlink_target=expected_link)
    # Disable local collection/sidecar activity in the offline image and new-user template.
    put(root,'root/.config/go/telemetry/mode','off\n')
    put(root,'etc/skel/.config/go/telemetry/mode','off\n')
    # The base package set is a rootfs payload, not a Debian installed system.
    # Never configure an unlocked login account as a side-effect of extraction.
    if not (root/'etc/passwd').exists():put(root,'etc/passwd','root:x:0:0:root:/root:/bin/sh\n')
    if not (root/'etc/group').exists():put(root,'etc/group','root:x:0:\n')
    if not (root/'etc/shadow').exists():put(root,'etc/shadow','root:!:0:0:99999:7:::\n',0o600)
    cert_dir=safe_path(root,'usr/share/ca-certificates/mozilla')
    certs=sorted(cert_dir.glob('*.crt')) if cert_dir.is_dir() else []
    if not certs:raise ValueError('packaged CA certificates missing')
    parts=[]
    for cert in certs:
        if not cert.resolve().is_relative_to(root):raise ValueError('CA path escapes rootfs')
        parts.append(cert.read_text().rstrip()+'\n')
    bundle=''.join(parts)
    put(root,'etc/ssl/certs/ca-certificates.crt',bundle)
    result={'schema_version':1,'status':'configured','certificate_count':len(certs),'ca_bundle_sha256':hashlib.sha256(bundle.encode()).hexdigest(),
       'postprocessing':'XTOS explicit provisioning; no package maintainer scripts',
       'limitations':['No running services or time sync configured in this userspace layer','No DNS server, password or private key copied from host']}
    put(root,'usr/share/xtos/userspace-provision.json',json.dumps(result,indent=2,sort_keys=True)+'\n')
    print(json.dumps(result,sort_keys=True))


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--rootfs',required=True,type=Path)
    args=p.parse_args()
    try:configure(args.rootfs)
    except (OSError,ValueError) as exc:p.error(str(exc))
    return 0

if __name__=='__main__':raise SystemExit(main())
