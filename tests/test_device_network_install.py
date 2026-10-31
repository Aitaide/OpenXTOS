"""Exercise the installed TCP API without checkout module shadowing."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DeviceNetworkInstallTests(unittest.TestCase):
    def test_source_package_installs_tcp_and_runs_real_loopback(self):
        spec = importlib.util.spec_from_file_location('network_test_installer', ROOT/'scripts/install_devices.py')
        if spec is None or spec.loader is None:
            raise RuntimeError('installer module unavailable')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        self.assertEqual(installer.SOURCES.get('network_transport.py'),
                         'tools/xtos-devices/network_transport.py',
                         'explicit TCP source missing from installed device API')
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            result = installer.install(stage, ROOT)
            package = stage/installer.DESTINATION
            module = package/'network_transport.py'
            expected = hashlib.sha256((ROOT/'tools/xtos-devices/network_transport.py').read_bytes()).hexdigest()
            self.assertEqual(hashlib.sha256(module.read_bytes()).hexdigest(), expected)
            self.assertEqual(result['source_sha256']['network_transport.py'], expected)
            self.assertEqual(module.stat().st_mode & 0o777, 0o644)
            self.assertFalse(result['physical_device_evidence'])
            self.assertFalse(result['authority_initialized'])
            self.assertFalse(result['fixture_or_test_sources_installed'])
            code = textwrap.dedent('''
                from pathlib import Path
                import socket, sys, time
                package = Path(sys.argv[1]).resolve()
                sys.path.insert(0, str(package))
                import network_transport as module
                if Path(module.__file__).resolve() != package/'network_transport.py':
                    raise RuntimeError('checkout module shadowed installed API')
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                listener.settimeout(2)
                transport = module.TCPTransport('127.0.0.1', 1)
                if transport.connected:
                    raise RuntimeError('constructor performed I/O')
                peer = None
                def deadline(): return time.monotonic_ns() + 2_000_000_000
                try:
                    listener.bind(('127.0.0.1', 0)); listener.listen(1)
                    transport = module.TCPTransport('127.0.0.1', listener.getsockname()[1])
                    transport.open(deadline_ns=deadline())
                    peer, _ = listener.accept(); peer.settimeout(2)
                    peer.sendall(b'fixture')
                    received = b''
                    limit = deadline()
                    while len(received) < 7:
                        received += transport.read(7-len(received), deadline_ns=limit)
                    if received != b'fixture': raise RuntimeError('installed read changed bytes')
                    transport.write_all(b'client', deadline_ns=deadline())
                    received = b''
                    while len(received) < 6:
                        part = peer.recv(6-len(received))
                        if not part: raise RuntimeError('installed write ended before complete payload')
                        received += part
                    if received != b'client': raise RuntimeError('installed write changed bytes')
                    transport.request_cancel()
                    try: transport.read(1, deadline_ns=deadline())
                    except module.NetworkCancelled: pass
                    else: raise RuntimeError('installed cancellation ignored')
                finally:
                    transport.close()
                    if peer is not None: peer.close()
                    listener.close()
                if transport.connected: raise RuntimeError('installed socket survived close')
                print('INSTALLED_TCP_LOOPBACK=PASS')
            ''')
            for flags in ([], ['-O']):
                run = subprocess.run([sys.executable, '-I', '-B', *flags, '-c', code, str(package)],
                                     capture_output=True, text=True, timeout=15)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual(run.stdout.strip(), 'INSTALLED_TCP_LOOPBACK=PASS')
            self.assertEqual(installer.install(stage, ROOT), result)
            stored = json.loads((stage/installer.RECORD).read_bytes())
            self.assertEqual(stored, result)


if __name__ == '__main__':
    unittest.main()
