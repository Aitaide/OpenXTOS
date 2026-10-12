#!/usr/bin/env python3
"""Download an authenticated package closure in isolated APT state; never install."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = ROOT / 'system/profiles'
KEYRING = Path('/usr/share/keyrings/debian-archive-keyring.gpg')
TOKEN = re.compile(r'[a-z0-9][a-z0-9+.-]*')

def describe(name, architecture, seen=()):
    if not TOKEN.fullmatch(name) or name in seen:
        raise ValueError('unsafe or cyclic profile')
    data = json.loads((PROFILE_DIR / (name + '.json')).read_text())
    if architecture not in data['architectures']:
        raise ValueError(f'architecture {architecture} not supported by {name}')
    parent = describe(data['extends'], architecture, (*seen, name)) if data.get('extends') else {}
    return {
        'schema_version': 1, 'profile': name, 'architecture': architecture,
        'repository_suite': data['repository_suite'],
        'requested_packages': sorted(set(parent.get('requested_packages', []) + data['requested_packages'])),
        'required_commands': sorted(set(parent.get('required_commands', []) + data['required_commands'])),
        'defaults': dict(parent.get('defaults', {}), **data.get('defaults', {})),
    }

def run(command, log_dir, name, apt_config=None):
    # APT_CONFIG is read before APT's host main/parts configuration. -c is too late.
    env={'PATH':'/usr/bin:/usr/sbin:/bin:/sbin', 'LC_ALL':'C'}
    if apt_config is not None:
        env['APT_CONFIG']=str(Path(apt_config).absolute())
    elif Path(command[0]).name in ('apt-get','apt-cache','apt-config'):
        raise ValueError('isolated early APT configuration is required')
    result = subprocess.run(command, text=True, capture_output=True, timeout=480, env=env)
    (log_dir / (name + '.log')).write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(f'{name} failed ({result.returncode}): {result.stderr[-1500:]}')
    return result.stdout

def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024*1024), b''): h.update(chunk)
    return h.hexdigest()

def paragraphs(text):
    rows = []
    for paragraph in text.split('\n\n'):
        row = {}
        for line in paragraph.splitlines():
            if line and not line[0].isspace() and ': ' in line:
                key, value = line.split(': ', 1); row[key] = value
        if row: rows.append(row)
    return rows

def acquire(profile, work, lock_output):
    if profile['architecture'] != 'x86_64': raise ValueError('only x86_64 acquisition is implemented')
    work = work.absolute()
    if any(ord(c) < 32 or c in '\\"' for c in str(work)):
        raise ValueError('unsafe work-dir characters for APT configuration')
    if work.is_symlink() or work.resolve() != work or work == ROOT or not work.is_relative_to(ROOT / '.build/userspace'):
        raise ValueError('work-dir must be a non-symlink directory under repository .build/userspace')
    marker = work / 'apt/xtos-apt-sandbox.json'
    if work.exists() and any(work.iterdir()):
        raise ValueError('a fresh empty download directory is required; reuse locked archives for offline assembly')
    if not KEYRING.is_file(): raise ValueError('Debian archive keyring missing')
    for rel in ['apt/etc/apt.conf.d', 'apt/etc/preferences.d', 'apt/lists/partial', 'apt/cache', 'apt/log', 'packages/partial', 'evidence']:
        (work/rel).mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({'schema_version': 1, 'purpose': 'xtos-download-only'}, sort_keys=True)+'\n')
    status=work/'apt/status'
    if status.exists() and status.stat().st_size: raise ValueError('APT sandbox status must remain empty')
    status.write_text('')
    sources = (
        f'deb [signed-by={KEYRING}] https://deb.debian.org/debian trixie main\n'
        f'deb [signed-by={KEYRING}] https://deb.debian.org/debian trixie non-free-firmware\n'
        f'deb [signed-by={KEYRING}] https://security.debian.org/debian-security trixie-security main\n'
    )
    (work/'apt/etc/sources.list').write_text(sources)
    config = '\n'.join([
        f'Dir::Etc "{work}/apt/etc";', 'Dir::Etc::sourcelist "sources.list";', 'Dir::Etc::sourceparts "-";',
        'Dir::Etc::main "-";', 'Dir::Etc::parts "apt.conf.d";',
        f'Dir::State "{work}/apt";', f'Dir::State::status "{status}";', f'Dir::State::lists "{work}/apt/lists";',
        f'Dir::State::extended_states "{work}/apt/extended_states";', f'Dir::Cache "{work}/apt/cache";',
        f'Dir::Cache::archives "{work}/packages";', f'Dir::Log "{work}/apt/log";',
        'APT::Architecture "amd64";', 'APT::Architectures { "amd64"; };',
        'APT::Install-Recommends "false";', 'APT::Install-Suggests "false";', 'APT::Sandbox::User "root";',
        'Acquire::Languages "none";', 'Acquire::Retries "2";', 'Acquire::https::Timeout "20";',
        'Acquire::AllowInsecureRepositories "false";', 'APT::Get::AllowUnauthenticated "false";', '',
    ])
    conf=work/'apt/apt.conf'; conf.write_text(config)
    logs=work/'evidence'
    base=['apt-get']
    run(base+['update'],logs,'apt-update',apt_config=conf)
    requested=profile['requested_packages']
    uri_text=run(base+['-y','--download-only','--print-uris','install',*requested],logs,'apt-uris',apt_config=conf)
    uris={}
    for line in uri_text.splitlines():
        if line.startswith("'https://"):
            fields=shlex.split(line)
            uris[urllib.parse.unquote(fields[1])]=fields[0]
    run(base+['-y','--download-only','install',*requested],logs,'apt-download',apt_config=conf)
    packages=[]
    files=sorted((work/'packages').glob('*.deb'))
    for index, file in enumerate(files):
        control=run(['dpkg-deb','--field',str(file),'Package','Version','Architecture'],logs,f'control-{index}')
        metadata=paragraphs(control)[0]
        name,version,arch=(metadata[k] for k in ('Package','Version','Architecture'))
        text=run(['apt-cache','show',f'{name}={version}'],logs,f'index-{index}',apt_config=conf)
        choices=[row for row in paragraphs(text) if row.get('Version')==version and row.get('Architecture')==arch and 'SHA256' in row]
        digest=sha(file)
        match=next((row for row in choices if row['SHA256']==digest and int(row['Size'])==file.stat().st_size),None)
        if match is None: raise ValueError(f'{name}: downloaded bytes do not match authenticated index')
        uri=uris.get(urllib.parse.unquote(file.name))
        if uri is None:
            # APT suppresses already-cached URLs; resolve using the signed filename.
            filename=match['Filename']
            host='https://security.debian.org/debian-security/' if filename.startswith('pool/updates/') else 'https://deb.debian.org/debian/'
            uri=host+filename
        packages.append({'name':name,'version':version,'architecture':arch,'filename':file.name,'sha256':digest,'size':file.stat().st_size,'source_url':uri})
    for p in requested:
        if p not in {entry['name'] for entry in packages}: raise ValueError(f'requested package missing: {p}')
    lock={
        **profile, 'package_format':'deb', 'packages':sorted(packages,key=lambda p:p['name']),
        'provenance':{'authentication':'APT signed InRelease and SHA256 in Packages; no insecure repositories',
          'sources':sources.splitlines(),'keyring_sha256':sha(KEYRING),
          'inrelease_sha256':{p.name:sha(p) for p in sorted((work/'apt/lists').glob('*InRelease'))}},
    }
    lock_output.parent.mkdir(parents=True, exist_ok=True)
    lock_output.write_text(json.dumps(lock,indent=2,sort_keys=True)+'\n')
    print(json.dumps({'status':'downloaded','package_count':len(packages),'archive_bytes':sum(p['size'] for p in packages),'lock':str(lock_output),'packages_dir':str(work/'packages')},sort_keys=True))

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', default='developer')
    parser.add_argument('--arch', default='x86_64')
    parser.add_argument('--describe',action='store_true')
    parser.add_argument('--work-dir',type=Path)
    parser.add_argument('--lock-output',type=Path)
    args=parser.parse_args()
    try:
        profile=describe(args.profile,args.arch)
        if args.describe:
            print(json.dumps(profile,indent=2,sort_keys=True));return 0
        if not args.work_dir or not args.lock_output: raise ValueError('--work-dir and --lock-output are required')
        acquire(profile,args.work_dir,args.lock_output)
    except (OSError,ValueError,RuntimeError,KeyError,subprocess.TimeoutExpired) as exc:
        parser.error(str(exc))
    return 0

if __name__=='__main__': raise SystemExit(main())
