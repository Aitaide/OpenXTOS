"""Native C/PTTY integration tests; fixtures follow official OpenBCI firmware.

Sources (retrieved 2026-10-27):
https://docs.openbci.com/Cyton/CytonSDK/
https://github.com/OpenBCI/OpenBCI_Cyton_Library/blob/master/OpenBCI_32bit_Library.cpp
printAllRegisters / RREGS / printRegisterName / printHex define:
CH1SET, 05, 68, 0, 1, 1, 0, 1, 0, 0, 0\r\n
https://github.com/OpenBCI/OpenBCI_Cyton_Library/blob/master/OpenBCI_32bit_Library_Definitions.h
CH1SET..CH8SET are addresses 05..0C, gain codes 0..6 occupy bits 6:4.
The fixture is a protocol emulation derived from those formatters, not a
hardware recording; identity is a firmware banner, not a unique hardware ID.
"""
import errno
import json
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import tempfile
import threading
import time
import unittest

HERE = Path(__file__).resolve().parent
BANNER = (b'OpenBCI V3 8-16 channel\r\nOn Board ADS1299 Device ID: 0x3E\r\n'
          b'LIS3DH Device ID: 0x33\r\nFirmware: v3.1.2\r\n$$$')
VALUES = [0x08, 0x18, 0x28, 0x38, 0x48, 0x58, 0x68, 0xE8]

def register_line(name, address, value):
    return (f'{name}, {address:02X}, {value:02X}, ' +
            ', '.join(str((value >> b) & 1) for b in range(7, -1, -1)) + '\r\n').encode()

def registers(values=VALUES):
    # Official ADS formatter shape; LIS3DH contents are irrelevant and omitted.
    names = ['ADS_ID', 'CONFIG1', 'CONFIG2', 'CONFIG3', 'LOFF']
    names += [f'CH{i}SET' for i in range(1, 9)]
    names += ['BIAS_SENSP', 'BIAS_SENSN', 'LOFF_SENSP', 'LOFF_SENSN',
              'LOFF_FLIP', 'LOFF_STATP', 'LOFF_STATN', 'GPIO', 'MISC1', 'MISC2', 'CONFIG4']
    data = [0x3E, 0x96, 0xC0, 0x60, 0] + list(values) + [0] * 11
    return (b'\r\nBoard ADS Registers\r\n' +
            b''.join(register_line(n, i, v) for i, (n, v) in enumerate(zip(names, data))) +
            b'\r\nLIS3DH Registers\r\n$$$')

class Board:
    def __init__(self, banner=BANNER, config=None, split=False, payload=b'\xa0\x01hello\xc0', on_start=None):
        self.master, self.slave = pty.openpty()
        self.path = os.ttyname(self.slave)
        self.banner = banner
        self.config = registers() if config is None else config
        self.split, self.payload, self.on_start = split, payload, on_start
        self.commands = bytearray()
        self.done = threading.Event()
        self.started = threading.Event()
        self.errors = []
        self.thread = threading.Thread(target=self.serve)
        self.thread.start()

    def send(self, reply):
        if reply is None:
            return
        chunks = [reply[i:i+7] for i in range(0, len(reply), 7)] if self.split else [reply]
        for chunk in chunks:
            os.write(self.master, chunk)
            if self.split:
                time.sleep(0.0002)

    def serve(self):
        try:
            while not self.done.is_set():
                if not select.select([self.master], [], [], .02)[0]:
                    continue
                data = os.read(self.master, 4096)
                for byte in data:
                    self.commands.append(byte)
                    if byte == ord('v'):
                        self.send(self.banner)
                    elif byte == ord('?'):
                        self.send(self.config)
                    elif byte == ord('b'):
                        self.started.set()
                        self.send(self.payload)
                        if self.on_start:
                            self.on_start(self)
        except OSError as exc:
            if not self.done.is_set() and exc.errno not in (errno.EIO, errno.EBADF):
                self.errors.append(repr(exc))

    def close(self):
        self.done.set()
        self.thread.join(1)
        assert not self.thread.is_alive()
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass
        assert not self.errors, self.errors

class SerialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='cyton-native-')
        cls.exe = Path(cls.tmp.name) / 'serial-fixture'
        result = subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-pedantic',
                                 '-O2', '-I', str(HERE), str(HERE/'serial_fixture.c'),
                                 str(HERE/'cyton_serial.c'), '-Wl,--wrap=write', '-o', str(cls.exe)],
                                text=True, capture_output=True)
        if result.returncode:
            raise AssertionError('Native serial implementation missing or failed compilation:\n' + result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_board(self, board, mode=0, duration=250, timeout=150, attempts=0, path=None):
        try:
            t = time.monotonic()
            result = subprocess.run([str(self.exe), path or board.path, str(duration),
                                     str(timeout), '20', str(attempts), str(mode)],
                                    capture_output=True, text=True, timeout=3)
            time.sleep(.025)
            return result, self.parse(result.stdout), time.monotonic()-t
        finally:
            board.close()

    @staticmethod
    def parse(text):
        events = [line.split() for line in text.splitlines() if line.startswith('EVENT')]
        results = [line.split() for line in text.splitlines() if line.startswith('RESULT')]
        if len(results) != 1:
            raise AssertionError(text)
        return events, list(map(int, results[0][1:]))

    def test_official_split_replies_actual_gain_and_powerdown(self):
        board = Board(split=True)
        result, (events, stats), elapsed = self.run_board(board)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertEqual(bytes(board.commands), b'sv?bs')
        connected = events[0]
        self.assertEqual(connected[1:3], ['0', '1'])
        self.assertEqual(connected[connected.index('MASK')+1], '127')
        i = connected.index('GAINS')+1
        self.assertEqual(list(map(int, connected[i:i+8])), [1,2,4,6,8,12,24,24])
        self.assertEqual(bytes.fromhex(connected[-1]), BANNER[:-3])
        chunks = [e for e in events if e[1] == '1']
        self.assertEqual(b''.join(bytes.fromhex(e[-1]) for e in chunks), board.payload)
        self.assertTrue(all(int(e[2]) == 1 and int(e[3]) > 0 and int(e[4]) <= 4096 for e in chunks))
        self.assertEqual(stats[2:], [1, 0, len(board.payload), 0, 0])
        self.assertLess(elapsed, .7)

    def test_incomplete_malformed_duplicate_and_reserved_gain_refused(self):
        good = registers()
        mutations = {
            'missing': good.replace(register_line('CH8SET', 12, VALUES[7]), b''),
            'duplicate': good.replace(b'LIS3DH Registers', register_line('CH1SET', 5, VALUES[0])+b'LIS3DH Registers'),
            'wrong_address': good.replace(b'CH4SET, 08,', b'CH4SET, 09,'),
            'unknown_gain': registers(VALUES[:6]+[0x78, VALUES[7]]),
            'bad_hex': good.replace(b'CH3SET, 07, 28', b'CH3SET, 07, zz'),
            'trailing_junk': good.replace(register_line('CH2SET', 6, VALUES[1]), register_line('CH2SET', 6, VALUES[1]).rstrip()+b'junk\r\n'),
            'wrong_bit_readback': good.replace(b'CH1SET, 05, 08, 0,', b'CH1SET, 05, 08, 1,'),
            'short_line': good.replace(register_line('CH8SET', 12, VALUES[7]), b'CH8SET, 0C, E8\r\n'),
            'no_header': good.replace(b'Board ADS Registers', b'Not a register section'),
            'truncated': good[:-2],
            'oversized': b'X'*8200+b'$$$',
        }
        for name, config in mutations.items():
            with self.subTest(name=name):
                board = Board(config=config)
                result, (events, stats), elapsed = self.run_board(board)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse(events, result.stdout)
                self.assertNotIn(ord('b'), board.commands)
                self.assertTrue(board.commands.endswith(b's'))
                self.assertLess(elapsed, .7)

    def test_invalid_identity_never_streams(self):
        for banner in [b'ADS1299 Device ID: 0x3E\r\n$$$',
                       BANNER.replace(b'0x3E', b'0x00'),
                       BANNER.replace(b'\r\n', b'\n').replace(b'0x3E', b'0x00'),
                       BANNER.replace(b'ADS1299', b'ADS1298'),
                       b'X'*260+b'$$$', BANNER[:-1]]:
            with self.subTest(banner=banner):
                board = Board(banner=banner)
                result, (events, _), _ = self.run_board(board)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse(events)
                self.assertNotIn(ord('b'), board.commands)
                self.assertNotIn(ord('?'), board.commands)

    def test_whole_session_budget_bounds_silent_probe(self):
        board = Board(banner=b'')
        result, (_, stats), elapsed = self.run_board(board, duration=70, timeout=900)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(stats[1], errno.ETIMEDOUT)
        self.assertLess(elapsed, .4)
        self.assertEqual(bytes(board.commands), b'svs')

    def test_refuses_non_tty_without_blocking_fifo(self):
        for kind in ['regular', 'fifo', 'null']:
            with self.subTest(kind=kind):
                path = Path(self.tmp.name)/kind
                if kind == 'regular': path.write_bytes(BANNER)
                elif kind == 'fifo': os.mkfifo(path)
                else: path = Path('/dev/null')
                board = Board()
                result, (events, stats), elapsed = self.run_board(board, path=str(path))
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(stats[1], errno.ENOTTY)
                self.assertFalse(events)
                self.assertLess(elapsed, .4)

    def test_callback_stop_and_sigint_send_final_stop(self):
        for mode in [2, 3]:
            with self.subTest(mode=mode):
                board = Board()
                try:
                    proc = subprocess.Popen([str(self.exe), board.path, '5000', '200', '20', '0', str(mode)],
                                            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    self.assertTrue(board.started.wait(1))
                    if mode == 3: proc.send_signal(signal.SIGINT)
                    stdout, stderr = proc.communicate(timeout=1)
                    time.sleep(.025)
                    self.assertEqual(proc.returncode, 0, stdout+stderr)
                    self.assertEqual(bytes(board.commands), b'sv?bs')
                finally:
                    board.close()

    def test_callback_failure_preserves_errno_and_stops(self):
        board = Board()
        result, (events, stats), _ = self.run_board(board, mode=1)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(stats[1], errno.ENOSPC)
        self.assertEqual(bytes(board.commands), b'sv?bs')

    def test_reconnect_generation_and_changed_identity(self):
        for changed in [False, True]:
            with self.subTest(changed=changed):
                path = Path(self.tmp.name)/('stable-changed' if changed else 'stable')
                second = Board(banner=BANNER.replace(b'v3.1.2', b'v3.1.3') if changed else BANNER,
                               payload=b'\x02new-stream')
                def disconnect(board):
                    replacement = Path(str(path)+'.next')
                    replacement.symlink_to(second.path)
                    replacement.replace(path)
                    time.sleep(.03)
                    os.close(board.master)
                    board.done.set()
                first = Board(payload=b'\xa0\x01half', on_start=disconnect)
                path.symlink_to(first.path)
                try:
                    result, (events, stats), _ = self.run_board(first, path=str(path), attempts=3, duration=400)
                    connects = [e for e in events if e[1] == '0']
                    disconnects = [e for e in events if e[1] == '2']
                    self.assertEqual(len(disconnects), 1, result.stdout)
                    self.assertEqual([e[2] for e in connects], ['1'] if changed else ['1', '2'])
                    if changed:
                        self.assertEqual(stats[1], errno.ESTALE)
                        self.assertNotIn(ord('b'), second.commands)
                    else:
                        self.assertEqual(stats[2:6], [2, 1, len(first.payload)+len(second.payload), 1])
                        self.assertIn('2', [e[2] for e in events if e[1] == '1'])
                        self.assertEqual(bytes(second.commands), b'sv?bs')
                    # The old PTY cannot accept final stop after hangup. Failure
                    # is retained even when the second session fully recovers.
                    self.assertEqual(stats[-1], 1)
                    self.assertEqual(result.returncode, 1)
                finally:
                    second.close()

    def test_sigint_during_probe(self):
        board = Board(banner=b'')
        try:
            proc = subprocess.Popen([str(self.exe), board.path, '5000', '4000', '20', '0', '0'],
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            end = time.monotonic()+1
            while ord('v') not in board.commands and time.monotonic() < end:
                time.sleep(.002)
            self.assertIn(ord('v'), board.commands)
            proc.send_signal(signal.SIGINT)
            stdout, stderr = proc.communicate(timeout=.5)
            time.sleep(.025)
            self.assertEqual(proc.returncode, 0, stdout+stderr)
            self.assertEqual(bytes(board.commands), b'svs')
        finally:
            board.close()

    def test_identity_markers_must_be_complete_lines(self):
        board = Board(banner=BANNER.replace(b'On Board ADS1299', b'garbage On Board ADS1299'))
        result, (events, stats), _ = self.run_board(board)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(stats[1], errno.EPROTO)
        self.assertFalse(events)

    def test_registers_cannot_be_borrowed_from_accelerometer_section(self):
        config = registers().replace(register_line('CH8SET', 12, VALUES[7]), b'')
        config = config.replace(b'LIS3DH Registers\r\n', b'LIS3DH Registers\r\n'+register_line('CH8SET', 12, VALUES[7]))
        board = Board(config=config)
        result, (events, stats), _ = self.run_board(board)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(events)

    def test_short_start_and_stop_errors_retryable_writes(self):
        for mode in [4, 5, 6, 7, 8, 11]:
            with self.subTest(mode=mode):
                board = Board()
                result, (events, stats), _ = self.run_board(board, mode=mode)
                if mode in [5, 8]:
                    self.assertEqual(result.returncode, 0, result.stdout)
                    self.assertEqual(bytes(board.commands), b'sv?bs')
                else:
                    self.assertEqual(result.returncode, 1, result.stdout)
                    expected = errno.ENOSPC if mode == 6 else (errno.EAGAIN if mode == 11 else errno.EIO)
                    self.assertEqual(stats[1], expected)
                    self.assertEqual(stats[-1], int(mode in [4, 6, 11]))
                    self.assertEqual(bytes(board.commands), b'sv?s' if mode == 7 else b'sv?b')

    def test_raw_binary_chunks_monotonic_and_termios_115200_8n1(self):
        import termios
        payload = bytes(range(256))*40
        observed = []
        def inspect(board):
            observed.append(termios.tcgetattr(board.slave))
        board = Board(payload=payload, on_start=inspect)
        result, (events, stats), _ = self.run_board(board)
        self.assertEqual(result.returncode, 0, result.stdout)
        chunks = [e for e in events if e[1] == '1']
        self.assertEqual(b''.join(bytes.fromhex(e[-1]) for e in chunks), payload)
        self.assertTrue(all(0 < int(e[4]) <= 4096 for e in chunks))
        times = [int(e[3]) for e in chunks]
        self.assertTrue(all(b > a for a,b in zip(times, times[1:])))
        attrs = observed[0]
        self.assertEqual(attrs[4:6], [termios.B115200]*2)
        self.assertEqual(attrs[2] & termios.CSIZE, termios.CS8)
        self.assertFalse(attrs[2] & (termios.PARENB | termios.CSTOPB))
        self.assertFalse(attrs[3] & (termios.ICANON | termios.ECHO | termios.ISIG))

    def test_reconnect_attempt_limit_and_sigint_during_backoff(self):
        for interrupt in [False, True]:
            path = Path(self.tmp.name)/('gone-int' if interrupt else 'gone')
            def disconnect(board):
                path.unlink()
                time.sleep(.015)
                os.close(board.master)
                board.done.set()
            board = Board(on_start=disconnect)
            path.symlink_to(board.path)
            try:
                start = time.monotonic()
                proc = subprocess.Popen([str(self.exe), str(path), '5000', '300',
                                         '4000' if interrupt else '20', '2', '0'],
                                        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertTrue(board.started.wait(1))
                if interrupt:
                    time.sleep(.04)
                    proc.send_signal(signal.SIGINT)
                stdout, stderr = proc.communicate(timeout=.6)
                events, stats = self.parse(stdout)
                self.assertEqual(proc.returncode, 1, stdout+stderr)
                self.assertEqual(stats[2:4], [1,0])
                self.assertEqual(stats[-2:], [1,1])
                self.assertEqual(len([e for e in events if e[1] == '2']), 1)
                self.assertLess(time.monotonic()-start, .6)
            finally:
                board.close()

    def test_connected_callback_failure_does_not_start(self):
        board = Board()
        result, (events, stats), _ = self.run_board(board, mode=10)
        self.assertEqual(stats[1], errno.EACCES)
        self.assertEqual(bytes(board.commands), b'sv?s')

    def test_disconnect_original_error_survives_callback_and_stop_errors(self):
        def disconnect(board):
            time.sleep(.015)
            os.close(board.master)
            board.done.set()
        board = Board(on_start=disconnect)
        result, (events, stats), _ = self.run_board(board, mode=9, attempts=3)
        self.assertEqual(stats[1], errno.EIO)
        self.assertEqual(stats[-1], 1)
        self.assertEqual(len([e for e in events if e[1] == '2']), 1)

if __name__ == '__main__':
    unittest.main()
