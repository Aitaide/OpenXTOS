"""Shared Cyton ADS parser against official formatter-shaped byte fixtures."""
import errno
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
VALUES = [0x08, 0x18, 0x28, 0x38, 0x48, 0x58, 0x68, 0xE8]


def row(name, address, value):
    return (f'{name}, {address:02X}, {value:02X}, ' +
            ', '.join(str((value >> bit) & 1) for bit in range(7, -1, -1)) + '\r\n').encode()


def channels(values=VALUES):
    return b'Board ADS Registers\r\n' + b''.join(
        row(f'CH{i + 1}SET', i + 5, value) for i, value in enumerate(values))


class ConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='cyton-config-')
        cls.exe = Path(cls.tmp.name) / 'config-test'
        flags = ['-O2']
        if os.environ.get('CYTON_CONFIG_SANITIZE'):
            flags = ['-O1', '-g', '-fsanitize=address,undefined',
                     '-fno-sanitize-recover=all', '-fno-omit-frame-pointer', '-no-pie']
        result = subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-pedantic',
                                 *flags, str(ROOT / 'tests/cyton_config_host/config_test.c'),
                                 '-o', str(cls.exe)], capture_output=True, text=True)
        if result.returncode:
            cls.tmp.cleanup()
            raise AssertionError('Shared parser compilation failed:\n' + result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def parse(self, data, mode=None):
        result = subprocess.run([str(self.exe), *([mode] if mode else [])], input=data,
                                capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        fields = list(map(int, result.stdout.split()))
        self.assertEqual(len(fields), 37)
        self.assertEqual(fields[2], 1, 'input changed')
        return fields

    def reject(self, data, error=errno.EPROTO, mode=None):
        result = self.parse(data, mode)
        self.assertEqual(result[0], -error)
        self.assertEqual(result[1], 1, 'failure changed sentinel output')

    def test_minimal_eight_channels_gain_powerdown_readbacks(self):
        result = self.parse(channels())
        self.assertEqual(result[0], 0)
        self.assertEqual(result[3:5], [127, 0x1FE0])
        self.assertEqual(result[5:13], [1, 2, 4, 6, 8, 12, 24, 24])
        self.assertEqual(result[13:37], [0] * 5 + VALUES + [0] * 11)

    def test_header_required(self):
        self.reject(channels().replace(b'Board ADS Registers\r\n', b''))

    def test_duplicate_board_header(self):
        self.reject(channels() + b'Board ADS Registers\n')

    def test_missing_channel(self):
        for ch in range(8):
            with self.subTest(channel=ch):
                self.reject(channels().replace(row(f'CH{ch + 1}SET', ch + 5, VALUES[ch]), b''))

    def test_duplicate_channel(self):
        for ch in range(8):
            with self.subTest(channel=ch):
                self.reject(channels() + row(f'CH{ch + 1}SET', ch + 5, VALUES[ch]))

    def test_channel_outside_board_before_header(self):
        self.reject(row('CH8SET', 12, VALUES[7]) +
                    channels().replace(row('CH8SET', 12, VALUES[7]), b''))

    def test_channel_outside_board_after_lis3dh(self):
        self.reject(channels().replace(row('CH8SET', 12, VALUES[7]), b'') +
                    b'LIS3DH Registers\n' + row('CH8SET', 12, VALUES[7]))

    def test_reserved_gain_rejected(self):
        for value in (0x78, 0xF8):
            with self.subTest(value=value):
                self.reject(channels(VALUES[:7] + [value]))

    def test_daisy_rejected(self):
        self.reject(channels() + b'Daisy ADS Registers\n', errno.EPROTONOSUPPORT)

    def test_short_fake_channel_rejected(self):
        for fake in (b'CH', b'CH9SET,', b'CH10SET,', b'CH0SET,', b'CH1BAD,'):
            with self.subTest(fake=fake):
                self.reject(channels() + fake)

    def test_null_input(self):
        self.reject(channels(), errno.EINVAL, 'null-input')

    def test_null_output(self):
        self.reject(channels(), errno.EINVAL, 'null-output')

    def test_empty(self):
        self.reject(b'', errno.EINVAL)

    def test_oversized(self):
        self.reject(channels() + b'X' * (8193 - len(channels())), errno.EMSGSIZE)

    def test_exact_cap_non_nul_input(self):
        self.assertEqual(self.parse(channels() + b'X' * (8192 - len(channels())))[0], 0)

    def test_embedded_nul(self):
        self.reject(channels() + b'\x00')

    def test_non_ascii_and_controls(self):
        for byte in (1, 11, 31, 127, 128, 255):
            with self.subTest(byte=byte):
                self.reject(channels() + bytes([byte]))

    def test_wrong_channel_address(self):
        for ch in range(8):
            with self.subTest(channel=ch):
                self.reject(channels().replace(row(f'CH{ch + 1}SET', ch + 5, VALUES[ch]),
                                               row(f'CH{ch + 1}SET', ch + 6, VALUES[ch])))

    def test_bad_hex(self):
        self.reject(channels().replace(b'CH8SET, 0C, E8', b'CH8SET, 0C, zz'))

    def test_bit_echo_mismatch(self):
        self.reject(channels().replace(b'CH8SET, 0C, E8, 1', b'CH8SET, 0C, E8, 0'))

    def test_truncated_row(self):
        self.reject(channels()[:-5])

    def test_trailing_row_junk(self):
        self.reject(channels().rstrip() + b'junk')

    def test_bounded_non_nul_last_line(self):
        self.assertEqual(self.parse(channels().rstrip())[0], 0)

    def test_whitespace_lowercase_lf_and_lis_noise(self):
        data = channels().replace(b', ', b',\t').replace(b'0C', b'0c').replace(b'E8', b'e8')
        data = data.replace(b'\r\n', b'\n') + b'LIS3DH Registers\nWHO_AM_I, 0F, 33\n'
        self.assertEqual(self.parse(data)[0], 0)

    def test_full_optional_readbacks(self):
        names = ['ADS_ID', 'CONFIG1', 'CONFIG2', 'CONFIG3', 'LOFF']
        names += [f'CH{i}SET' for i in range(1, 9)]
        names += ['BIAS_SENSP', 'BIAS_SENSN', 'LOFF_SENSP', 'LOFF_SENSN', 'LOFF_FLIP',
                  'LOFF_STATP', 'LOFF_STATN', 'GPIO', 'MISC1', 'MISC2', 'CONFIG4']
        values = [0x3E, 0x96, 0xC0, 0x60, 0] + VALUES + list(range(11))
        data = b'Board ADS Registers\n' + b''.join(row(n, i, v) for i, (n, v) in enumerate(zip(names, values)))
        result = self.parse(data)
        self.assertEqual(result[0], 0)
        self.assertEqual(result[4], 0xFFFFFF)
        self.assertEqual(result[13:], values)

    def test_optional_row_wrong_address(self):
        self.reject(channels() + row('CONFIG1', 2, 0x96))

    def test_optional_row_bad_byte(self):
        self.reject(channels() + row('CONFIG1', 1, 0x96).replace(b'96', b'zz'))

    def test_optional_row_bad_bits(self):
        self.reject(channels() + row('CONFIG1', 1, 0x96).replace(b'96, 1', b'96, 0'))

    def test_optional_row_duplicate(self):
        self.reject(channels() + row('CONFIG1', 1, 0x96) * 2)

    def test_optional_row_truncated(self):
        self.reject(channels() + b'CONFIG1, 01, 96')


class SerialConfigIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        here = ROOT / 'tools/xtos-cyton'
        spec = importlib.util.spec_from_file_location('config_emulator', here / 'test_cyton_serial.py')
        cls.emulator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.emulator)
        cls.tmp = tempfile.TemporaryDirectory(prefix='cyton-config-serial-')
        cls.exe = Path(cls.tmp.name) / 'serial'
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-pedantic',
                        str(here / 'serial_fixture.c'), str(here / 'cyton_serial.c'),
                        '-Wl,--wrap=write', '-o', str(cls.exe)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_serial_rejects_bad_optional_readback_with_errno(self):
        config = self.emulator.registers().replace(b'CONFIG1, 01,', b'CONFIG1, 02,')
        board = self.emulator.Board(config=config)
        try:
            result = subprocess.run([str(self.exe), board.path, '250', '150', '20', '0', '0'],
                                    capture_output=True, text=True, timeout=3)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn(f'RESULT -1 {errno.EPROTO} ', result.stdout)
            self.assertNotIn('EVENT', result.stdout)
            self.assertNotIn(ord('b'), board.commands)
        finally:
            board.close()


if __name__ == '__main__':
    unittest.main()
