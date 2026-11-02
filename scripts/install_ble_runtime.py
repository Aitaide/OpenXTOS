#!/usr/bin/env python3
"""Install the optional, exact BLE dependencies into a fresh target only.

No pip, network, host package install, Bluetooth IPC or system service changes.
The target's own CPython is executed in chroot, before atomic publication.
"""
import argparse
import ctypes
import base64
import csv
import email
import io
import stat
import re
from urllib.parse import urlparse, unquote
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

DEFAULT_LOCK = Path(__file__).resolve().parents[1] / "system/packages/ble-runtime-cp313-x86_64.json"
PYTHON = "/usr/bin/python3.13"

PROBE = '''import importlib, json, pathlib, sys, sysconfig
base = pathlib.Path(__file__).resolve().parent
site = base / "site-packages"
sys.path[:] = [str(site)] + [p for p in sys.path if p and "site-packages" not in p and "dist-packages" not in p]
if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 13) or sysconfig.get_config_var("SOABI") != "cpython-313-x86_64-linux-gnu":
    raise RuntimeError("target ABI mismatch")
# Only import: never instantiate a client/bus or scan, connect, pair or send.
import dbus_fast
import importlib.metadata
from bleak import BleakClient
from bleak.backends.bluezdbus.client import BleakClientBlueZDBus
versions = {name: importlib.metadata.version(name) for name in ('bleak', 'dbus-fast')}
if BleakClientBlueZDBus.__module__ != 'bleak.backends.bluezdbus.client':
    raise RuntimeError('wrong Linux backend')
modules = []
for file in sorted((site / "dbus_fast").rglob("*.so")):
    relative = file.relative_to(site)
    name = ".".join(relative.parts[:-1] + (relative.name.split(".")[0],))
    mod = importlib.import_module(name)
    if pathlib.Path(mod.__file__).resolve() != file.resolve():
        raise RuntimeError("native module origin mismatch")
    modules.append(str(file))
if not modules or not pathlib.Path(dbus_fast.__file__).resolve().is_relative_to(site):
    raise RuntimeError("missing owned dbus_fast native closure")
if not pathlib.Path(sys.modules[BleakClient.__module__].__file__).resolve().is_relative_to(site):
    raise RuntimeError("BleakClient origin mismatch")
print(json.dumps({"status":"dependencies_ready", "python":sys.version, "soabi":sysconfig.get_config_var("SOABI"), "optimization":sys.flags.optimize, "versions":versions, "linux_backend":BleakClientBlueZDBus.__module__, "native_modules":modules, "physical_ble_verified":False, "bluez_verified":False, "no_action":True}))
'''


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key: ' + key)
        result[key] = value
    return result


def _relative(name, directory=False):
    if not isinstance(name, str) or not name or '\\' in name or '\x00' in name:
        raise ValueError('unsafe relative path')
    text = name[:-1] if directory and name.endswith('/') else name
    if any(part in ('', '.', '..') for part in text.split('/')):
        raise ValueError('unsafe relative path: ' + name)
    return text


