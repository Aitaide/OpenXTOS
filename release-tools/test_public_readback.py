"""Exercise public-readback orchestration with explicit controlled fixtures only."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('readback_fixture', HERE / 'verify_public_release.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
recipe = '110048c955c71a0701f5eee04351ab7a4903b0f6'
blob = b'Controlled project fixture\n'
runtime = b'Controlled runtime fixture\n'
expected = {'fixture.txt': {'mode': '100644', 'kind': 'blob', 'oid': hashlib.sha1(b'blob ' + str(len(blob)).encode() + b'\0' + blob).hexdigest()}}
module.project_inventory = lambda: expected
symlink_blob = b'fixture.txt'
symlink_row = {'mode': '120000', 'kind': 'blob', 'oid': hashlib.sha1(b'blob ' + str(len(symlink_blob)).encode() + b'\0' + symlink_blob).hexdigest()}
recipe_paths = subprocess.check_output(['git', 'ls-tree', '-r', '--name-only', recipe, '--', 'release-tools'], cwd=module.ROOT, text=True).splitlines()
recipe_members = {'release-recipe/' + p.removeprefix('release-tools/'): subprocess.check_output(['git', 'show', recipe + ':' + p], cwd=module.ROOT) for p in recipe_paths}


def source_bytes():
    dsc = f'Source: fixture\nVersion: 1.0\nChecksums-Sha256:\n {hashlib.sha256(runtime).hexdigest()} {len(runtime)} fixture_1.0.tar.gz\n'.encode()
    members = {**recipe_members, 'project-source/openxtos-v1.0.2/fixture.txt': blob, 'runtime-sources/fixture_1.0.dsc': dsc, 'runtime-sources/fixture_1.0.tar.gz': runtime, 'SOURCE_PACKAGES.tsv': b'source_package\tsource_version\tbinary_packages\nfixture\t1.0\tfixture\n', 'PROVENANCE.json': json.dumps({'source_commit': module.SHA, 'recipe_commit': recipe, 'applied_release_overlays': []}).encode()}
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as archive:
        for name, data in members.items():
            item = tarfile.TarInfo(name)
            item.size = len(data)
            item.mode = 0o644
            archive.addfile(item, io.BytesIO(data))
    return stream.getvalue()


source = source_bytes()
iso = b'Controlled binary fixture: not a bootable image\n'
iso_digest = hashlib.sha256(iso).hexdigest()
checks = ['bios_iso_install=PASS', 'bios_disk_modules_sdk_selftest=PASS', 'public_installer_has_no_embedded_ssh_key=PASS', 'bios_reboot_persistence_selftest=PASS', 'uefi_fallback_disk_selftest=PASS', 'uefi_iso_boot=PASS']
results = []
for case in ['pass', 'wrong-download-digest', 'wrong-checksum', 'wrong-source-sha', 'unbound-qemu', 'failing-qemu', 'manifest-coverage']:
    files = {'openxtos-v1.0.2-x86_64-installer.iso': iso, 'openxtos-v1.0.2-arm64.tar.gz': b'Controlled ARM fixture\n', 'openxtos-v1.0.2-x86_64-corresponding-source.tar.gz': source, 'openxtos-v1.0.2-arm64-corresponding-source.tar.gz': source, 'x86_64-build-manifest.json': json.dumps({'git_head': module.SHA, 'dirty': '', 'installer_variant': 'public', 'installer_artifact': {'sha256': iso_digest}}).encode(), 'x86_64-qemu-results.json': json.dumps({'iso_sha256': '0' * 64 if case == 'unbound-qemu' else iso_digest, 'results': checks + (['bad=FAIL'] if case == 'failing-qemu' else [])}).encode()}
    inventory = {name: {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()} for name, data in files.items()}
    if case == 'manifest-coverage':
        inventory.pop('openxtos-v1.0.2-arm64.tar.gz')
    files['RELEASE-MANIFEST.json'] = json.dumps({'source_commit': '0' * 40 if case == 'wrong-source-sha' else module.SHA, 'source_tag': 'v1.0.2', 'release_recipe_commit': recipe, 'artifacts': inventory}).encode()
    files['SHA256SUMS.txt'] = ''.join(f'{hashlib.sha256(data).hexdigest()}  {name}\n' for name, data in files.items()).encode()
    if case == 'wrong-checksum':
        files['SHA256SUMS.txt'] = files['SHA256SUMS.txt'].replace(iso_digest.encode(), b'0' * 64, 1)
    release = {'id': 1, 'draft': False, 'tag_name': 'v1.0.2', 'html_url': 'https://fixture.invalid/release', 'assets': [{'name': name, 'size': len(data), 'digest': 'sha256:' + ('0' * 64 if case == 'wrong-download-digest' and name.endswith('.iso') else hashlib.sha256(data).hexdigest()), 'browser_download_url': 'https://fixture.invalid/' + name} for name, data in files.items()]}
    def get(url):
        data = json.dumps(release).encode() if url == module.API else files[url.rsplit('/', 1)[1]]
        return contextlib.closing(io.BytesIO(data))
    module.get = get
    with tempfile.TemporaryDirectory(prefix='entry-fixture-', dir=HERE) as directory:
        module.OUT = Path(directory)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                module.main()
        except RuntimeError as error:
            if case == 'pass':
                raise
            results.append({'case': case, 'rejected': True, 'reason': str(error)})
        else:
            if case != 'pass':
                raise RuntimeError('accepted bad fixture: ' + case)
            record = json.loads((module.OUT / 'verified.json').read_text())
            if record['status'] != 'PASS' or len(record['assets']) != len(files):
                raise RuntimeError('fixture closeout missing')
            results.append({'case': case, 'accepted': True})
for source_case in ['real-symlink', 'regular-impersonates-symlink']:
    source_stream = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(source), mode='r:gz') as original, tarfile.open(fileobj=source_stream, mode='w:gz') as amended:
        for item in original:
            stream = original.extractfile(item) if item.isfile() else None
            amended.addfile(item, stream)
        item = tarfile.TarInfo('project-source/openxtos-v1.0.2/fixture-link')
        if source_case == 'real-symlink':
            item.type = tarfile.SYMTYPE
            item.linkname = symlink_blob.decode()
            amended.addfile(item)
        else:
            item.size = len(symlink_blob)
            item.mode = 0o644
            amended.addfile(item, io.BytesIO(symlink_blob))
    with tempfile.TemporaryDirectory(prefix='source-type-fixture-', dir=HERE) as directory:
        archive = Path(directory) / 'source.tar.gz'
        archive.write_bytes(source_stream.getvalue())
        try:
            module.source_archive(archive, {**expected, 'fixture-link': symlink_row})
        except RuntimeError:
            if source_case == 'real-symlink':
                raise
            results.append({'case':source_case,'rejected':True})
        else:
            if source_case != 'real-symlink':
                raise RuntimeError('accepted regular file impersonating symlink')
            results.append({'case':source_case,'accepted':True})
evidence = module.ROOT / '.build/public-readback-fixtures'
evidence.mkdir(parents=True, exist_ok=True)
(evidence / 'readback-entry-fixture-results.json').write_text(json.dumps({'scope': 'readback helper fixtures only; no actual image or public release acceptance', 'results': results}, indent=2))
print(json.dumps(results))
