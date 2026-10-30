"""Strict text-only Cyton banner and default-register protocol tests."""
from pathlib import Path
import sys
import unittest
from dataclasses import FrozenInstanceError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/xtos-devices"))
import cyton_protocol as cyton


ADS_NAMES = (
    "ADS_ID", "CONFIG1", "CONFIG2", "CONFIG3", "LOFF",
    "CH1SET", "CH2SET", "CH3SET", "CH4SET", "CH5SET", "CH6SET",
    "CH7SET", "CH8SET", "BIAS_SENSP", "BIAS_SENSN", "LOFF_SENSP",
    "LOFF_SENSN", "LOFF_FLIP", "LOFF_STATP", "LOFF_STATN", "GPIO",
    "MISC1", "MISC2", "CONFIG4",
)
DEFAULT_VALUES = (0x3E, 0x96, 0xC0, 0x60, 0x00) + (0x68,) * 8 + (0x00,) * 11


def banner(*, board_prefix=True, ending=b"\r\n"):
    ads = b"On Board " if board_prefix else b""
    return (
        b"OpenBCI V3 8-16 channel" + ending
        + ads + b"ADS1299 Device ID: 0x3e" + ending
        + b"LIS3DH Device ID: 0x33" + ending
        + b"Firmware: v3.1.2" + ending
    )


def ads_row(name, address, value, *, bits=None):
    if bits is None:
        bits = [(value >> bit) & 1 for bit in range(7, -1, -1)]
    bit_text = ", ".join(str(bit) for bit in bits)
    return f"{name}, {address:02x}, {value:02x}, {bit_text}\r\n".encode()


def registers(values=DEFAULT_VALUES, *, lis=False):
    body = b"\r\nBoard ADS Registers\r\n"
    body += b"".join(ads_row(name, address, value)
                       for address, (name, value) in enumerate(zip(ADS_NAMES, values)))
    if lis:
        body += b"\r\nLIS3DH Registers\r\n"
        body += b"0x07 00\r\n0x0f 33\r\n\r\n"
        body += b"0x1f 10\r\n0x33 20\r\n\r\n0x38 30\r\n0x3d 40\r\n"
    return body


class BannerTests(unittest.TestCase):
    def assert_reject(self, body):
        with self.assertRaises(cyton.CytonProtocolError):
            cyton.parse_banner(body)

    def test_official_banner_returns_fixed_identity(self):
        info = cyton.parse_banner(banner())
        self.assertEqual(info.firmware, "v3.1.2")
        self.assertEqual(info.board_id, 0x3E)
        self.assertEqual(info.accelerometer_id, 0x33)
        self.assertEqual(info.banner, banner().decode("ascii"))
        with self.assertRaises(FrozenInstanceError):
            info.firmware = "v3.1.3"

    def test_banner_accepts_no_board_prefix(self):
        info = cyton.parse_banner(banner(board_prefix=False))
        self.assertEqual((info.board_id, info.accelerometer_id), (0x3E, 0x33))

    def test_banner_rejects_lf_only_line_endings(self):
        with self.assertRaises(cyton.CytonProtocolError):
            cyton.parse_banner(banner(ending=b"\n"))

    def test_banner_accepts_unterminated_final_line(self):
        info = cyton.parse_banner(banner().rstrip(b"\r\n"))
        self.assertEqual(info.firmware, "v3.1.2")

    def test_banner_rejects_footer(self):
        self.assert_reject(banner() + b"$$$")

    def test_banner_rejects_wrong_order_missing_duplicate_and_unknown_lines(self):
        body = banner().splitlines(keepends=True)
        for mutated in (
            body[1:] + body[:1],
            body[:2] + body[3:],
            body + [body[1]],
            body[:3] + [b"Noise\r\n"] + body[3:],
        ):
            with self.subTest(mutated=mutated):
                self.assert_reject(b"".join(mutated))

    def test_banner_rejects_daisy_wrong_ids_and_version(self):
        for text in (
            banner().replace(b"On Board ADS1299", b"On Daisy ADS1299"),
            banner().replace(b"0x3e", b"0x3f"),
            banner().replace(b"0x33", b"0x32"),
            banner().replace(b"v3.1.2", b"v3.1.3"),
        ):
            with self.subTest(text=text):
                self.assert_reject(text)

    def test_banner_rejects_invalid_size_controls_and_trailing_garbage(self):
        for body in (
            b"",
            b"A" * 256,
            banner().replace(b"channel", b"channel\x00"),
            banner().replace(b"channel", b"channel\x01"),
            banner() + b"junk",
            banner().replace(b"channel\r\n", b"channel\r"),
        ):
            with self.subTest(body=body):
                self.assert_reject(body)


