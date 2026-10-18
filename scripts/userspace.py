#!/usr/bin/env python3
"""Offline locked Debian assembly and root-scoped ELF inspection (stdlib)."""
import argparse
import hashlib
import json
import os
import re
from urllib.parse import urlsplit
from pathlib import Path
import shutil
import subprocess
import tempfile
import tarfile
import stat
import posixpath
from typing import NoReturn

REPO = Path(__file__).resolve().parents[1]
MANIFEST = 'usr/share/xtos/userspace-manifest.json'
MACHINES = {'x86_64': 62, 'arm64': 183, 'riscv64': 243}
DEB_ARCHES = {'x86_64': 'amd64', 'arm64': 'arm64', 'riscv64': 'riscv64'}


class UserspaceError(ValueError):
    pass


def run(args):
    result = subprocess.run(args, capture_output=True, env={**os.environ, 'LC_ALL': 'C'})
    if result.returncode:
        raise UserspaceError(result.stderr.decode(errors='replace').strip())
    return result.stdout


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def load_lock(path):
    lock = json.loads(Path(path).read_text())
    if not isinstance(lock, dict) or type(lock.get('schema_version')) is not int or lock['schema_version'] != 1:
        raise UserspaceError('lock schema_version must be 1')
    if lock.get('architecture') not in MACHINES or lock.get('package_format') != 'deb':
        raise UserspaceError('unsupported lock architecture or package_format')
    if not isinstance(lock.get('packages'), list) or not lock['packages']:
        raise UserspaceError('lock packages must be a nonempty list')
    names, filenames = set(), set()
    for package in lock['packages']:
        if not isinstance(package, dict):
            raise UserspaceError('invalid package record')
        for field in ('name', 'version', 'architecture', 'filename', 'sha256', 'source_url'):
            if not isinstance(package.get(field), str) or not package[field]:
                raise UserspaceError(f'invalid package {field}')
        if not re.fullmatch(r'[a-z0-9][a-z0-9+.-]*', package['name']):
            raise UserspaceError('invalid Debian package name')
        filename = package['filename']
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+:~%-]*\.deb', filename):
            raise UserspaceError('filename must be a safe .deb basename')
        if package['name'] in names or filename in filenames:
            raise UserspaceError('duplicate package name or filename')
        names.add(package['name']); filenames.add(filename)
        if package['architecture'] not in (DEB_ARCHES[lock['architecture']], 'all'):
            raise UserspaceError(f'package architecture mismatch: {package["name"]}')
        if not re.fullmatch(r'[0-9a-f]{64}', package['sha256']):
            raise UserspaceError('sha256 must contain 64 lowercase hex digits')
        if type(package.get('size')) is not int or package['size'] <= 0:
            raise UserspaceError('package size must be a positive integer')
        url = urlsplit(package['source_url'])
        if url.scheme != 'https' or not url.netloc:
            raise UserspaceError('source_url must be an HTTPS URL')
    return lock


def validate_output(output):
    if os.path.lexists(output):
        raise UserspaceError('output target already exists')
    resolved = output.resolve()
    scratch = Path(os.environ.get('TMPDIR', '/root/.hermes/cache/scratch')).resolve()
    build = REPO / '.build'
    allowed_build = ((resolved.parent == build and resolved.name.startswith('userspace-'))
                     or resolved.is_relative_to(build / 'userspace') and resolved != build / 'userspace')
    allowed_scratch = resolved != scratch and resolved.is_relative_to(scratch)
    if not (allowed_build or allowed_scratch):
        raise UserspaceError('output must be a fresh .build/userspace-* or scratch directory')
    if not output.parent.is_dir() or output.parent.resolve() != output.parent:
        raise UserspaceError('output parent must exist and contain no symlinks')


