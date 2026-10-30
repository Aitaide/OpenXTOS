"""Real standalone CLI + frozen serial/estimator, stateful synthetic PTY only."""
import importlib.util
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / 'tools/xtos-cyton'
E = ROOT / '.build/xtos-roadmap-v1/diagnostic-cli/integration'
spec = importlib.util.spec_from_file_location('diagnostic_board', HERE / 'test_cyton_diagnostic_serial.py')
board_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(board_module)
Board = board_module.Board

MAIN = r'''
#define _POSIX_C_SOURCE 200809L
#include "bci_diagnostic_cli.h"
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/resource.h>
#include <errno.h>
#include <string.h>
#include <unistd.h>
ssize_t __real_write(int fd, const void *buffer, size_t count);
ssize_t __wrap_write(int fd, const void *buffer, size_t count) {
    static int pending;
    const char *fault = getenv("CLI_TEST_PARTIAL");
    if (pending == 1 && fault && fault[0] == '2') { pending = 2; errno = EIO; return -1; }
    if (!pending && fault && count > 1 && ((const char *)buffer)[0] == 'z') {
        pending = 1;
        return __real_write(fd, buffer, 2);
    }
    return __real_write(fd, buffer, count);
}
static void prior(int s) { (void)s; }
int main(int argc, char **argv) {
    struct sigaction a = {0}, got;
    int rc;
    a.sa_handler = prior; sigemptyset(&a.sa_mask);
    sigaction(SIGINT, &a, 0); sigaction(SIGTERM, &a, 0); sigaction(SIGPIPE, &a, 0);
    if (getenv("CLI_TEST_FILE_LIMIT")) {
        struct rlimit limit = {128, 128};
        signal(SIGXFSZ, SIG_IGN);
        if (setrlimit(RLIMIT_FSIZE, &limit)) return 94;
    }
    rc = bci_diagnostic_cli_main(argc, argv);
    if (sigaction(SIGINT, 0, &got) || got.sa_handler != prior) return 91;
    if (sigaction(SIGTERM, 0, &got) || got.sa_handler != prior) return 92;
    if (sigaction(SIGPIPE, 0, &got) || got.sa_handler != prior) return 93;
    return rc;
}
'''

class DiagnosticCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        E.mkdir(parents=True, exist_ok=True)
        (E / 'main.c').write_text(MAIN)
        cls.exe = E / ('cli-san' if os.environ.get('BCI_DIAGNOSTIC_CLI_SANITIZE') == '1' else 'cli')
        flags = ['-fsanitize=address,undefined', '-fno-sanitize-recover=all', '-fno-omit-frame-pointer', '-no-pie'] if os.environ.get('BCI_DIAGNOSTIC_CLI_SANITIZE') == '1' else []
        cmd = ['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-pedantic', '-O1', '-g', *flags, '-I', str(HERE), str(E / 'main.c'), str(HERE / 'bci_diagnostic_cli.c'), str(HERE / 'cyton_diagnostic_serial.c'), str(HERE / 'bci_diagnostic.c'), '-Wl,--wrap=write', '-lm', '-o', str(cls.exe)]
        p = subprocess.run(cmd, capture_output=True, text=True)
        (E / ('compile-san.log' if flags else 'compile.log')).write_text(' '.join(cmd) + '\n' + p.stdout + p.stderr)
        if p.returncode:
            raise AssertionError('standalone diagnose helper unavailable:\n' + p.stderr)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=E)
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name) / 'report.json'

    def run_board(self, board, mode='shorted', channel=2, frames=32, settle=2, extra=(), stdout=subprocess.PIPE, env=None):
        args = [str(self.exe), 'diagnose', '--device', board.path, '--mode', mode, '--channel', str(channel), '--output', str(self.output), '--frames', str(frames), '--settle-frames', str(settle), '--command-timeout-ms', '180']
        for key, value in zip(extra[::2], extra[1::2]):
            if key in args:
                args[args.index(key) + 1] = value
            else:
                args.extend([key, value])
        try:
            start = time.monotonic()
            p = subprocess.Popen(args, stdout=stdout, stderr=subprocess.PIPE, text=True, env=env)
            board.proc = p
            out, err = p.communicate(timeout=13)
            return p.returncode, out, err, time.monotonic() - start
        finally:
            board.close()

    def report(self):
        return json.loads(self.output.read_text())

    def test_shorted_selected_channel_real_capture_oracle_and_restoration(self):
        board = Board(split=True)
        rc, out, err, elapsed = self.run_board(board)
        self.assertEqual(rc, 0, err)
        self.assertEqual(err, '')
        r = self.report()
        self.assertEqual(r['schema'], 'xtos.bci.diagnostic')
        self.assertEqual(r['version'], 1)
        self.assertTrue(r['result_valid'])
        self.assertEqual((r['mode'], r['channel'], r['input']), ('shorted', 2, None))
        self.assertEqual(r['firmware'], 'v3.1.5')
        self.assertEqual(r['before']['registers'], board.original)
        self.assertEqual(r['after']['registers'], board.original)
        self.assertEqual(board.regs, board.original)
        expected = board.original.copy()
        expected[6] = 0x11
        expected[15] = expected[16] = expected[21] = 0
        self.assertEqual(r['test']['registers'], expected)
        self.assertEqual(r['actual_gain'], 2)
        nv = (1000 * 4500000000) // (2 * 8388607)
        self.assertEqual(r['raw_nv'], [-nv, nv] * 16)
        self.assertEqual(r['stats']['count'], 32)
        self.assertAlmostEqual(r['stats']['mean_nv'], 0, delta=1e-8)
        self.assertAlmostEqual(r['stats']['stddev_nv'], nv)
        self.assertEqual(r['stats']['observed_peak_nv'], nv)
        self.assertIsNone(r['stats']['nominal_rms_estimate_ohm'])
        self.assertNotIn('channels', r)
        self.assertEqual(r['restoration'], 'restored')
        self.assertEqual(r['sample_count'], 32)
        self.assertEqual(r['method'], 'selected_population_stats')
        self.assertFalse(r['calibration'])
        self.assertFalse(r['physical_acceptance'])
        self.assertEqual(json.loads(out)['sample_count'], 32)
        self.assertLess(elapsed, 2)

    def test_cancel_sigint_cleanup_and_retained_report(self):
        board = Board(cancel='stream')
        rc, out, err, elapsed = self.run_board(board, mode='impedance-p')
        self.assertEqual(rc, 3, (out, err))
        r = self.report()
        self.assertFalse(r['result_valid'])
        self.assertIsNone(r['stats'])
        self.assertEqual(r['error'], 125)
        self.assertEqual(r['restoration'], 'restored')
        self.assertEqual(board.regs, board.original)
        self.assertLess(elapsed, 2)

    def impedance_oracle(self, mode):
        board = Board()
        rc, out, err, elapsed = self.run_board(board, mode=mode, channel=8)
        self.assertEqual(rc, 0, (out, err))
        r = self.report()
        nv = (1000 * 4500000000) // 8388607
        self.assertEqual(r['raw_nv'], [-nv, nv] * 16)
        self.assertAlmostEqual(r['stats']['stddev_nv'], nv)
        self.assertAlmostEqual(r['stats']['nominal_rms_estimate_ohm'], math.sqrt(2) * nv / 6 - 2200)
        self.assertEqual(r['actual_gain'], 1)
        self.assertEqual(r['input'], mode[-1].upper())
        self.assertEqual(r['method'], 'NOMINAL_CURRENT_RMS')
        self.assertEqual(r['assumptions']['nominal_frequency_hz'], 31.25)
        self.assertEqual(r['assumptions']['current_tolerance_percent'], 20)
        self.assertFalse(r['assumptions']['gui_preprocessing'])
        self.assertEqual(r['test']['registers'][15:17], [128, 0] if mode.endswith('p') else [0, 128])
        for phase in ('before', 'test', 'after'):
            self.assertEqual(r[phase]['register_mask'], 0xffffff)
            self.assertEqual(len(r[phase]['registers']), 24)
            self.assertEqual(len(r[phase]['gains']), 8)
        self.assertEqual(board.regs, board.original)
        self.assertEqual(r['after']['registers'], board.original)
        self.assertGreater(r['first_receive_ns'], 0)
        self.assertGreaterEqual(r['last_receive_ns'], r['first_receive_ns'])
        self.assertEqual(r['timestamp_resolution_ns'], 1000000)

    def test_impedance_p_selected_oracle(self):
        self.impedance_oracle('impedance-p')

    def test_impedance_n_selected_oracle(self):
        self.impedance_oracle('impedance-n')

    def failure(self, **kw):
        board = Board(**kw)
        rc, out, err, elapsed = self.run_board(board, mode='impedance-p')
        self.assertEqual(rc, 3, (out, err))
        r = self.report()
        self.assertFalse(r['result_valid'])
        self.assertIsNone(r['stats'])
        self.assertNotEqual(r['error'], 0)
        self.assertLess(elapsed, 3)
        return board, r

    def test_malformed_config_never_mutates(self):
        b, r = self.failure(malformed=lambda data: data.replace(b'CONFIG4,', b'Unknown,'))
        self.assertFalse(any(c.startswith((b'x', b'z', b'b')) for c in b.commands))
        self.assertEqual(r['raw_nv'], [])

    def test_ack_without_effect_never_starts(self):
        b, r = self.failure(no_x_effect=True)
        self.assertNotIn(b'b', b.commands)

    def test_bad_ack_never_starts(self):
        b, r = self.failure(bad_ack=True)
        self.assertNotIn(b'b', b.commands)

    def test_counter_gap_retains_partial_samples(self):
        b, r = self.failure(gap=True)
        self.assertEqual(r['counter_errors'], 1)
        self.assertEqual(len(r['raw_nv']), 3)
        self.assertEqual(r['sample_count'], 3)
        self.assertEqual(b.regs, b.original)

    def test_rail_retains_partial_samples(self):
        b, r = self.failure(rail=True)
        self.assertEqual(r['rail_errors'], 1)
        self.assertEqual(len(r['raw_nv']), 3)
        self.assertEqual(b.regs, b.original)

    def test_bad_frame_retains_partial_samples(self):
        b, r = self.failure(bad_frame=True)
        self.assertEqual(r['bad_frames'], 1)
        self.assertEqual(len(r['raw_nv']), 3)

    def test_restoration_mismatch_withholds_stats(self):
        b, r = self.failure(restore_mismatch=True)
        self.assertEqual(r['restoration'], 'failed')
        self.assertNotEqual(r['cleanup_errno'], 0)
        self.assertEqual(len(r['raw_nv']), 32)
        self.assertNotEqual(r['before']['registers'], r['after']['registers'])

    def test_disconnect_never_claims_restored(self):
        b, r = self.failure(loss=True)
        self.assertEqual(r['restoration'], 'unknown')
        self.assertNotEqual(r['cleanup_errno'], 0)

    def test_existing_file_preserved_without_device_commands(self):
        self.output.write_text('sentinel')
        b = Board()
        rc, _, _, _ = self.run_board(b)
        self.assertEqual(rc, 2)
        self.assertEqual(self.output.read_text(), 'sentinel')
        self.assertEqual(b.commands, [])

    def test_symlink_preserved_without_device_commands(self):
        target = self.output.with_name('sentinel')
        target.write_text('sentinel')
        self.output.symlink_to(target)
        b = Board()
        rc, _, _, _ = self.run_board(b)
        self.assertEqual(rc, 2)
        self.assertEqual(target.read_text(), 'sentinel')
        self.assertTrue(self.output.is_symlink())
        self.assertEqual(b.commands, [])

    def test_fifo_refused_without_blocking(self):
        os.mkfifo(self.output)
        b = Board()
        rc, _, _, elapsed = self.run_board(b)
        self.assertEqual(rc, 2)
        self.assertEqual(b.commands, [])
        self.assertLess(elapsed, 1)

    def test_invalid_arguments_before_output_and_device(self):
        for extra in (['--frames', '31'], ['--frames', '4097'], ['--command-timeout-ms', '3001'], ['--channel', '0'], ['--settle-frames', '1025']):
            with self.subTest(extra=extra):
                b = Board()
                rc, _, _, _ = self.run_board(b, extra=extra)
                self.assertEqual(rc, 2)
                self.assertFalse(self.output.exists())
                self.assertEqual(b.commands, [])
        p = subprocess.run([str(self.exe), 'diagnose'], capture_output=True)
        self.assertEqual(p.returncode, 2)
        self.assertFalse(self.output.exists())

    def test_stdout_full_returns_failure_keeps_valid_report(self):
        with open('/dev/full', 'w') as full:
            b = Board()
            rc, _, err, _ = self.run_board(b, stdout=full)
        self.assertEqual(rc, 3)
        self.assertIn('summary output failed', err)
        self.assertTrue(self.report()['result_valid'])
        self.assertEqual(b.regs, b.original)

    def test_report_write_failure_returns_failure_after_cleanup(self):
        env = dict(os.environ, CLI_TEST_FILE_LIMIT='1')
        b = Board()
        rc, out, err, _ = self.run_board(b, env=env)
        self.assertEqual(rc, 3)
        self.assertIn('report output failed', err)
        self.assertFalse(json.loads(out)['result_valid'])
        self.assertEqual(b.regs, b.original)
        self.assertEqual(self.output.stat().st_size, 128)

    def test_maximum_bounds_and_counter_wrap(self):
        b = Board(frame_count=5120)
        rc, _, err, _ = self.run_board(b, frames=4096, settle=1024)
        self.assertEqual(rc, 0, err)
        r = self.report()
        self.assertEqual((r['sample_count'], r['settled_frames'], r['frames_observed']), (4096, 1024, 5120))
        self.assertEqual(len(r['raw_nv']), 4096)
        self.assertEqual(r['counter_errors'], 0)
        self.assertEqual(b.regs, b.original)

    def test_command_timeout_is_finite(self):
        b, r = self.failure(version=b'')
        self.assertEqual(b.commands, [b's', b'V'])
        self.assertEqual(r['error'], 110)

    def test_cancel_sigterm_after_actual_samples_restores(self):
        class CancelBoard(Board):
            def command(self, c):
                if c != b'b':
                    return super().command(c)
                self.commands.append(c)
                self.started.set()
                self.send(b''.join(board_module.packet(i, (-1000 if i % 2 == 0 else 1000)) for i in range(6)))
                time.sleep(.04)
                os.kill(self.proc.pid, signal.SIGTERM)
        b = CancelBoard()
        rc, _, _, _ = self.run_board(b)
        self.assertEqual(rc, 3)
        r = self.report()
        self.assertEqual(r['sample_count'], 4)
        self.assertEqual(r['restoration'], 'restored')
        self.assertIsNone(r['stats'])
        self.assertEqual(r['error'], 125)
        self.assertEqual(b.regs, b.original)

    def test_high_gain_shorted_conversion(self):
        b = Board()
        rc, _, err, _ = self.run_board(b, channel=7, settle=0)
        self.assertEqual(rc, 0, err)
        r = self.report()
        nv = (1000 * 4500000000) // (24 * 8388607)
        self.assertEqual(r['actual_gain'], 24)
        self.assertEqual(r['raw_nv'], [-nv, nv] * 16)
        self.assertAlmostEqual(r['stats']['stddev_nv'], nv)

    def test_no_leadoff_effect_never_starts(self):
        b, r = self.failure(no_z_effect=True)
        self.assertNotIn(b'b', b.commands)

    def test_partial_command_error_retained_and_restored(self):
        b = Board()
        rc, _, err, elapsed = self.run_board(b, env=dict(os.environ, CLI_TEST_PARTIAL='2'))
        self.assertEqual(rc, 3, err)
        r = self.report()
        self.assertEqual(r['partial_command'], 1)
        self.assertEqual(r['error'], 5)
        self.assertEqual(r['restoration'], 'restored')
        self.assertIsNone(r['stats'])
        self.assertEqual(b.regs, b.original)
        self.assertGreater(elapsed, 1.1)
        self.assertLess(elapsed, 3)

    def test_partial_write_contiguous_retry_succeeds(self):
        b = Board()
        rc, _, err, _ = self.run_board(b, env=dict(os.environ, CLI_TEST_PARTIAL='1'))
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.report()['result_valid'])
        self.assertEqual(b.regs, b.original)

    def test_broken_stdout_pipe_returns_failure(self):
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        try:
            b = Board()
            rc, _, err, _ = self.run_board(b, stdout=write_fd)
        finally:
            os.close(write_fd)
        self.assertEqual(rc, 3)
        self.assertIn('summary output failed', err)
        self.assertTrue(self.report()['result_valid'])
        self.assertEqual(b.regs, b.original)

if __name__ == '__main__':
    unittest.main(verbosity=2)
