#!/usr/bin/env python3
"""Offline pinned-source admission; not signature, build or runtime attestation."""
import argparse
import fnmatch
import subprocess
import hashlib
import json
import os
from pathlib import Path
import re
import sys


def load_pin(root):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate pin field=' + key)
            result[key] = value
        return result
    pin = json.loads(contained_file(root, 'kernel/source-pin.json').read_bytes(), object_pairs_hook=pairs)
    fields = {'schema', 'version', 'upstream', 'archive_sha256', 'archive_bytes',
              'source_signer_fingerprint', 'rt_mode', 'rt_config', 'rt_patch',
              'xtos_patch', 'xtos_patch_sha256', 'previous_baseline', 'status'}
    if not isinstance(pin, dict) or set(pin) != fields:
        raise ValueError('invalid pin schema fields')
    for key, value in {'schema': 'xtos.kernel.source.v1', 'version': '7.3.1',
                       'rt_mode': 'mainline', 'rt_config': 'CONFIG_PREEMPT_RT=y',
                       'rt_patch': None, 'previous_baseline': '6.6.0-rt15',
                       'status': 'source_pin_not_runtime_acceptance',
                       'xtos_patch': 'kernel/patches/xtos-linux-7.3.1.patch',
                       'upstream': 'https://cdn.kernel.org/pub/linux/kernel/v7.x/linux-7.3.1.tar.xz'}.items():
        if pin[key] != value:
            raise ValueError('invalid pin field=' + key)
    if type(pin['archive_bytes']) is not int or pin['archive_bytes'] <= 0:
        raise ValueError('invalid archive_bytes')
    for key in ('archive_sha256', 'xtos_patch_sha256'):
        if not isinstance(pin[key], str) or not re.fullmatch('[0-9a-f]{64}', pin[key]):
            raise ValueError('invalid ' + key)
    if not isinstance(pin['source_signer_fingerprint'], str) or not re.fullmatch('[0-9A-F]{40}', pin['source_signer_fingerprint']):
        raise ValueError('invalid source_signer_fingerprint')
    return pin


def contained_file(base, relative):
    path = base / relative
    if not path.resolve().is_relative_to(base.resolve()):
        raise ValueError('source path escapes root=' + str(path))
    if not path.is_file():
        raise ValueError('missing required source=' + str(path))
    return path


def validate(root, source):
    pin = load_pin(root)
    text = contained_file(source, 'Makefile').read_text()
    values = []
    for key in ('VERSION', 'PATCHLEVEL', 'SUBLEVEL'):
        matches = re.findall(r'^' + key + r'\s*=\s*(\d+)\s*$', text, re.M)
        if len(matches) != 1:
            raise ValueError('invalid Linux version field ' + key)
        values.append(matches[0])
    if '.'.join(values) != pin['version']:
        raise ValueError('unexpected Linux version=' + '.'.join(values))
    extra = re.findall(r'^EXTRAVERSION[ \t]*=[ \t]*(.*)$', text, re.M)
    if len(extra) != 1 or extra[0].strip():
        raise ValueError('unexpected Linux EXTRAVERSION')
    for rel in ('kernel/sched/xtos_sched.c', 'kernel/sched/xtos/Kconfig',
                'drivers/xtos/bci/xtos_bci_core.c', 'include/linux/xtos.h'):
        if not contained_file(source, rel).stat().st_size:
            raise ValueError('missing XTOS source=' + rel)
    patch = contained_file(root, pin['xtos_patch'])
    if hashlib.sha256(patch.read_bytes()).hexdigest() != pin['xtos_patch_sha256']:
        raise ValueError('XTOS patch SHA256 mismatch')
    if not contained_file(root, 'kernel/xtos_defconfig').stat().st_size:
        raise ValueError('empty XTOS defconfig')
    preempt = contained_file(source, 'kernel/Kconfig.preempt').read_text()
    arch = contained_file(source, 'arch/x86/Kconfig').read_text()
    block = re.search(r'^config PREEMPT_RT[ \t]*\n(.*?)(?=^config |\Z)', preempt, re.M | re.S)
    if not block or not re.search(r'^\s+depends on EXPERT && ARCH_SUPPORTS_RT && !COMPILE_TEST\s*$', block[1], re.M):
        raise ValueError('missing mainline PREEMPT_RT definition/dependencies')
    if not re.search(r'^\s+select ARCH_SUPPORTS_RT\s*$', arch, re.M):
        raise ValueError('missing x86 ARCH_SUPPORTS_RT selection')
    return pin


