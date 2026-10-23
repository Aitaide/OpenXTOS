#!/usr/bin/env python3
"""Run bounded acceptance inside an already assembled XTOS userspace root.

This checks real binaries through chroot; it is not an XTOS-kernel boot test.
No mounts, host network configuration or block devices are changed.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
import sys
# Permit loading this verifier by path from stdlib unittest.
sys.path.insert(0,str(Path(__file__).resolve().parent))
from userspace_root import open_owned_root, DIR_FLAGS

COMMANDS = {
    'nano': ['/usr/bin/nano', '--version'],
    'vi': ['/usr/bin/vi', '--version'],
    'wget': ['/usr/bin/wget', '--version'],
    'curl': ['/usr/bin/curl', '--version'],
    'git': ['/usr/bin/git', '--version'],
    'python': ['/usr/bin/python3', '--version'],
    'pip': ['/usr/bin/python3', '-m', 'pip', '--version'],
    'go': ['/usr/bin/go', 'version'],
    'gcc': ['/usr/bin/gcc', '--version'],
    'g++': ['/usr/bin/g++', '--version'],
    'python_stdlib': ['/usr/bin/python3', '-c', 'import ssl,sqlite3,ctypes,lzma,bz2,gzip,zipfile,hashlib,venv; print("PYTHON_STDLIB=PASS"); print(ssl.OPENSSL_VERSION)'],
}
PYTHON_ACCEPTANCE = r'''
import hashlib, json, os, pathlib, subprocess, tempfile
work=pathlib.Path(tempfile.mkdtemp(prefix='xtos-accept-',dir='/tmp'))
def run(args, **kwargs):
    p=subprocess.run(args, capture_output=True, text=True, timeout=90, **kwargs)
    if p.returncode: raise RuntimeError(str(args)+'\n'+p.stdout+'\n'+p.stderr)
    return p.stdout.strip()
try:
    content='XTOS 开发环境验证\n'.encode()
    (work/'input.txt').write_bytes(content)
    for compressor,decompressor,suffix in [('gzip','gzip','.gz'),('xz','xz','.xz'),('bzip2','bzip2','.bz2')]:
        packed=subprocess.run(['/usr/bin/'+compressor,'-c',str(work/'input.txt')],capture_output=True,check=True).stdout
        file=work/('archive'+suffix); file.write_bytes(packed)
        restored=subprocess.run(['/usr/bin/'+decompressor,'-dc',str(file)],capture_output=True,check=True).stdout
        if restored!=content: raise RuntimeError('archive mismatch '+compressor)
    run(['/usr/bin/tar','-czf',str(work/'data.tar.gz'),'-C',str(work),'input.txt'])
    unpack=work/'unpack';unpack.mkdir()
    run(['/usr/bin/tar','-xzf',str(work/'data.tar.gz'),'-C',str(unpack)])
    if (unpack/'input.txt').read_bytes()!=content:raise RuntimeError('tar mismatch')
    run(['/usr/bin/zip','-q',str(work/'data.zip'),'input.txt'],cwd=work)
    zipdir=work/'unzip';zipdir.mkdir()
    run(['/usr/bin/unzip','-q',str(work/'data.zip'),'-d',str(zipdir)])
    if (zipdir/'input.txt').read_bytes()!=content:raise RuntimeError('zip mismatch')
    print('ARCHIVES=PASS',flush=True)
    run(['/usr/bin/vi','-u','NONE','-i','NONE','-n','-es',str(work/'vi.txt')],input='a\nXTOS editor fixture\n.\nwq\n')
    if 'XTOS editor fixture' not in (work/'vi.txt').read_text():raise RuntimeError('vi save failed')
    print('VI_SAVE=PASS',flush=True)
    # Exercise GNU Wget's real certificate verification against bounded loopback servers.
    import http.server, ssl, threading
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200);self.send_header('Content-Length',str(len(content)));self.end_headers();self.wfile.write(content)
        def log_message(self,*args):pass
        def handle(self):
            try:super().handle()
            except (ssl.SSLError,ConnectionResetError,BrokenPipeError):pass  # Client rejects the deliberate invalid certificate.
    for index,san in enumerate(['127.0.0.1','127.0.0.2']):
        cert=work/('cert'+str(index)+'.pem');key=work/('key'+str(index)+'.pem')
        run(['/usr/bin/openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=xtos-test','-addext','subjectAltName=IP:'+san])
        server=http.server.HTTPServer(('127.0.0.1',0),Handler)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(str(cert),str(key))
        server.socket=context.wrap_socket(server.socket,server_side=True)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url='https://127.0.0.1:'+str(server.server_port)+'/'
        try:
            trusted=subprocess.run(['/usr/bin/wget','--no-proxy','--timeout=5','--tries=1','--ca-certificate='+str(cert),'-q','-O','-',url],capture_output=True,timeout=10)
            if index==0:
                if trusted.returncode or trusted.stdout!=content:raise RuntimeError('wget trusted TLS failed exit='+str(trusted.returncode)+' stdout='+repr(trusted.stdout)+' stderr='+trusted.stderr.decode())
                unknown=subprocess.run(['/usr/bin/wget','--no-proxy','--timeout=5','--tries=1','-q','-O','-',url],capture_output=True,timeout=10)
                if unknown.returncode==0:raise RuntimeError('wget accepted unknown CA')
            elif trusted.returncode==0:raise RuntimeError('wget accepted wrong hostname')
        finally:
            server.shutdown();server.server_close();thread.join(timeout=3)
    print('WGET_TLS_TRUST_REJECT=PASS',flush=True)
    venv=work/'venv'
    run(['/usr/bin/python3','-m','venv',str(venv)])
    pip_version=run([str(venv/'bin/python'),'-m','pip','--version'])
    run([str(venv/'bin/python'),'-m','pip','check'])
    print('PYTHON_VENV=PASS '+pip_version,flush=True)
    c=work/'hello.c';c.write_text('#include <stdio.h>\nint main(void){puts("XTOS_C_OK");return 0;}\n')
    run(['/usr/bin/gcc',str(c),'-o',str(work/'hello-c')])
    if run([str(work/'hello-c')])!='XTOS_C_OK':raise RuntimeError('C executable wrong')
    cpp=work/'hello.cpp';cpp.write_text('#include <iostream>\nint main(){std::cout<<"XTOS_CPP_OK\\n";}\n')
    run(['/usr/bin/g++',str(cpp),'-o',str(work/'hello-cpp')])
    if run([str(work/'hello-cpp')])!='XTOS_CPP_OK':raise RuntimeError('C++ executable wrong')
    print('NATIVE_C_CPP=PASS',flush=True)
    go=work/'hello.go';go.write_text('package main\nimport "fmt"\nfunc main(){fmt.Println("XTOS_GO_OK")}\n')
    env=dict(os.environ,GOTOOLCHAIN='local',GOPROXY='off',GOSUMDB='off',CGO_ENABLED='0',GOMAXPROCS='2',GOCACHE=str(work/'gocache'),GOTMPDIR=str(work))
    run(['/usr/bin/go','build','-o',str(work/'hello-go'),str(go)],env=env)
    if run([str(work/'hello-go')])!='XTOS_GO_OK':raise RuntimeError('Go executable wrong')
    print('NATIVE_GO=PASS',flush=True)
    cgo=work/'cgo.go';cgo.write_text('package main\n/*\nint value(void){return 42;}\n*/\nimport "C"\nimport "fmt"\nfunc main(){fmt.Println(C.value())}\n')
    env['CGO_ENABLED']='1'
    run(['/usr/bin/go','build','-o',str(work/'hello-cgo'),str(cgo)],env=env)
    if run([str(work/'hello-cgo')])!='42':raise RuntimeError('CGO executable wrong')
    print('NATIVE_CGO=PASS',flush=True)
finally:
    import shutil
    shutil.rmtree(work)
'''


def prepare_runtime_devnull(root):
    """Pin root/dev and remember the exact node we create, without parent links."""
    import stat
    root_fd, _ = open_owned_root(root)
    directory_fd = None
    try:
        try:os.mkdir('dev', 0o755, dir_fd=root_fd)
        except FileExistsError:pass
        directory_fd = os.open('dev', DIR_FLAGS, dir_fd=root_fd)
        created = False
        try:info = os.stat('null', dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            os.mknod('null', stat.S_IFCHR | 0o666, os.makedev(1,3), dir_fd=directory_fd)
            created = True
            info = os.stat('null', dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISCHR(info.st_mode) or info.st_rdev != os.makedev(1,3):
            raise ValueError('unexpected or symlinked dev/null')
        return {'root_fd':root_fd, 'directory_fd':directory_fd, 'created':created,
                'identity':(info.st_dev,info.st_ino,info.st_rdev)}
    except BaseException:
        if directory_fd is not None:os.close(directory_fd)
        os.close(root_fd)
        raise


def cleanup_runtime_devnull(root, state):
    """Unlink only our exact node through the pinned fd, even if dev is renamed."""
    import stat
    try:
        if state['created']:
            try:info=os.stat('null',dir_fd=state['directory_fd'],follow_symlinks=False)
            except FileNotFoundError:return
            if stat.S_ISCHR(info.st_mode) and (info.st_dev,info.st_ino,info.st_rdev)==state['identity']:
                os.unlink('null',dir_fd=state['directory_fd'])
    finally:
        os.close(state['directory_fd'])
        os.close(state['root_fd'])


def run_bounded(command, env, timeout, pass_fds=()):
    import signal
    child = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, pass_fds=pass_fds)
    try:
        stdout, stderr = child.communicate(timeout=timeout)
        return subprocess.CompletedProcess(command, child.returncode, stdout.decode(errors='replace'), stderr.decode(errors='replace'))
    except BaseException as exc:
        try:os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:pass
        stdout, stderr = child.communicate()
        if isinstance(exc, subprocess.TimeoutExpired):
            exc.output, exc.stderr = stdout, stderr
        raise


def command_in_root(root_fd, command):
    # The host helper enters the pinned root, closes its directory fd, then execs.
    # Target programs never inherit a directory handle they could use to escape.
    helper = ('import os,sys; fd=int(sys.argv[1]); os.fchdir(fd); '
              'os.chroot("."); os.chdir("/"); os.close(fd); '
              'os.execvpe(sys.argv[2],sys.argv[2:],os.environ)')
    return [sys.executable, '-c', helper, str(root_fd), *command]


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--rootfs',type=Path,required=True)
    p.add_argument('--report',type=Path,required=True)
    args=p.parse_args()
    root=args.rootfs.absolute()
    try:
        root_fd,_=open_owned_root(root)
        os.close(root_fd)
    except ValueError as exc:p.error(str(exc))
    if os.geteuid()!=0:p.error('chroot verification requires root on the control host')
    report={'schema_version':1,'rootfs':str(root),'mode':'chroot on control-host kernel; not physical XTOS validation','commands':{},'unverified':['nano interactive save/exit','public HTTPS/DNS and package installation','XTOS-kernel boot','AI inference']}
    env={'PATH':'/usr/bin:/usr/sbin:/bin:/sbin','LANG':'C.UTF-8','LC_ALL':'C.UTF-8','HOME':'/root','GOTOOLCHAIN':'local','GOROOT':'/usr/lib/go','GOMAXPROCS':'2','TERM':'xterm-256color'}
    created_devnull=prepare_runtime_devnull(root)
    try:
        for name,command in list(COMMANDS.items())+[('native_environment',['/usr/bin/python3','-c',PYTHON_ACCEPTANCE])]:
            start=time.monotonic()
            try:
                r=run_bounded(command_in_root(created_devnull['root_fd'],command),env=env,timeout=360,pass_fds=(created_devnull['root_fd'],))
                row={'exit_code':r.returncode,'stdout':r.stdout,'stderr':r.stderr,'elapsed_seconds':round(time.monotonic()-start,3)}
            except subprocess.TimeoutExpired:
                row={'exit_code':124,'stderr':'acceptance timeout; process group stopped','elapsed_seconds':round(time.monotonic()-start,3)}
            report['commands'][name]=row
            print(name+' exit='+str(row['exit_code']),flush=True)
            if row.get('stdout'):print(row['stdout'][:1400],flush=True)
            if row.get('stderr'):print(row['stderr'][:1400],flush=True)
    finally:
        cleanup_runtime_devnull(root,created_devnull)
    report['status']='pass' if all(r['exit_code']==0 for r in report['commands'].values()) else 'fail'
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(report,indent=2,sort_keys=True)+'\n')
    return 0 if report['status']=='pass' else 1

if __name__=='__main__':raise SystemExit(main())