def read_lock(path):
    raw = path.read_bytes()
    if len(raw) > 65536:
        raise ValueError('lock too large')
    lock = json.loads(raw, object_pairs_hook=_object)
    target = dict(implementation='cpython', python='3.13', abi='cp313',
                  architecture='x86_64', prefix='/opt/xtos-ble')
    if (not isinstance(lock, dict) or set(lock) != {'schema', 'target', 'packages'}
            or lock['schema'] != 'xtos.ble-runtime-lock.v1' or lock['target'] != target
            or not isinstance(lock['packages'], list) or len(lock['packages']) != 2):
        raise ValueError('unsupported BLE lock schema or target')
    names = set()
    fields = {'name', 'version', 'wheel', 'bytes', 'sha256', 'requires_python',
              'requires_dist', 'licenses', 'source_url'}
    for package in lock['packages']:
        if not isinstance(package, dict) or set(package) != fields:
            raise ValueError('invalid package fields')
        name = package['name']
        if name not in ('bleak', 'dbus-fast') or name in names:
            raise ValueError('invalid or duplicate package')
        names.add(name)
        if not isinstance(package['version'], str) or not re.fullmatch(r'[0-9]+(?:\.[0-9]+){2}', package['version']):
            raise ValueError('invalid package version')
        prefix = name.replace('-', '_') + '-' + package['version']
        wheel = _relative(package['wheel'])
        if '/' in wheel or not wheel.startswith(prefix + '-') or not wheel.endswith('.whl'):
            raise ValueError('invalid wheel filename')
        if name == 'bleak' and wheel != prefix + '-py3-none-any.whl':
            raise ValueError('unsupported Bleak wheel tag')
        if name == 'dbus-fast' and (not wheel.startswith(prefix + '-cp313-cp313-')
                or not re.fullmatch(r'(?:manylinux(?:2014|_2_[0-9]+)_x86_64\.?)+', wheel[len(prefix + '-cp313-cp313-'):-4])):
            raise ValueError('unsupported dbus-fast wheel tag')
        if (type(package['bytes']) is not int or not 0 < package['bytes'] <= 16000000
                or not isinstance(package['sha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', package['sha256'])):
            raise ValueError('invalid wheel hash/bytes')
        if not isinstance(package['source_url'], str):
            raise ValueError('invalid locked source URL type')
        url = urlparse(package['source_url'])
        if (url.scheme != 'https' or url.netloc != 'files.pythonhosted.org'
                or url.query or url.fragment or unquote(url.path.rsplit('/', 1)[-1]) != wheel):
            raise ValueError('invalid locked source URL')
        if not isinstance(package['requires_python'], str) or not package['requires_python']:
            raise ValueError('invalid Python requirement')
        if (not isinstance(package['requires_dist'], list)
                or any(not isinstance(v, str) or not v for v in package['requires_dist'])):
            raise ValueError('invalid dependency metadata')
        licenses = package['licenses']
        if (not isinstance(licenses, list) or not licenses
                or any(not isinstance(name, str) for name in licenses)
                or len(set(licenses)) != len(licenses)):
            raise ValueError('invalid license inventory')
        for name in licenses:
            if not _relative(name, directory=True).startswith(prefix + '.dist-info/'):
                raise ValueError('invalid license member')
    if names != {'bleak', 'dbus-fast'}:
        raise ValueError('incomplete BLE package set')
    return lock


def read_wheel(path, package):
    if path.is_symlink() or not path.is_file() or path.stat().st_size != package['bytes']:
        raise ValueError('wheel hash/bytes or file type mismatch: ' + str(path))
    raw = path.read_bytes()
    if len(raw) != package['bytes'] or hashlib.sha256(raw).hexdigest() != package['sha256']:
        raise ValueError('wheel hash/bytes mismatch: ' + str(path))
    prefix = package['name'].replace('-', '_') + '-' + package['version'] + '.dist-info/'
    module = package['name'].replace('-', '_') + '/'
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        infos = archive.infolist()
        if not infos or len(infos) > 4096 or sum(i.file_size for i in infos) > 64000000:
            raise ValueError('wheel expanded size/member limit')
        names, payload = set(), {}
        for info in infos:
            name = _relative(info.filename, directory=info.is_dir())
            if name in names:
                raise ValueError('duplicate wheel member')
            names.add(name)
            mode = info.external_attr >> 16
            if (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)
                    or bool(stat.S_IFMT(mode) == stat.S_IFDIR) != info.is_dir() and stat.S_IFMT(mode) != 0
                    or mode & 0o7000 or info.flag_bits & 1):
                raise ValueError('unsafe wheel member type/mode')
            if not (info.filename.startswith(module) or info.filename.startswith(prefix)):
                raise ValueError('unexpected wheel install location')
            if info.is_dir():
                continue
            if (info.file_size > 16000000 or info.file_size > max(info.compress_size, 1) * 1000
                    or not re.fullmatch(r'[A-Za-z0-9_./+\-]+', name)):
                raise ValueError('unsafe wheel member or compression limit')
            payload[name] = archive.read(info)
    for name in payload:
        parents = Path(name).parents
        if any(str(parent) in payload for parent in parents):
            raise ValueError('wheel file/directory collision')
    metadata_path = prefix + 'METADATA'
    if not {metadata_path, prefix + 'WHEEL', prefix + 'RECORD'} <= payload.keys():
        raise ValueError('missing wheel metadata')
    metadata = email.message_from_bytes(payload[metadata_path])
    if (metadata.get_all('Name') != [package['name']] or metadata.get_all('Version') != [package['version']]
            or metadata.get_all('Requires-Python') != [package['requires_python']]
            or (metadata.get_all('Requires-Dist') or []) != package['requires_dist']):
        raise ValueError('wheel metadata differs from lock')
    wheel_meta = email.message_from_bytes(payload[prefix + 'WHEEL'])
    expected_tags = (['py3-none-any'] if package['name'] == 'bleak' else
        ['cp313-cp313-' + tag for tag in path.name.rsplit('-', 1)[1][:-4].split('.')])
    if (wheel_meta.get_all('Wheel-Version') != ['1.0'] or
            set(wheel_meta.get_all('Tag') or []) != set(expected_tags)):
        raise ValueError('wheel ABI metadata mismatch')
    licenses = {name for name in names if name == prefix + 'licenses' or name.startswith(prefix + 'licenses/')}
    if set(package['licenses']) != {name + ('/' if name not in payload else '') for name in licenses}:
        raise ValueError('wheel license inventory mismatch')
    record_name = prefix + 'RECORD'
    records = {}
    for row in csv.reader(io.StringIO(payload[record_name].decode('utf-8'))):
        if len(row) != 3 or row[0] in records or row[0] not in payload:
            raise ValueError('invalid wheel RECORD inventory')
        name, digest, size = row
        if name == record_name:
            if digest or size:
                raise ValueError('invalid wheel RECORD self entry')
        else:
            expected = 'sha256=' + base64.urlsafe_b64encode(hashlib.sha256(payload[name]).digest()).rstrip(b'=').decode()
            if digest != expected or size != str(len(payload[name])):
                raise ValueError('wheel RECORD hash/bytes mismatch')
        records[name] = row
    if records.keys() != payload.keys():
        raise ValueError('wheel RECORD incomplete')
    return payload


