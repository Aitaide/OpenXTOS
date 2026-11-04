#!/usr/bin/env python3
"""Build v1.0.2 media with an explicit, separately versioned release recipe."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
from preserve_tracked import preserved

SOURCE_SHA = 'c24e923717670c4724cdaf24c7c105caee8dc2fb'
TAG = 'v1.0.2'
TOOLS = Path(__file__).resolve().parent


def run(args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def require_source(root):
    if git(root, 'rev-parse', 'HEAD') != SOURCE_SHA:
        raise RuntimeError('wrong source commit')
    if git(root, 'status', '--porcelain', '--untracked-files=all'):
        raise RuntimeError('source must remain clean')
    if 'XTOS_VERSION=1.0.2' not in (root / 'VERSION').read_text():
        raise RuntimeError('wrong source version')


def release_overlays():
    return [
        {'recipe_source': 'release-tools/xtos-install', 'packaged_path': '/usr/sbin/xtos-install', 'sha256': digest(TOOLS / 'xtos-install'), 'scope': 'UEFI grub-install adds --no-uefi-secure-boot to generate the disk-specific unsigned loader; core source tag is unchanged'},
        {'recipe_source': 'release-tools/cyton-read-bounds.patch', 'source_path': 'tools/xtos-cyton/cyton_diagnostic_serial.c', 'packaged_path': '/usr/bin/bci', 'sha256': digest(TOOLS / 'cyton-read-bounds.patch'), 'scope': 'Use size_t for partial-frame length and explicitly reject invalid read bounds before adding bytes; applied only to isolated native build staging'}
    ]


def source_package(root, dist, docroot, arch):
    stage = dist / f'{arch}-source'
    stage.mkdir()
    project = stage / 'project-source'
    project.mkdir()
    archive = stage / 'project.tar'
    with archive.open('wb') as f:
        run(['git', '-C', root, 'archive', '--format=tar', f'--prefix=openxtos-{TAG}/', SOURCE_SHA], stdout=f)
    run(['tar', '-xf', archive, '-C', project])
    archive.unlink()
    shutil.copytree(TOOLS, stage / 'release-recipe', ignore=shutil.ignore_patterns('__pycache__'))
    recipe_sha = git(TOOLS, 'rev-parse', 'HEAD')
    (stage / 'PROVENANCE.json').write_text(json.dumps({'source_commit': SOURCE_SHA, 'source_tag': TAG, 'recipe_commit': recipe_sha, 'recipe_repository': 'https://github.com/Aitaide/OpenXTOS', 'recipe_branch': 'release/v1.0.2-images', 'applied_release_overlays': release_overlays() if arch == 'x86_64' else [], 'scope': 'Tagged core system source plus explicitly listed release installer overlay; not the private developer Live image.'}, indent=2) + '\n')
    for name in ['HOST_PACKAGES.tsv', 'SOURCE_PACKAGES.tsv']:
        shutil.copy2(docroot / name, stage / name)
    for name in ['third-party-copyright', 'third-party-common-licenses']:
        shutil.copytree(docroot / name, stage / name)
    env = dict(os.environ, XTOS_SOURCE_ARCHIVE='ubuntu')
    run(['python3', TOOLS / 'fetch_distribution_sources.py', stage / 'SOURCE_PACKAGES.tsv', stage / 'runtime-sources'], env=env)
    target = dist / f'openxtos-{TAG}-{arch}-corresponding-source.tar.gz'
    run(['tar', '-czf', target, '-C', stage, '.'])
    return target


def build_x86(root, dist):
    build = root / '.build/baremetal-release'
    env = dict(os.environ, XTOS_RELEASE_SOURCE_ROOT=str(root), XTOS_BAREMETAL_BUILD_ROOT=str(build), XTOS_BUILD_ID='release-1.0.2', XTOS_SOURCE_STATE='clean', JOBS=os.environ.get('JOBS', '4'))
    run(['bash', TOOLS / 'build_baremetal.sh', '--public'], env=env)
    require_source(root)
    iso = build / 'images/xtos-installer-public-x86_64.iso'
    env.update(XTOS_INSTALLER_ISO=str(iso), QEMU_TIMEOUT='1800')
    run(['python3', TOOLS / 'verify_baremetal.py'], env=env)
    manifest = json.loads((build / 'images/build-manifest.json').read_text())
    if manifest['git_head'] != SOURCE_SHA or manifest['dirty']:
        raise RuntimeError('incorrect source provenance')
    if manifest['installer_artifact']['sha256'] != digest(iso):
        raise RuntimeError('ISO digest mismatch')
    if (build / 'rootfs/root/.ssh/authorized_keys').read_bytes():
        raise RuntimeError('public image contains authorized SSH key')
    verify_iso(build)
    output = dist / f'openxtos-{TAG}-x86_64-installer.iso'
    shutil.copy2(iso, output)
    shutil.copy2(build / 'images/build-manifest.json', dist / 'x86_64-build-manifest.json')
    shutil.copy2(build / 'evidence/results.json', dist / 'x86_64-qemu-results.json')
    source_package(root, dist, build / 'rootfs/usr/share/doc/openxtos', 'x86_64')


def verify_iso(build):
    readback = build / 'iso-readback'
    readback.mkdir()
    iso = build / 'images/xtos-installer-public-x86_64.iso'
    run(['xorriso', '-osirrox', 'on', '-indev', iso, '-extract', '/boot/bzImage', readback / 'bzImage', '-extract', '/boot/initramfs.gz', readback / 'initramfs.gz'])
    for actual, expected in [('bzImage', 'xtos-bzImage'), ('initramfs.gz', 'installer.cpio.gz')]:
        if digest(readback / actual) != digest(build / 'images' / expected):
            raise RuntimeError('ISO embedded payload mismatch: ' + actual)
    if '1.0.2' not in (build / 'rootfs/etc/os-release').read_text():
        raise RuntimeError('packaged root has wrong version')


def build_arm64(root, dist):
    env = dict(os.environ, XTOS_BUILD_ID='release-1.0.2', XTOS_SOURCE_STATE='clean', JOBS=os.environ.get('JOBS', '4'))
    with preserved(root):
        run(['bash', root / 'scripts/build_system_arm64.sh'], env=env)
    require_source(root)
    run(['bash', root / 'scripts/verify_system_arm64.sh'], env=dict(env, QEMU_TIMEOUT='420'))
    run(['bash', root / 'scripts/verify_guest_arm64.sh'], env=dict(env, QEMU_TIMEOUT='420'))
    b = root / '.build'
    run(['python3', TOOLS / 'record_static_runtime.py', 'aarch64-linux-gnu-gcc', b / 'host-files-arm64.txt'])
    run(['python3', TOOLS / 'collect_distribution_notices.py', root, b / 'rootfs-arm64', b / 'host-files-arm64.txt', 'arm64 bundle'])
    # Repack the shipped normal root, not the guest-check root.
    run(['bash', '-c', 'cd "$1"; find . -print0 | LC_ALL=C sort -z | cpio --null -o -H newc --owner=0:0 --reproducible 2>/dev/null | gzip -n -6 > "$2"', 'pack', b / 'rootfs-arm64', b / 'initramfs-arm64.cpio.gz'])
    run(['bash', root / 'scripts/verify_system_arm64.sh'], env=dict(env, QEMU_TIMEOUT='420'))
    target = dist / 'arm64'
    target.mkdir()
    for src, name in [(b / 'kernel-install-arm64/Image', 'Image'), (b / 'initramfs-arm64.cpio.gz', 'initramfs-arm64.cpio.gz'), (b / 'kernel-build-arm64/.config', 'kernel.config')]:
        shutil.copy2(src, target / name)
    shutil.copytree(b / 'kernel-install-arm64/dtbs', target / 'dtbs')
    shutil.copytree(b / 'rootfs-arm64/usr/share/doc/openxtos', target / 'notices')
    shutil.copy2(TOOLS / 'ARM64-README.txt', target / 'README.txt')
    run(['tar', '-czf', dist / f'openxtos-{TAG}-arm64.tar.gz', '-C', dist, 'arm64'])
    source_package(root, dist, b / 'rootfs-arm64/usr/share/doc/openxtos', 'arm64')
    require_source(root)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('arch', choices=['x86_64', 'arm64'])
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--dist', type=Path, required=True)
    a = p.parse_args()
    root, dist = a.source.resolve(), a.dist.resolve()
    require_source(root)
    dist.mkdir(exist_ok=True, parents=True)
    budget_gib = {'kernel_and_modules': 7, 'userland_and_staging': 3, 'iso_and_readback': 3, 'corresponding_sources': 4, 'archives_and_test_disk': 4, 'reserve': 3}
    required_bytes = sum(budget_gib.values()) * 1024 ** 3
    (dist / 'space-budget.json').write_text(json.dumps({'components_gib': budget_gib, 'required_free_bytes': required_bytes, 'filesystem': str(dist)}, indent=2) + '\n')
    if shutil.disk_usage(dist).free < required_bytes:
        raise RuntimeError('insufficient space for the serialized release budget')
    (build_x86 if a.arch == 'x86_64' else build_arm64)(root, dist)
    require_source(root)
    print('release_artifacts=pass arch=' + a.arch)


if __name__ == '__main__':
    main()
