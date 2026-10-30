"""Native bci end-to-end CLI tests; synthetic input is explicitly fixture data."""
import json
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / 'tools/xtos-cyton'


def packet(counter, values=None, footer=0xC0):
    values = values or list(range(1, 9))
    return bytes([0xA0, counter]) + b''.join((v & 0xFFFFFF).to_bytes(3, 'big') for v in values) + bytes(6) + bytes([footer])


class BciCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.tmp.name) / 'bci'
        command = ['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-pthread',
                   '-I' + str(ROOT / 'sdk/libxtos-rt/include'),
                   '-I' + str(ROOT / 'sdk/libxtos-signal/include'),
                   '-I' + str(ROOT / 'sdk/libxtos-bci/include'),
                   str(HERE / 'main.c'), str(HERE / 'bci_stream.c'), str(HERE / 'cyton_serial.c'),
                   str(HERE / 'bci_config_cli.c'), str(ROOT / 'sdk/libxtos-bci/src/session.c'),
                   str(HERE / 'bci_calibration_cli.c'), str(HERE / 'bci_calibration.c'),
                   str(HERE / 'bci_diagnostic_cli.c'), str(HERE / 'cyton_diagnostic_serial.c'), str(HERE / 'bci_diagnostic.c'),
                   str(ROOT / 'sdk/libxtos-rt/src/spsc.c'), str(ROOT / 'sdk/libxtos-signal/src/signal.c'),
                   '-lm', '-o', str(cls.binary)]
        subprocess.run(command, check=True, capture_output=True, text=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def invoke(self, data, extra=None, existing=False):
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp) / 'input.bin', Path(tmp) / 'out.jsonl'
            source.write_bytes(data)
            if existing:
                output.write_text('sentinel\n')
            result = subprocess.run([str(self.binary), '--input', str(source), '--output', str(output),
                                     * (extra or [])], capture_output=True, text=True, timeout=10)
            text = output.read_text() if output.exists() else ''
            return result, text

    def test_eight_channels_streamed_and_summary_honest(self):
        result, text = self.invoke(b''.join(packet(i) for i in range(20)))
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        frames = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(summary['source_mode'], 'fixture')
        self.assertFalse(summary['physical_evidence'])
        self.assertEqual(summary['processed'], 20)
        self.assertEqual(len(frames), 20)
        self.assertEqual([f['sequence'] for f in frames], list(range(20)))
        for f in frames:
            self.assertEqual(f['schema'], 'xtos-bci-stream-v1')
            self.assertEqual(len(f['samples_nv']), 8)
            self.assertEqual(len(f['filtered_nv']), 8)
            self.assertEqual(f['gain'], [24] * 8)
            self.assertGreater(f['receive_ns'], 0)
            self.assertGreaterEqual(f['processed_ns'], f['receive_ns'])

    def test_gaps_reject_clean_success_and_reset_filter(self):
        result, text = self.invoke(packet(1) + packet(4))
        self.assertEqual(result.returncode, 3, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary['missing_frames'], 2)
        frames = [json.loads(line) for line in text.splitlines()]
        self.assertEqual([f['sequence'] for f in frames], [0, 3])
        self.assertTrue(frames[1]['filter_reset'])
        self.assertTrue(frames[1]['quality'] & 1)

    def test_invalid_footer_and_partial_are_degraded(self):
        for data in (packet(0, footer=0xB0) + packet(1), packet(0) + packet(1)[:16]):
            with self.subTest(data=data):
                result, _ = self.invoke(data)
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertEqual(json.loads(result.stdout)['status'], 'degraded')

    def test_existing_output_is_preserved(self):
        result, text = self.invoke(packet(0), existing=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(text, 'sentinel\n')

    def test_reconnect_number_range_is_refused(self):
        for attempts in ('-1', '33', '4294967296'):
            result, _ = self.invoke(packet(0), extra=['--reconnect-attempts', attempts])
            self.assertEqual(result.returncode, 2)

    def test_gain24_signed_endpoints(self):
        result, text = self.invoke(packet(0, [-8388608, 8388607, -1, 0, 1, 100, -100, 2]))
        self.assertEqual(result.returncode, 0, result.stderr)
        frame=json.loads(text)
        self.assertEqual(frame['samples_nv'][:5],[-187500022,187500000,-22,0,22])

    def test_counter_wrap_is_clean(self):
        result,text=self.invoke(packet(255)+packet(0))
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual([json.loads(v)['sequence'] for v in text.splitlines()],[0,1])

    def test_noise_cannot_be_reported_as_clean_acquisition(self):
        result, text = self.invoke(b'noise' + packet(0) + b'\xff\x00')
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(json.loads(result.stdout)['discarded_bytes'], 7)
        self.assertEqual(len(text.splitlines()), 1)

    def test_duplicate_is_degraded_without_duplicate_output(self):
        result,text=self.invoke(packet(1)+packet(1)+packet(2))
        self.assertEqual(result.returncode,3,result.stderr)
        self.assertEqual(len(text.splitlines()),2)
        self.assertEqual(json.loads(result.stdout)['parser_errors'],1)

    def run_serial(self, board, args=None):
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'stream.jsonl'
            result=subprocess.run([str(self.binary),'--device',board.path,'--output',str(output),
                '--duration-ms','180','--command-timeout-ms','150','--reconnect-attempts','0',*(args or [])],
                capture_output=True,text=True,timeout=3)
            return result,[json.loads(v) for v in output.read_text().splitlines()]

    def test_serial_actual_per_channel_gain_is_used(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('serial_emulator',HERE/'test_cyton_serial.py')
        emulator=importlib.util.module_from_spec(spec);spec.loader.exec_module(emulator)
        values=[1,2,4,6,8,12,24,24]
        board=emulator.Board(payload=packet(0,[24]*8))
        try:
            result,frames=self.run_serial(board)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(len(frames),1)
            self.assertEqual(frames[0]['gain'],values)
            self.assertEqual(frames[0]['enabled_mask'],127)
            self.assertEqual(frames[0]['firmware_identity'],emulator.BANNER[:-3].decode())
            self.assertEqual(frames[0]['samples_nv'],[int(24*4500000000/(g*8388607)) for g in values])
        finally:board.close()

    def test_serial_unreadable_gain_never_emits_frame(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('serial_emulator',HERE/'test_cyton_serial.py')
        emulator=importlib.util.module_from_spec(spec);spec.loader.exec_module(emulator)
        board=emulator.Board(config=b'Board ADS Registers\r\n$$$',payload=packet(0))
        try:
            result,frames=self.run_serial(board)
            self.assertEqual(result.returncode,3,result.stderr)
            self.assertEqual(frames,[])
            self.assertNotIn(ord('b'),board.commands)
        finally:board.close()

    def test_serial_low_gain_overflow_is_rejected(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('serial_emulator',HERE/'test_cyton_serial.py')
        emulator=importlib.util.module_from_spec(spec);spec.loader.exec_module(emulator)
        board=emulator.Board(payload=packet(0,[8388607]*8))
        try:
            result,frames=self.run_serial(board)
            self.assertEqual(result.returncode,3,result.stderr)
            self.assertEqual(frames,[])
            self.assertEqual(json.loads(result.stdout)['parser_errors'],1)
        finally:board.close()

    def test_cli_reconnect_does_not_mix_generations_or_partial_packets(self):
        import importlib.util
        import time
        spec=importlib.util.spec_from_file_location('serial_emulator',HERE/'test_cyton_serial.py')
        emulator=importlib.util.module_from_spec(spec);spec.loader.exec_module(emulator)
        second=emulator.Board(config=emulator.registers([0x68]*8),payload=packet(77,[48]*8))
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'stable';output=Path(tmp)/'output'
            def disconnect(board):
                replacement=Path(tmp)/'next';replacement.symlink_to(second.path);replacement.replace(path)
                time.sleep(.03);os.close(board.master);board.done.set()
            first=emulator.Board(payload=packet(1,[24]*8)+packet(2)[:10],on_start=disconnect)
            path.symlink_to(first.path)
            try:
                result=subprocess.run([str(self.binary),'--device',str(path),'--output',str(output),
                    '--duration-ms','400','--command-timeout-ms','150','--reconnect-delay-ms','10','--reconnect-attempts','3'],
                    capture_output=True,text=True,timeout=3)
                self.assertEqual(result.returncode,3,result.stderr)
                frames=[json.loads(v) for v in output.read_text().splitlines()]
                self.assertEqual([f['generation'] for f in frames],[1,2])
                self.assertEqual([f['sequence'] for f in frames],[0,0])
                self.assertEqual(frames[1]['gain'],[24]*8)
                self.assertTrue(frames[1]['filter_reset'])
                self.assertTrue(frames[1]['quality']&2)
                summary=json.loads(result.stdout)
                self.assertEqual(summary['connections'],2)
                self.assertEqual(summary['reconnects'],1)
                self.assertEqual(summary['partial_frames'],1)
            finally:
                first.close();second.close()

    def test_interrupt_preserves_frames_and_marks_degraded(self):
        import importlib.util
        import signal
        import time
        spec=importlib.util.spec_from_file_location('serial_emulator',HERE/'test_cyton_serial.py')
        emulator=importlib.util.module_from_spec(spec);spec.loader.exec_module(emulator)
        for sig in (signal.SIGINT,signal.SIGTERM):
            with self.subTest(signal=sig), tempfile.TemporaryDirectory() as tmp:
                output=Path(tmp)/'output'
                board=emulator.Board(payload=packet(0))
                proc=None
                try:
                    proc=subprocess.Popen([str(self.binary),'--device',board.path,'--output',str(output),
                        '--duration-ms','5000','--command-timeout-ms','300','--reconnect-attempts','0'],
                        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                    self.assertTrue(board.started.wait(1))
                    # The serial start event precedes producer receipt; allow the
                    # one frame to reach the parser before requesting shutdown.
                    time.sleep(.03)
                    proc.send_signal(sig)
                    stdout,stderr=proc.communicate(timeout=1)
                    self.assertEqual(proc.returncode,3,stdout+stderr)
                    summary=json.loads(stdout)
                    self.assertEqual(summary['status'],'degraded')
                    self.assertTrue(summary['interrupted'])
                    self.assertEqual(summary['processed'],1)
                    self.assertEqual(len(output.read_text().splitlines()),1)
                    time.sleep(.025)
                    self.assertTrue(board.commands.endswith(b's'))
                finally:
                    if proc is not None and proc.poll() is None:
                        proc.kill();proc.communicate(timeout=1)
                    board.close()

    def test_summary_output_failure_is_not_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'input';output=Path(tmp)/'output'
            source.write_bytes(packet(0))
            with open('/dev/full','w') as sink:
                result=subprocess.run([str(self.binary),'--input',str(source),'--output',str(output)],
                                      stdout=sink,stderr=subprocess.PIPE,text=True,timeout=3)
            self.assertEqual(result.returncode,3)
            self.assertIn('summary',result.stderr)
            self.assertEqual(len(output.read_text().splitlines()),1)

    def test_empty_input_and_invalid_options_fail(self):
        result, _ = self.invoke(b'')
        self.assertEqual(result.returncode, 3)
        for args in (['--capacity', '0'], ['--capacity', 'abc'], ['--duration-ms', '-1']):
            result, _ = self.invoke(packet(0), extra=args)
            self.assertEqual(result.returncode, 2, result.stderr)


if __name__ == '__main__':
    unittest.main()
