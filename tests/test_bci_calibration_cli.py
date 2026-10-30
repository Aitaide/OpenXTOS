"""Real C subprocess checks for the offline calibration file helper."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
E = ROOT / '.build/xtos-roadmap-v1/calibration-cli'
SRC = ROOT / 'tools/xtos-cyton'


class CalibrationCLI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        E.mkdir(parents=True, exist_ok=True)
        harness = E / 'main.c'
        harness.write_text('#include "bci_calibration_cli.h"\nint main(int argc,char **argv){return bci_calibration_cli_main(argc,argv);}\n')
        cls.binary = E / ('cli-' + os.environ.get('BCI_TEST_MODE', 'normal'))
        flags = ['-O2']
        if os.environ.get('BCI_TEST_MODE') == 'sanitize':
            flags = ['-O1', '-g', '-fsanitize=address,undefined', '-fno-sanitize-recover=all', '-fno-omit-frame-pointer', '-no-pie']
        result = subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', *flags, '-I', str(SRC), str(harness), str(SRC / 'bci_calibration_cli.c'), str(SRC / 'bci_calibration.c'), '-lm', '-o', str(cls.binary)], capture_output=True, text=True)
        assert result.returncode == 0, 'CLI helper must compile: ' + result.stderr

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=E)
        self.addCleanup(self.temp.cleanup)
        self.d = Path(self.temp.name)
        self.session = self.d / 'session'
        self.firmware = self.d / 'firmware'
        self.points = self.d / 'pairs.csv'
        self.model = self.d / 'new.model'
        self.session.write_bytes(b'fixture session descriptor\n')
        self.firmware.write_bytes(bytes(range(256)))
        self.points.write_bytes(self.pairs())

    @staticmethod
    def pairs():
        return ''.join(','.join(map(str, [x] * 8 + [2*x+7] * 8)) + '\n' for x in (-100, 0, 100)).encode()

    def context(self):
        return ['--session-file', str(self.session), '--firmware-file', str(self.firmware), '--reference-id', 'fixture-1', '--source', 'fixture']

    def fit_args(self):
        return ['bci', 'calibrate', '--points', str(self.points), *self.context(), '--mask', '255', '--min-span-nv', '1', '--max-residual-nv', '0', '--output', str(self.model)]

    def run_cli(self, args, **kw):
        return subprocess.run([str(self.binary), *args[1:]], capture_output=True, timeout=4, **kw)

    def fit(self):
        result = self.run_cli(self.fit_args())
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def apply_args(self, raw, out):
        return ['bci', 'apply-calibration', '--model', str(self.model), '--input', str(raw), *self.context(), '--output', str(out)]

    def test_apply_roundtrip(self):
        self.fit()
        raw, out = self.d / 'raw.csv', self.d / 'corrected.csv'
        original = b'-10,-3,-1,0,1,3,10,100\n'
        raw.write_bytes(original)
        result = self.run_cli(self.apply_args(raw, out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(out.read_bytes(), b'-13,1,5,7,9,13,27,207\n')
        self.assertEqual(raw.read_bytes(), original)
        self.assertFalse(json.loads(result.stdout)['physical_acceptance'])

    def test_context_mismatch_preserves_output_absence(self):
        self.fit()
        raw, out = self.d / 'raw', self.d / 'out'
        raw.write_bytes(b'0,0,0,0,0,0,0,0\n')
        for target in (self.session, self.firmware):
            original = target.read_bytes()
            target.write_bytes(original + b'x')
            with self.subTest(target=target.name):
                result = self.run_cli(self.apply_args(raw, out))
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(out.exists())
            target.write_bytes(original)
        for key, value in [('--source', 'external-reference'), ('--reference-id', 'other')]:
            args = self.apply_args(raw, out)
            args[args.index(key)+1] = value
            self.assertEqual(self.run_cli(args).returncode, 2)
            self.assertFalse(out.exists())

    def test_model_tamper_and_strict_structure(self):
        self.fit()
        original = self.model.read_bytes()
        raw, out = self.d / 'raw', self.d / 'out'
        raw.write_bytes(b'0,0,0,0,0,0,0,0\n')
        prefix = original.rsplit(b'sha256=', 1)[0]
        def seal(p):
            return p + b'sha256=' + hashlib.sha256(p).hexdigest().encode() + b'\n'
        mutations = [original.replace(b'scale_0=0x1p+1', b'scale_0=0x1p+0'),
                     original + b'\n', original.replace(b'version=1', b'version=\x00'),
                     seal(prefix.replace(b'version=1', b'version=2')),
                     seal(prefix.replace(b'scale_0=0x1p+1', b'scale_0=nan')),
                     seal(prefix.replace(b'point_count=3', b'point_count=65')),
                     seal(prefix.replace(b'selected_mask=255', b'selected_mask=0')),
                     seal(prefix + b'unknown=1\n'), seal(prefix.replace(b'version=1', b'Version=1'))]
        for i, mutation in enumerate(mutations):
            with self.subTest(mutation=i):
                self.model.write_bytes(mutation)
                self.assertEqual(self.run_cli(self.apply_args(raw, out)).returncode, 2)
                self.assertFalse(out.exists())
        # A separately valid model with a new digest is integrity, not authenticity.
        self.model.write_bytes(seal(prefix.replace(b'scale_0=0x1p+1', b'scale_0=0x1p+0')))
        self.assertEqual(self.run_cli(self.apply_args(raw, out)).returncode, 0)

    def test_bad_csv_never_creates_output(self):
        valid = self.pairs()
        mutations = [b'', b'\n', b'header\n', valid + b'\n', valid + b'junk', valid.replace(b',', b';', 1), valid.replace(b'-100', b'2147483648', 1), valid.replace(b'-100', b'-2147483649', 1), valid.replace(b'-100', b'1.0', 1), valid.replace(b'-100', b'\x00', 1), valid[:-1], valid * 22, valid.replace(b'-100', b' 1', 1)]
        for i, data in enumerate(mutations):
            with self.subTest(mutation=i):
                self.points.write_bytes(data)
                self.assertEqual(self.run_cli(self.fit_args()).returncode, 2)
                self.assertFalse(self.model.exists())

    def test_arguments_strict(self):
        good = self.fit_args()
        cases = [good + ['--unknown', '1'], good + ['--mask', '1'], good[:-1], good[0:2] + good[4:]]
        for key, value in [('--mask','0'), ('--mask','256'), ('--mask','4294967296'), ('--mask','+1'), ('--min-span-nv','0'), ('--min-span-nv','4294967296'), ('--max-residual-nv','-1'), ('--source','physical'), ('--reference-id','bad id')]:
            args = good.copy()
            args[args.index(key)+1] = value
            cases.append(args)
        for args in cases:
            with self.subTest(args=args):
                self.assertEqual(self.run_cli(args).returncode, 2)
                self.assertFalse(self.model.exists())

    def test_leaf_symlinks_fifo_directory_and_limits(self):
        for target, limit in [(self.session,1048576),(self.firmware,1048576),(self.points,1048576)]:
            original = target.read_bytes()
            real = self.d / (target.name + '.real')
            real.write_bytes(original)
            target.unlink()
            for kind in ('symlink', 'fifo', 'directory', 'oversize'):
                with self.subTest(target=target.name, kind=kind):
                    if kind == 'symlink': target.symlink_to(real)
                    elif kind == 'fifo': os.mkfifo(target)
                    elif kind == 'directory': target.mkdir()
                    else: target.write_bytes(b'0' * (limit+1))
                    self.assertEqual(self.run_cli(self.fit_args()).returncode, 2)
                    self.assertFalse(self.model.exists())
                    if kind == 'directory': target.rmdir()
                    else: target.unlink()
            target.write_bytes(original)

    def test_create_only_and_immutable_sources(self):
        before = {p:p.read_bytes() for p in (self.points,self.session,self.firmware)}
        self.model.write_bytes(b'caller sentinel')
        self.assertEqual(self.run_cli(self.fit_args()).returncode, 2)
        self.assertEqual(self.model.read_bytes(), b'caller sentinel')
        self.model.unlink()
        self.model.symlink_to(self.points)
        self.assertEqual(self.run_cli(self.fit_args()).returncode, 2)
        self.model.unlink()
        self.fit()
        raw = self.d / 'raw'
        raw.write_bytes(b'0,0,0,0,0,0,0,0\n')
        out = self.d / 'out'
        out.write_bytes(b'caller sentinel')
        self.assertEqual(self.run_cli(self.apply_args(raw,out)).returncode, 2)
        self.assertEqual(out.read_bytes(), b'caller sentinel')
        for path, content in before.items(): self.assertEqual(path.read_bytes(),content)

    def test_apply_bad_csv_and_overflow(self):
        self.fit()
        raw, out = self.d / 'raw', self.d / 'out'
        row = b'0,0,0,0,0,0,0,0\n'
        for data in [b'',row+b'\n',row[:-1],row.replace(b'0',b'\x00',1),row*4097,b'2147483647,0,0,0,0,0,0,0\n',b'0'*524289]:
            with self.subTest(length=len(data)):
                raw.write_bytes(data)
                self.assertEqual(self.run_cli(self.apply_args(raw,out)).returncode, 2)
                self.assertFalse(out.exists())
        raw.write_bytes(row*4096)
        self.assertEqual(self.run_cli(self.apply_args(raw,out)).returncode, 0)
        self.assertEqual(len(out.read_bytes().splitlines()),4096)

    def test_stdout_failure_retains_valid_data(self):
        with open('/dev/full','wb') as full:
            result = subprocess.run([str(self.binary),*self.fit_args()[1:]],stdout=full,stderr=subprocess.PIPE,timeout=4)
        self.assertEqual(result.returncode,3,result.stderr)
        self.assertIn(b'stdout',result.stderr)
        self.assertTrue(self.model.exists())

    def test_sha256_known_answers(self):
        harness = E / 'sha-main.c'
        harness.write_text('#include <stdio.h>\n#include "bci_sha256.h"\nint main(void){unsigned char b[2048];size_t n=fread(b,1,sizeof(b),stdin);char h[65];bci_sha256_hex(b,n,h);puts(h);return 0;}\n')
        flags = ['-O2'] if os.environ.get('BCI_TEST_MODE') != 'sanitize' else ['-O1','-g','-fsanitize=address,undefined','-fno-sanitize-recover=all','-no-pie']
        binary = E / ('sha-' + os.environ.get('BCI_TEST_MODE','normal'))
        subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror',*flags,'-I',str(SRC),str(harness),'-o',str(binary)],check=True)
        vectors = [b'',b'abc',*[b'a'*n for n in (55,56,64,65)],b'\x80',bytes(range(256))]
        results = []
        for data in vectors:
            result = subprocess.run([str(binary)],input=data,capture_output=True,timeout=4)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout.strip().decode(),hashlib.sha256(data).hexdigest())
            results.append({'length':len(data),'sha256':hashlib.sha256(data).hexdigest()})
        (E / ('sha-oracle-' + os.environ.get('BCI_TEST_MODE','normal') + '.json')).write_text(json.dumps(results,indent=2)+'\n')

    def test_apply_file_type_guards_and_model_limit(self):
        self.fit()
        raw, out = self.d / 'raw', self.d / 'out'
        raw.write_bytes(b'0,0,0,0,0,0,0,0\n')
        for path in (self.model, raw):
            saved = path.read_bytes()
            real = self.d / (path.name + '.real')
            real.write_bytes(saved)
            path.unlink()
            for kind in ('symlink','fifo','directory','oversize'):
                with self.subTest(path=path.name,kind=kind):
                    if kind == 'symlink': path.symlink_to(real)
                    elif kind == 'fifo': os.mkfifo(path)
                    elif kind == 'directory': path.mkdir()
                    else: path.write_bytes(b'x' * (8193 if path == self.model else 524289))
                    self.assertEqual(self.run_cli(self.apply_args(raw,out)).returncode,2)
                    self.assertFalse(out.exists())
                    if kind == 'directory': path.rmdir()
                    else: path.unlink()
            path.write_bytes(saved)

    def test_data_write_failure_retains_partial_file(self):
        import resource
        import signal
        def restrict():
            signal.signal(signal.SIGXFSZ,signal.SIG_IGN)
            resource.setrlimit(resource.RLIMIT_FSIZE,(16,16))
        result = subprocess.run([str(self.binary),*self.fit_args()[1:]],capture_output=True,timeout=4,preexec_fn=restrict)
        self.assertEqual(result.returncode,3,result.stderr)
        self.assertIn(b'file retained',result.stderr)
        self.assertEqual(self.model.stat().st_size,16)

    def test_identity_mask_boundaries_and_large_offset(self):
        # Identity accepts complete int32 range and mask=1 passes other channels.
        self.points.write_bytes(b''.join((','.join(map(str,[x]*16))+'\n').encode() for x in (-2147483648,0,2147483647)))
        args = self.fit_args()
        args[args.index('--mask')+1] = '1'
        args[args.index('--min-span-nv')+1] = '4294967295'
        args[args.index('--max-residual-nv')+1] = '4294967295'
        self.assertEqual(self.run_cli(args).returncode,0)
        raw,out = self.d/'raw',self.d/'out'
        data = b'-2147483648,2147483647,-1,0,1,2,3,4\n'
        raw.write_bytes(data)
        self.assertEqual(self.run_cli(self.apply_args(raw,out)).returncode,0)
        self.assertEqual(out.read_bytes(),data)
        self.model.unlink(); out.unlink()
        # Zero is outside this fit's useful domain. A dummy-zero structural
        # apply would overflow despite the real frames being valid.
        self.points.write_bytes(b''.join((','.join(map(str,[x]*8+[x+3000000000]*8))+'\n').encode() for x in (-2000000000,-1900000000,-1800000000)))
        args = self.fit_args()
        self.assertEqual(self.run_cli(args).returncode,0)
        raw.write_bytes(b'-1900000000,-1900000000,-1900000000,-1900000000,-1900000000,-1900000000,-1900000000,-1900000000\n')
        self.assertEqual(self.run_cli(self.apply_args(raw,out)).returncode,0)
        self.assertEqual(out.read_bytes(),b'1100000000,1100000000,1100000000,1100000000,1100000000,1100000000,1100000000,1100000000\n')

    def test_all_required_options_once(self):
        self.fit()
        raw,out = self.d/'raw',self.d/'out'
        raw.write_bytes(b'0,0,0,0,0,0,0,0\n')
        for base in (self.fit_args(),self.apply_args(raw,out)):
            for i in range(2,len(base),2):
                with self.subTest(command=base[1],option=base[i]):
                    self.assertEqual(self.run_cli(base[:i]+base[i+2:]).returncode,2)
                    self.assertEqual(self.run_cli(base+base[i:i+2]).returncode,2)
        self.assertFalse(out.exists())

    def test_external_source_64_points_and_rounding_oracle(self):
        self.points.write_bytes(b''.join((','.join(map(str,[2*x]*8+[x]*8))+'\n').encode() for x in range(-32,32)))
        args = self.fit_args()
        args[args.index('--source')+1] = 'external-reference'
        self.assertEqual(self.run_cli(args).returncode,0)
        model = self.model.read_bytes()
        self.assertIn(b'point_count=64\n',model)
        raw,out = self.d/'raw',self.d/'out'
        frames = [[-7,-5,-3,-1,1,3,5,7],[-2147483648,2147483647,0,2,-2,4,-4,6]]
        raw.write_bytes(''.join(','.join(map(str,row))+'\n' for row in frames).encode())
        apply = self.apply_args(raw,out)
        apply[apply.index('--source')+1] = 'external-reference'
        result = self.run_cli(apply)
        self.assertEqual(result.returncode,0,result.stderr)
        expected = [[(1 if x >= 0 else -1)*((abs(x)+1)//2) for x in row] for row in frames]
        actual = [[int(v) for v in line.split(',')] for line in out.read_text().splitlines()]
        self.assertEqual(actual,expected)
        evidence = {'input':frames,'expected':expected,'actual':actual,'physical_acceptance':False,'model_sha256':hashlib.sha256(model).hexdigest()}
        (E/('rounding-oracle-'+os.environ.get('BCI_TEST_MODE','normal')+'.json')).write_text(json.dumps(evidence,indent=2)+'\n')

    def test_apply_stdout_failure_and_broken_pipe(self):
        self.fit()
        raw,out = self.d/'raw',self.d/'out'
        raw.write_bytes(b'0,0,0,0,0,0,0,0\n')
        args = self.apply_args(raw,out)
        with open('/dev/full','wb') as full:
            result = subprocess.run([str(self.binary),*args[1:]],stdout=full,stderr=subprocess.PIPE,timeout=4)
        self.assertEqual(result.returncode,3,result.stderr)
        self.assertEqual(out.read_bytes(),b'7,7,7,7,7,7,7,7\n')
        out.unlink()
        read_fd,write_fd = os.pipe()
        os.close(read_fd)
        try:
            result = subprocess.run([str(self.binary),*args[1:]],stdout=write_fd,stderr=subprocess.PIPE,timeout=4)
        finally:
            os.close(write_fd)
        self.assertEqual(result.returncode,3,result.stderr)
        self.assertEqual(out.read_bytes(),b'7,7,7,7,7,7,7,7\n')

    def test_model_records_measured_spans(self):
        self.fit()
        text = self.model.read_text()
        self.assertIn('observed_span_nv_0=200\n', text)
        self.assertIn('reference_span_nv_0=400\n', text)

    def test_valid_fit_binds_actual_files(self):
        result = self.fit()
        summary = json.loads(result.stdout)
        self.assertFalse(summary['physical_acceptance'])
        model = self.model.read_bytes()
        for name, data in [('session_sha256', self.session.read_bytes()), ('firmware_sha256', self.firmware.read_bytes()), ('pairs_sha256', self.points.read_bytes())]:
            self.assertIn((name + '=' + hashlib.sha256(data).hexdigest() + '\n').encode(), model)
        prefix, digest = model.rsplit(b'sha256=', 1)
        self.assertEqual(digest, hashlib.sha256(prefix).hexdigest().encode() + b'\n')
        self.assertIn(b'scale_0=0x1p+1\n', model)
        self.assertEqual(self.model.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