def command(argv):
    p = subprocess.run(argv, text=True, capture_output=True, timeout=30,
                       env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1"})
    result = {"argv": argv, "exit": p.returncode, "stdout": p.stdout, "stderr": p.stderr}
    if p.returncode:
        raise ValueError("target execution failed: " + json.dumps(result))
    return result


def _directory(path):
    path = Path(os.path.abspath(path))
    for member in (*reversed(path.parents), path):
        if member.is_symlink() or not member.is_dir():
            raise ValueError('target/cache path must contain only directories: ' + str(member))
    return path


def _publish(stage, final):
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(stage), -100, os.fsencode(final), 1):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(final))


def install(root, wheel_dir, lock_path):
    root = _directory(root)
    if root == Path('/'):
        raise ValueError('a non-host target root is required')
    wheel_dir = _directory(wheel_dir)
    lock_bytes = lock_path.read_bytes()
    lock = read_lock(lock_path)
    if lock_path.read_bytes() != lock_bytes:
        raise ValueError('lock changed during input validation')
    payload = {}
    for package in lock['packages']:
        members = read_wheel(wheel_dir / package['wheel'], package)
        if payload.keys() & members.keys():
            raise ValueError('wheel payload collision')
        payload.update(members)
    chroot = shutil.which('chroot')
    if not chroot:
        raise ValueError('chroot required')
    abi_code = 'import sys,sysconfig,json; print(json.dumps([sys.implementation.name,list(sys.version_info[:2]),sysconfig.get_config_var("SOABI")]))'
    flags = ['-O'] if sys.flags.optimize else []
    abi = command([chroot, str(root), PYTHON, *flags, '-B', '-I', '-S', '-c', abi_code])
    if json.loads(abi['stdout']) != ['cpython', [3, 13], 'cpython-313-x86_64-linux-gnu']:
        raise ValueError('target ABI mismatch')
    opt = root / 'opt'
    if os.path.lexists(opt):
        _directory(opt)
    else:
        opt.mkdir(mode=0o755)
    final = opt / 'xtos-ble'
    if os.path.lexists(final):
        raise ValueError('fresh-only: /opt/xtos-ble already exists')
    stage = Path(tempfile.mkdtemp(prefix='.xtos-ble-', dir=opt))
    try:
        for name, raw in payload.items():
            target = stage / 'site-packages' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as stream:
                stream.write(raw)
            target.chmod(0o644)
        (stage / 'probe.py').write_text(PROBE)
        (stage / 'probe.py').chmod(0o644)
        actual = command([chroot, str(root), PYTHON, *flags, '-B', '-I', '-S',
                          '/opt/' + stage.name + '/probe.py'])
        probe = json.loads(actual['stdout'])
        if (not isinstance(probe, dict) or probe.get('status') != 'dependencies_ready'
                or probe.get('physical_ble_verified') is not False
                or probe.get('bluez_verified') is not False or probe.get('no_action') is not True
                or not isinstance(probe.get('native_modules'), list) or not probe['native_modules']
                or type(probe.get('optimization')) is not int or probe['optimization'] != sys.flags.optimize
                or probe.get('versions') != {p['name']: p['version'] for p in lock['packages']}
                or probe.get('linux_backend') != 'bleak.backends.bluezdbus.client'):
            raise ValueError('target BLE import result mismatch')
        files = []
        expected = {'site-packages/' + name: raw for name, raw in payload.items()}
        expected['probe.py'] = PROBE.encode('utf-8')
        observed = {}
        for path in sorted(stage.rglob('*')):
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                raise ValueError('payload changed after target import')
            if path.is_file():
                data = path.read_bytes()
                name = str(path.relative_to(stage))
                if name not in expected or data != expected[name] or path.stat().st_mode & 0o7777 != 0o644:
                    raise ValueError('payload changed after target import')
                observed[name] = data
                files.append(dict(path=name, bytes=len(data),
                                  sha256=hashlib.sha256(data).hexdigest(), mode=0o644))
        if observed.keys() != expected.keys():
            raise ValueError('payload changed after target import')
        if lock_path.read_bytes() != lock_bytes:
            raise ValueError('lock changed during target import')
        receipt = dict(schema='xtos.ble-runtime-install.v1', prefix='/opt/xtos-ble',
            lock_sha256=hashlib.sha256(lock_bytes).hexdigest(),
            probe=probe, commands=[abi, actual], files=files, fresh_only=True, no_host_install=True)
        with (stage / 'manifest.json').open('x') as stream:
            json.dump(receipt, stream, indent=2)
            stream.write('\n')
        stage.chmod(0o755)
        _publish(stage, final)
        return receipt
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--wheel-dir", required=True, type=Path)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    args = parser.parse_args()
    try:
        print(json.dumps(install(args.root, args.wheel_dir, args.lock)))
        return 0
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
        print("BLE runtime refused: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