# Exact upstream fixture bytes from the independently verified 7.3.1 archive.
# Never exempt a suffix or an entire directory: replacements remain generated.
VENDOR_FIXTURES = {'tools/testing/kunit/test_data/test_config_printk_time.log': '115e5bd906137a4f86b98a29f4016ac99b3624e790b502524b9e40a16d3171e2', 'tools/testing/kunit/test_data/test_insufficient_memory.log': 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855', 'tools/testing/kunit/test_data/test_interrupted_tap_output.log': 'd6b49b1d6e11c8339c8b126a483c5a476375ddb47028667a9ff81ce73c9afdd6', 'tools/testing/kunit/test_data/test_is_test_passed-all_passed.log': 'fa23fe4bc3bdade23cca05724946fe233f9fab68c7c62170e37ce6c3a49012c2', 'tools/testing/kunit/test_data/test_is_test_passed-all_passed_nested.log': '7c4eaf1d33ade7ca106aa735c0b532b064c42befb4ecb12fa3e40f43447eb9b6', 'tools/testing/kunit/test_data/test_is_test_passed-failure-nested.log': '80ae5e90585b099db8ff75ef15e25054b6fda5dfabaf72d880859dfc87befe1e', 'tools/testing/kunit/test_data/test_is_test_passed-failure.log': '3a5660daa472a4e0b0f5198a796e4f919d145951a871ca1b7538c550327c7372', 'tools/testing/kunit/test_data/test_is_test_passed-kselftest.log': 'd690f3bb26ee104aa295c923818b0d2693498f39921f1242210c50fe8cf63e65', 'tools/testing/kunit/test_data/test_is_test_passed-missing_plan.log': 'e2f39d87e9c4c3232da8f8f31d623fce226ff659e7835d740c475ea7f7fef731', 'tools/testing/kunit/test_data/test_is_test_passed-no_tests_no_plan.log': '41f481fd0631553bde574944e542344f422e8af2632b692068a221fea6bd9dcc', 'tools/testing/kunit/test_data/test_is_test_passed-no_tests_run_no_header.log': '54df581112aeb2273f4cffa9da36fe437788c4bb24a8313de987b4b4f2b8ccf3', 'tools/testing/kunit/test_data/test_is_test_passed-no_tests_run_with_header.log': '9086dd877a9d7a5ec1e7050b6b5e7869071fabb4a4dc469f1173e4d70e8841e6', 'tools/testing/kunit/test_data/test_kernel_panic_interrupt.log': 'c995fa28bd9640580f23647e95bc2c4bc9f2cae4de037bdd94266ca528b6148b', 'tools/testing/kunit/test_data/test_multiple_prefixes.log': '911399137fce0130c9ece94947555259431e3c6160367899f79ff8ee0a414031', 'tools/testing/kunit/test_data/test_output_isolated_correctly.log': '77b6cda15b03bfa66e110121571c6b3125f1d9f3fc11f82d6d9deff5344c06a8', 'tools/testing/kunit/test_data/test_output_with_prefix_isolated_correctly.log': '862ca5b595bdff0dd2714df64bd527358c4e647f8d059295c78fa8554ac537b1', 'tools/testing/kunit/test_data/test_parse_attributes.log': 'dc28f46deb3bdcb715ac8dc2ac24351e377dc6a4d8d446420744abcfc14b2c3a', 'tools/testing/kunit/test_data/test_parse_ktap_output.log': 'e0e5de94b5b2f5891208047eb264d582b05063262409fcc14643db9efb6307c6', 'tools/testing/kunit/test_data/test_parse_subtest_header.log': '3ec5d407665b263e76c9400459733e7873157e713b78704f81ebd65bc8f28917', 'tools/testing/kunit/test_data/test_pound_no_prefix.log': 'fb0ba70d56f809c2261a7abc7cf365113a75e60d1ac4bfe577c3ed6f77e6350b', 'tools/testing/kunit/test_data/test_pound_sign.log': '786f8f808b895174fe484ee9cebc0a29b57a50851f3137621d6dc41f9d3d8d53', 'tools/testing/kunit/test_data/test_skip_all_tests.log': '01790f1186116c295542c89a2d4f96a334953bcea49d3f83f8751370ae22ac06', 'tools/testing/kunit/test_data/test_skip_tests.log': '7dabb95f6765de505ca66c2571c5f1d85de8049712ec09648245570888af6c02', 'tools/testing/kunit/test_data/test_strip_hyphen.log': 'b384136fe1a8b1ea0a15aa79d0f15969adbb8da067b25916091a1363f969fd58'}
GENERATED = ('*.o', '*.ko', '*.a', '*.so', '*.so.*', '*.cmd', '*.mod',
             '*.mod.c', '*.mod.o', 'bzImage', 'vmlinux', 'System.map',
             '*.pyc', '*.pyo', '*.log', '*.tar.xz', '*.tar.gz', '*.zip',
             '*.stdout', '*.tmp', '*.out')
KERNEL_GENERATED = GENERATED + ('Module.symvers', 'modules.order', 'modules.builtin*')


def approved_vendor(path, base):
    try:
        relative = path.relative_to(base).as_posix()
    except ValueError:
        return False
    expected = VENDOR_FIXTURES.get(relative)
    return bool(expected and path.is_file() and
                path.resolve().is_relative_to(base.resolve()) and
                hashlib.sha256(path.read_bytes()).hexdigest() == expected)


def scan(root, source):
    result = subprocess.run(['git', '-C', str(root), 'ls-files', '-z', '--cached',
                             '--others', '--exclude-standard'], capture_output=True, check=True)
    violations = 0
    for raw in result.stdout.split(b'\0'):
        if not raw:
            continue
        rel = os.fsdecode(raw)
        if fnmatch.fnmatchcase(rel, 'kernel/patches/*.patch') or fnmatch.fnmatchcase(rel, 'userspace/busybox/testsuite/*.zip'):
            continue
        path = root / rel
        if any(fnmatch.fnmatchcase(path.name, pat) for pat in GENERATED):
            if approved_vendor(path, root / 'kernel/linux'):
                continue
            print('generated_or_dependency=' + rel)
            violations += 1
    for directory, dirs, files in os.walk(source, followlinks=False):
        for name in files:
            path = Path(directory) / name
            if any(fnmatch.fnmatchcase(name, pat) for pat in KERNEL_GENERATED):
                if approved_vendor(path, source):
                    continue
                print('generated_in_bundled_kernel=' + str(path.relative_to(source)))
                violations += 1
    if violations:
        raise ValueError('generated source violations=' + str(violations))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--kernel-dir', required=True, type=Path)
    parser.add_argument('--scan', action='store_true')
    args = parser.parse_args()
    try:
        pin = validate(args.root.resolve(), args.kernel_dir.resolve())
        if args.scan:
            scan(args.root.resolve(), args.kernel_dir.resolve())
        print('bundled_linux=' + str(args.kernel_dir))
        print('bundled_release=' + pin['version'])
        print('rt_source=mainline available; configuration and runtime unverified')
        print('xtos_integration=present')
        if args.scan:
            print('source_tree=pass linux=' + pin['version'] + ' rt=mainline xtos=present')
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        print('source_tree=fail ' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