def preflight(lock, packages_dir):
    packages_dir = Path(packages_dir).resolve()
    for package in lock['packages']:
        deb = packages_dir / package['filename']
        if deb.is_symlink() or not deb.is_file():
            raise UserspaceError(f'missing or symlinked package: {package["filename"]}')
        if deb.stat().st_size != package['size'] or sha256(deb) != package['sha256']:
            raise UserspaceError(f'package hash/size mismatch: {package["filename"]}')
        for field, control in (('name', 'Package'), ('version', 'Version'), ('architecture', 'Architecture')):
            actual = run(['dpkg-deb', '-f', str(deb), control]).decode().strip()
            if actual != package[field]:
                raise UserspaceError(f'package control {control} mismatch: {package["filename"]}')
    combined = {}
    for package in lock['packages']:
        for name, entry in inspect_payload(packages_dir / package['filename']).items():
            if name in combined and combined[name]['type'] != entry['type']:
                raise UserspaceError(f'conflicting archive types: {name}')
            combined[name] = entry
    for name in combined:
        parts = name.split('/')
        for count in range(1, len(parts)):
            parent = '/'.join(parts[:count])
            if parent in combined and combined[parent]['type'] != 'directory':
                raise UserspaceError(f'payload traverses non-directory parent: {name}')
    parts = MANIFEST.split('/')
    for count in range(1, len(parts) + 1):
        path = '/'.join(parts[:count])
        if path in combined and (count == len(parts) or combined[path]['type'] != 'directory'):
            raise UserspaceError(f'package claims reserved manifest path: {path}')
    return packages_dir


def archive_path(name):
    if not name or name.startswith('/') or '..' in name.split('/'):
        raise UserspaceError(f'unsafe archive path: {name}')
    return posixpath.normpath(name).removeprefix('./')


def scoped_parts(path, parent=()):
    parts = [] if path.startswith('/') else list(parent)
    for part in path.split('/'):
        if part in ('', '.'):
            continue
        if part == '..':
            if not parts:
                raise UserspaceError(f'path escapes rootfs: {path}')
            parts.pop()
        else:
            parts.append(part)
    return parts


