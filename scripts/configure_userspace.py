#!/usr/bin/env python3
"""Provision a verified XTOS package layer without executing package scripts."""
import argparse
import hashlib
import json
from pathlib import Path
import os
from userspace_root import open_owned_root


def safe_path(root, relative):
    path=root/relative
    parent=path.parent.resolve()
    if not parent.is_relative_to(root):raise ValueError(f'parent escapes rootfs: {relative}')
    return path


def put(root, relative, text, mode=0o644):
    path=safe_path(root,relative)
    if path.is_symlink():raise ValueError(f'refusing symlink output: {relative}')
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
    put(root,'etc/profile.d/xtos-developer.sh','export LANG=C.UTF-8\nexport LC_ALL=C.UTF-8\nexport GOTOOLCHAIN=local\nexport GOROOT=/usr/lib/go\n')
    # Non-login shells (dropbear ssh exec) never source profile.d; wrap go/gofmt
    # so the toolchain resolves GOROOT itself. Real binaries live under
    # /usr/lib/go-1.24/bin; /usr/lib/go is a symlink created by the package.
    for wrapper,target in [('go','/usr/lib/go/bin/go'),('gofmt','/usr/lib/go/bin/gofmt')]:
        real=safe_path(root,'usr/lib/go/bin/'+wrapper)
        if real.is_file():
            script='#!/bin/sh\n'+('if [ -z "$GOROOT" ]; then export GOROOT=/usr/lib/go; fi\n' if wrapper=='go' else '')+'exec %s "$@"\n'%target
            put(root,'usr/bin/'+wrapper,script,0o755)
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