class RegisterTests(unittest.TestCase):
    def assert_reject(self, body):
        with self.assertRaises(cyton.CytonProtocolError):
            cyton.parse_registers(body)

    def test_official_default_board_snapshot(self):
        config = cyton.parse_registers(registers())
        self.assertEqual(config.registers, DEFAULT_VALUES)
        self.assertEqual(config.gains, (24,) * 8)
        self.assertEqual(config.enabled_mask, 0xFF)
        self.assertEqual(config.sample_rate_hz, 250)
        with self.assertRaises(FrozenInstanceError):
            config.enabled_mask = 0

    def test_optional_lis_section_uses_official_address_groups(self):
        config = cyton.parse_registers(registers(lis=True))
        self.assertEqual(config.registers, DEFAULT_VALUES)

    def test_registers_accept_unterminated_final_row(self):
        config = cyton.parse_registers(registers().rstrip(b"\r\n"))
        self.assertEqual(config.registers, DEFAULT_VALUES)

    def test_other_ads_fields_are_format_checked_not_authenticated(self):
        values = list(DEFAULT_VALUES)
        values[2] = 0x01
        values[3] = 0xA5
        values[13] = 0x55
        config = cyton.parse_registers(registers(tuple(values)))
        self.assertEqual(config.registers[2:4], (0x01, 0xA5))
        self.assertEqual(config.registers[13], 0x55)

    def test_registers_accept_tab_whitespace(self):
        body = registers().replace(b", ", b",\t")
        config = cyton.parse_registers(body)
        self.assertEqual(config.enabled_mask, 0xFF)

    def test_registers_accept_exact_size_limit(self):
        body = registers()
        body += b"\r\n" * ((8192 - len(body)) // 2)
        config = cyton.parse_registers(body)
        self.assertEqual(config.sample_rate_hz, 250)

    def test_registers_reject_missing_duplicate_unknown_or_wrong_order(self):
        good = registers()
        missing = good.replace(ads_row("CH8SET", 12, 0x68), b"")
        duplicate = good + ads_row("CONFIG4", 23, 0x00)
        unknown = good.replace(ads_row("CONFIG2", 2, 0xC0), b"UNKNOWN, 02, c0, 1, 1, 0, 0, 0, 0, 0, 0\r\n")
        wrong_order = good.replace(
            ads_row("CONFIG1", 1, 0x96) + ads_row("CONFIG2", 2, 0xC0),
            ads_row("CONFIG2", 2, 0xC0) + ads_row("CONFIG1", 1, 0x96),
        )
        for body in (missing, duplicate, unknown, wrong_order):
            with self.subTest(body=body):
                self.assert_reject(body)

    def test_registers_reject_bad_addresses_values_bits_and_rows(self):
        good = registers()
        mutations = (
            good.replace(b"CONFIG1, 01, 96", b"CONFIG1, 02, 96"),
            good.replace(b"CONFIG1, 01, 96", b"CONFIG1, 01, zz"),
            good.replace(b"CH1SET, 05, 68, 0", b"CH1SET, 05, 68, 1"),
            good.replace(ads_row("CH8SET", 12, 0x68), b"CH8SET, 0c, 68, 0, 1"),
            good + b"trailing",
        )
        for body in mutations:
            with self.subTest(body=body):
                self.assert_reject(body)

    def test_registers_reject_nondefault_sample_rate_and_channel_modes(self):
        cases = (
            (1, 0x95, "wrong sample rate"),
            (5, 0x58, "gain 12"),
            (6, 0x78, "reserved gain 7"),
            (7, 0xE8, "powerdown"),
        )
        for index, value, label in cases:
            values = list(DEFAULT_VALUES)
            values[index] = value
            with self.subTest(label=label):
                self.assert_reject(registers(tuple(values)))

    def test_registers_reject_wrong_ads_identity(self):
        values = list(DEFAULT_VALUES)
        values[0] = 0x00
        self.assert_reject(registers(tuple(values)))

    def test_registers_reject_daisy_and_missing_board_header(self):
        self.assert_reject(registers().replace(b"Board ADS Registers", b"Daisy ADS Registers"))
        self.assert_reject(registers().replace(b"\r\nBoard ADS Registers\r\n", b""))

    def test_lis_requires_rows_and_strict_increasing_allowed_addresses(self):
        prefix = registers() + b"\r\nLIS3DH Registers\r\n"
        for lis in (
            b"",
            b"0x10 00\r\n",
            b"0x07 00\r\n0x07 01\r\n",
            b"0x0f 00\r\n0x07 01\r\n",
            b"0X07 00\r\n",
            b"0x07 000\r\n",
        ):
            with self.subTest(lis=lis):
                self.assert_reject(prefix + lis)

    def test_registers_reject_size_controls_nul_and_footer(self):
        for body in (
            b"",
            registers() + b"$$$",
            registers().replace(b"Board", b"Boa\x00rd"),
            registers().replace(b"Board", b"Boa\x01rd"),
            registers() + b"X" * (8193 - len(registers())),
        ):
            with self.subTest(body=body):
                self.assert_reject(body)


if __name__ == "__main__":
    unittest.main()