def inspect_payload(deb):
    """Stream data.tar metadata and reject unsafe extraction semantics."""
    process = subprocess.Popen(['dpkg-deb', '--fsys-tarfile', str(deb)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    entries = {}
    try:
        with tarfile.open(fileobj=process.stdout, mode='r|') as archive:
            for member in archive:
                name = archive_path(member.name)
                if name == '.':
                    if not member.isdir():
                        raise UserspaceError('archive root must be a directory')
                    continue
                if not (member.isdir() or member.isfile() or member.issym() or member.islnk()):
                    raise UserspaceError(f'special archive entry: {name}')
                if name in entries and not (member.isdir() and entries[name]['type'] == 'directory'):
                    raise UserspaceError(f'duplicate archive path: {name}')
                kind = ('directory' if member.isdir() else 'symlink' if member.issym()
                        else 'hardlink' if member.islnk() else 'file')
                entry = {'type': kind, 'mode': member.mode & 0o7777}
                if member.issym():
                    scoped_parts(member.linkname, name.split('/')[:-1])
                    entry['target'] = member.linkname
                elif member.islnk():
                    entry['target'] = archive_path(member.linkname)
                entries[name] = entry
        stderr = process.stderr.read()
        if process.wait():
            raise UserspaceError(stderr.decode(errors='replace'))
    finally:
        if process.poll() is None:
            process.kill(); process.wait()
        process.stdout.close(); process.stderr.close()
    for name, entry in entries.items():
        parts = name.split('/')
        for count in range(1, len(parts)):
            parent = '/'.join(parts[:count])
            if parent in entries and entries[parent]['type'] != 'directory':
                raise UserspaceError(f'archive non-directory parent: {name}')
        if entry['type'] == 'hardlink':
            target = entries.get(entry['target'])
            if target is None or target['type'] != 'file':
                raise UserspaceError(f'hardlink must target an archive regular file: {name}')
    return entries


def merge_payload(package_root, stage):
    for current, dirs, files in os.walk(package_root, followlinks=False):
        for name in sorted(dirs + files):
            source = Path(current) / name
            target = stage / source.relative_to(package_root)
            parent = target.parent
            while parent != stage:
                if parent.is_symlink():
                    raise UserspaceError(f'payload traverses symlink parent: {target}')
                parent = parent.parent
            exists = os.path.lexists(target)
            if source.is_symlink():
                if exists:
                    if not target.is_symlink() or os.readlink(target) != os.readlink(source):
                        raise UserspaceError(f'conflicting symlink: {target}')
                else:
                    target.symlink_to(os.readlink(source))
            elif source.is_dir():
                if exists and (target.is_symlink() or not target.is_dir()):
                    raise UserspaceError(f'conflicting directory: {target}')
                target.mkdir(exist_ok=True)
                target.chmod(stat.S_IMODE(source.stat().st_mode))
            else:
                if exists:
                    if (target.is_symlink() or not target.is_file() or sha256(target) != sha256(source)
                            or stat.S_IMODE(target.stat().st_mode) != stat.S_IMODE(source.stat().st_mode)):
                        raise UserspaceError(f'conflicting file: {target}')
                else:
                    shutil.copy2(source, target)


def inventory(root):
    result = []
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            path = Path(current) / name
            entry = {'path': '/' + path.relative_to(root).as_posix()}
            if path.is_symlink():
                entry.update(type='symlink', target=os.readlink(path))
            elif path.is_dir():
                entry.update(type='directory', mode=stat.S_IMODE(path.stat().st_mode))
            else:
                entry.update(type='file', size=path.stat().st_size, sha256=sha256(path),
                             mode=stat.S_IMODE(path.stat().st_mode))
            result.append(entry)
    return sorted(result, key=lambda item: item['path'])


def assemble(lock_path, packages_dir, output_dir, verify=False, elf_data_policy=None):
    lock = load_lock(lock_path)
    output = Path(os.path.abspath(output_dir))
    validate_output(output)
    packages_dir = preflight(lock, packages_dir)
    stage = Path(tempfile.mkdtemp(prefix='.' + output.name + '-stage-', dir=output.parent))
    stage.chmod(0o755)
    try:
        packages = []
        for package in lock['packages']:
            deb = Path(packages_dir) / package['filename']
            with tempfile.TemporaryDirectory(prefix='xtos-package-', dir=stage.parent) as temp:
                package_root = Path(temp)
                run(['dpkg-deb', '-x', str(deb), str(package_root)])
                entries = inventory(package_root)
                merge_payload(package_root, stage)
            packages.append({'package': package, 'files': entries})
        manifest = {'schema_version': 1, 'architecture': lock['architecture'],
                    'lock_sha256': sha256(lock_path), 'lock': lock, 'packages': packages,
                    'assembly': 'dpkg-deb -x only; maintainer scripts were not executed'}
        target = stage / MANIFEST
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')
        if verify:
            report = verify_rootfs(stage, lock_path, elf_data_policy)
            if report['status'] != 'pass':
                raise UserspaceError('ELF verification failed: ' + json.dumps(report, sort_keys=True))
        if os.path.lexists(output):
            raise UserspaceError('output appeared while staging; refusing to replace it')
        os.rename(stage, output)
        return manifest
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def resolve_root(root, target):
    """Resolve target symlinks as a kernel inside root, never on the host."""
    pending = target.split('/')
    parts = []
    links = 0
    while pending:
        part = pending.pop(0)
        if part in ('', '.'):
            continue
        if part == '..':
            if not parts:
                raise UserspaceError(f'path escapes rootfs: {target}')
            parts.pop()
            continue
        candidate = root.joinpath(*parts, part)
        if candidate.is_symlink():
            links += 1
            if links > 40:
                raise UserspaceError(f'symlink loop: {target}')
            link = os.readlink(candidate)
            if link.startswith('/'):
                parts = []
            pending = link.split('/') + pending
        else:
            parts.append(part)
    return root.joinpath(*parts)


def elf_header(path):
    with path.open('rb') as stream:
        header = stream.read(20)
    if not header.startswith(b'\x7fELF'):
        return None
    if len(header) != 20 or header[4] not in (1, 2) or header[5] not in (1, 2):
        raise UserspaceError('malformed ELF header')
    return {'machine': int.from_bytes(header[18:20], 'little' if header[5] == 1 else 'big'),
            'class': header[4], 'byte_order': header[5]}


def load_elf_data_policy(policy_path, root, manifest):
    """Validate explicit Go source-data ELF exceptions against the file manifest."""
    if policy_path is None:
        return {}

    def invalid(reason) -> NoReturn:
        raise UserspaceError('ELF data policy: ' + reason)

    try:
        policy = json.loads(Path(policy_path).read_text())
    except (OSError, ValueError) as error:
        invalid(f'cannot read JSON: {error}')
    if not isinstance(policy, dict):
        invalid('top-level value must be an object')
    if set(policy) != {'schema_version', 'architecture', 'data_files'}:
        invalid('top-level fields are invalid')
    if type(policy['schema_version']) is not int or policy['schema_version'] != 1:
        invalid('schema_version must be 1')
    if policy['architecture'] != manifest.get('architecture'):
        invalid('architecture does not match manifest')
    if not isinstance(policy['data_files'], list):
        invalid('data_files must be a list')

    owners = {}
    packages = manifest.get('packages')
    if not isinstance(packages, list):
        invalid('manifest package inventory is missing')
    for package_record in packages:
        if not isinstance(package_record, dict) or not isinstance(package_record.get('package'), dict):
            continue
        name = package_record['package'].get('name')
        for file_record in package_record.get('files', []):
            if isinstance(file_record, dict) and isinstance(file_record.get('path'), str):
                owners.setdefault(file_record['path'], []).append((name, file_record))

    triplet = {'x86_64': 'x86_64-linux-gnu', 'arm64': 'aarch64-linux-gnu',
               'riscv64': 'riscv64-linux-gnu'}[manifest['architecture']]
    entries = {}
    for entry in policy['data_files']:
        if not isinstance(entry, dict) or set(entry) != {'path', 'sha256', 'package', 'purpose'}:
            invalid('data file fields are invalid')
        path = entry['path']
        digest = entry['sha256']
        package = entry['package']
        purpose = entry['purpose']
        if not isinstance(path, str) or not (
                re.fullmatch(r'/usr/share/go-([0-9]+(?:\.[0-9]+)*)/src/.+', path)
                or (path.startswith('/usr/lib/perf-core/') and path.count('/') == 4)
                or (path.startswith(f'/usr/lib/{triplet}/caca/') and path.count('/') == 5)):
            invalid('path must be an exact file below /usr/share/go-<version>/src, '
                    f'/usr/lib/perf-core, or the libcaca optional-dlopen plugin '
                    f'directory /usr/lib/{triplet}/caca')
        if (path != posixpath.normpath(path) or '\\' in path or any(
                part in ('', '.', '..') for part in path.split('/')[1:])
                or any(char in path for char in '*?[]{}')
                or any(ord(char) < 0x20 for char in path)):
            invalid(f'path is not canonical or contains a pattern: {path}')
        if path.startswith('/usr/lib/perf-core/'):
            if package != 'linux-perf':
                invalid(f'perf-core data must belong to linux-perf: {path}')
            if purpose != 'perf-i386-vdso-helper':
                invalid(f'unsupported purpose for {path}')
        elif path.startswith(f'/usr/lib/{triplet}/caca/'):
            # libcaca's GL output plugin is dlopened only on explicit request;
            # its libGLU/libglut dependencies are Recommends and intentionally
            # absent from the image, so it is exempt from runtime closure.
            if package != 'libcaca0':
                invalid(f'caca plugin data must belong to libcaca0: {path}')
            if purpose != 'optional-dlopen-plugin-deps-are-recommends':
                invalid(f'unsupported purpose for {path}')
        elif package == 'golang-1.24-src' and path.startswith('/usr/share/go-1.24/src/'):
            if purpose not in ('go-test-fixture', 'go-cross-compile-object'):
                invalid(f'unsupported purpose for {path}')
        else:
            invalid(f'path/package must identify golang-1.24-src source data or '
                    f'linux-perf helper data: {path}')
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            invalid(f'invalid sha256 for {path}')
        if path in entries:
            invalid(f'duplicate path: {path}')
        logical = root.joinpath(*path.lstrip('/').split('/'))
        try:
            resolved = resolve_root(root, path)
        except (OSError, ValueError, UserspaceError) as error:
            invalid(f'cannot resolve {path}: {error}')
        if resolved != logical or not logical.is_file() or logical.is_symlink():
            invalid(f'file is missing, symlinked, or non-regular: {path}')
        mode = 0
        actual_hash = ''
        try:
            mode = logical.stat().st_mode
            actual_hash = sha256(logical)
        except (OSError, ValueError) as error:
            invalid(f'cannot inspect {path}: {error}')
        if not stat.S_ISREG(mode) or actual_hash != digest:
            invalid(f'file type or hash mismatch: {path}')
        try:
            header = elf_header(logical)
        except (OSError, ValueError, UserspaceError) as error:
            invalid(f'cannot inspect ELF data file {path}: {error}')
        if header is None:
            invalid(f'file is not ELF: {path}')
        matching = [record for owner, record in owners.get(path, [])
                    if owner == package and record.get('type') == 'file'
                    and record.get('sha256') == digest]
        if not matching:
            invalid(f'manifest package inventory does not own matching hash: {path}')
        entries[path] = dict(entry)
    return entries


def reject_runtime_elf_data_aliases(root, data_files):
    """Walk runtime names through target-root links, including directory aliases."""
    if not data_files:
        return []
    aliases = []
    pending: list[tuple[str, tuple[str, ...]]] = [
        (prefix, ()) for prefix in
        ('/bin', '/sbin', '/usr/bin', '/usr/sbin', '/usr/libexec')
    ]
    examined = 0
    while pending:
        logical, ancestors = pending.pop()
        examined += 1
        if examined > 100000:
            raise UserspaceError('runtime alias traversal exceeds its path limit')
        try:
            resolved = resolve_root(root, logical)
            canonical = '/' + resolved.relative_to(root).as_posix()
            if canonical in data_files:
                if logical != canonical:
                    aliases.append(logical)
                continue
            if not resolved.is_dir() or canonical in ancestors:
                continue
            # Never let host symlink semantics enter the walk. resolve_root treats
            # every absolute target as belonging to this rootfs; ancestors bound loops.
            chain = (*ancestors, canonical)
            for child in sorted(resolved.iterdir(), key=lambda path: path.name):
                pending.append((logical.rstrip('/') + '/' + child.name, chain))
        except (OSError, UserspaceError):
            continue
    return sorted(set(aliases))


def verify_rootfs(rootfs, lock_path=None, elf_data_policy=None):
    root = Path(rootfs).resolve()
    report = {'schema_version': 1, 'status': 'fail', 'architecture': None, 'elf_count': 0,
              'runtime_elf_count': 0, 'elf_data_files': [],
              'verification_level': 'ELF closure paths and interpreter; no ABI symbol-version proof',
              'errors': []}
    errors = report['errors']
    def fail(path, reason):
        errors.append({'path': path, 'reason': reason})
    try:
        if not root.is_dir() or root == Path('/'):
            raise UserspaceError('rootfs must be an isolated directory')
        manifest_path = resolve_root(root, '/' + MANIFEST)
        manifest = json.loads(manifest_path.read_text())
        architecture = manifest['architecture']
        if architecture not in MACHINES:
            raise UserspaceError('unsupported manifest architecture')
        report['architecture'] = architecture
        data_files = load_elf_data_policy(elf_data_policy, root, manifest)
        for alias in reject_runtime_elf_data_aliases(root, data_files):
            fail(alias, 'ELF data policy: runtime alias resolves into declared nonruntime data')
        if lock_path:
            lock = load_lock(lock_path)
            if lock['architecture'] != architecture or manifest.get('lock_sha256') != sha256(lock_path) or manifest.get('lock') != lock:
                fail('/' + MANIFEST, 'lock identity does not match manifest')
        triplet = {'x86_64': 'x86_64-linux-gnu', 'arm64': 'aarch64-linux-gnu',
                   'riscv64': 'riscv64-linux-gnu'}[architecture]
        defaults = ['/lib/' + triplet, '/usr/lib/' + triplet, '/lib', '/usr/lib', '/lib64', '/usr/lib64']
        inspected = set()
        for current, dirs, files in os.walk(root, followlinks=False):
            for name in sorted(dirs + files):
                logical = '/' + (Path(current) / name).relative_to(root).as_posix()
                try:
                    path = resolve_root(root, logical)
                    if not path.exists() or path.is_dir():
                        continue  # Debian documentation/development symlinks may be dangling.
                    if not path.is_file():
                        fail(logical, 'special file in rootfs')
                        continue
                    if path in inspected:
                        continue
                    if logical.startswith('/usr/lib/firmware/') or logical.startswith('/lib/firmware/'):
                        # Firmware blobs are ELF payloads for OTHER architectures
                        # (Qualcomm Hexagon DSPs, ARM MCUs). They are device data,
                        # not host runtime binaries — excluded from closure checks.
                        continue
                    header = elf_header(path)
                    if header is None:
                        continue
                    inspected.add(path)
                    report['elf_count'] += 1
                    canonical = '/' + path.relative_to(root).as_posix()
                    if canonical in data_files:
                        report['elf_data_files'].append({**data_files[canonical],
                                                         'classification': 'nonruntime_data'})
                        continue
                    report['runtime_elf_count'] += 1
                    if header['machine'] != MACHINES[architecture]:
                        fail(logical, f'wrong ELF machine {header["machine"]}; expected {MACHINES[architecture]}')
                    if header['class'] != 2 or header['byte_order'] != 1:
                        fail(logical, 'ELF must be 64-bit little-endian')
                    detail = run(['readelf', '-W', '-l', '-d', str(path)]).decode(errors='replace')
                    loader = re.search(r'Requesting program interpreter:\s*([^\]]+)', detail)
                    if loader:
                        interpreter = loader.group(1).strip()
                        if not interpreter.startswith('/'):
                            fail(logical, 'interpreter is not an absolute target path')
                        else:
                            loader_path = resolve_root(root, interpreter)
                            if not loader_path.is_file():
                                fail(logical, f'missing interpreter: {interpreter}')
                            elif '/' + loader_path.relative_to(root).as_posix() in data_files:
                                fail(logical, f'interpreter resolves to nonruntime data: {interpreter}')
                            elif elf_header(loader_path) is None:
                                fail(logical, f'interpreter is not ELF: {interpreter}')
                    origin = posixpath.dirname(canonical)
                    search = []
                    for tag in re.findall(r'\((?:RUNPATH|RPATH)\).*?\[([^\]]*)\]', detail):
                        for entry in tag.split(':'):
                            if entry == '':
                                continue  # empty RUNPATH list / separators are no-ops
                            expanded = entry.replace('${ORIGIN}', origin).replace('$ORIGIN', origin)
                            if not expanded.startswith('/') or '$' in expanded:
                                fail(logical, f'unsafe or unsupported RPATH/RUNPATH: {entry}')
                                continue
                            directory = resolve_root(root, expanded)
                            if not directory.is_dir():
                                # The dynamic loader silently ignores RUNPATH
                                # entries that do not exist (Debian libLLVM
                                # ships a harmless $ORIGIN/../lib leftover).
                                # A missing search hint is not a closure
                                # failure; an unresolvable NEEDED is.
                                continue
                            search.append(expanded)
                    for needed in re.findall(r'\(NEEDED\).*?\[([^\]]+)\]', detail):
                        if '/' in needed:
                            expanded = needed.replace('${ORIGIN}', origin).replace('$ORIGIN', origin)
                            candidates = [expanded] if expanded.startswith('/') and '$' not in expanded else []
                        else:
                            candidates = [directory + '/' + needed for directory in search + defaults]
                        found = False
                        for candidate in candidates:
                            library = resolve_root(root, candidate)
                            if library.is_file():
                                if '/' + library.relative_to(root).as_posix() in data_files:
                                    fail(logical, f'NEEDED library resolves to nonruntime data: {candidate}')
                                elif elf_header(library) is None:
                                    fail(logical, f'NEEDED library is not ELF: {candidate}')
                                found = True
                                break
                        if not found:
                            fail(logical, f'missing NEEDED library in target rootfs: {needed}')
                except (UserspaceError, OSError, ValueError) as error:
                    fail(logical, str(error))
    except (UserspaceError, OSError, ValueError, KeyError, TypeError) as error:
        fail('/', str(error))
    report['elf_data_files'] = sorted(report['elf_data_files'], key=lambda item: item['path'])
    report['errors'] = sorted(errors, key=lambda item: (item['path'], item['reason']))
    report['status'] = 'fail' if errors else 'pass'
    return report


def verify_main(argv=None):
    parser = argparse.ArgumentParser(description='Inspect ELF closure using target files and readelf only')
    parser.add_argument('--rootfs', required=True)
    parser.add_argument('--lock')
    parser.add_argument('--report')
    parser.add_argument('--elf-data-policy', help='hash-bound Go vendor ELF data policy JSON')
    args = parser.parse_args(argv)
    report = verify_rootfs(args.rootfs, args.lock, args.elf_data_policy)
    serialized = json.dumps(report, sort_keys=True, indent=2) + '\n'
    if args.report:
        try:
            Path(args.report).write_text(serialized)
        except OSError as error:
            print(f'cannot write verification report: {error}', file=__import__('sys').stderr)
            return 1
    print(serialized, end='')
    return 0 if report['status'] == 'pass' else 1


def build_main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lock', required=True)
    parser.add_argument('--packages-dir', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('--elf-data-policy', help='hash-bound Go vendor ELF data policy JSON')
    args = parser.parse_args(argv)
    try:
        if args.elf_data_policy and not args.verify:
            raise UserspaceError('--elf-data-policy requires --verify')
        manifest = assemble(args.lock, args.packages_dir, args.output_dir, args.verify,
                            args.elf_data_policy)
        print(json.dumps({'status': 'pass', 'architecture': manifest['architecture'],
                          'package_count': len(manifest['packages']), 'rootfs': args.output_dir}))
        return 0
    except (UserspaceError, OSError, ValueError, KeyError) as error:
        print(f'userspace assembly failed: {error}', file=__import__('sys').stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(build_main())
