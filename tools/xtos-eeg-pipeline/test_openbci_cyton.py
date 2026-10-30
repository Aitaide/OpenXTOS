import unittest

from openbci_cyton import CytonPacketParser, nanovolts_from_raw24, signed24


def packet(counter, channels, footer=0xC0, auxiliary=b"\x01\x02\x03\x04\x05\x06"):
    payload = bytearray([0xA0, counter])
    for value in channels:
        raw = value & 0xFFFFFF
        payload.extend((raw >> 16 & 0xFF, raw >> 8 & 0xFF, raw & 0xFF))
    payload.extend(auxiliary)
    payload.append(footer)
    assert len(payload) == 33
    return bytes(payload)


class CytonPacketParserTests(unittest.TestCase):
    def test_decodes_eight_signed_channels_and_integer_nanovolts(self):
        raw = [-8388608, -1, 0, 1, 123456, -654321, 8388607, 42]
        parser = CytonPacketParser(monotonic_ns=iter([100, 200]).__next__)
        frames = parser.feed(packet(9, raw, footer=0xCF))
        self.assertEqual(len(frames), 1)
        frame = frames[0]
        self.assertEqual(frame.sequence, 9)
        self.assertEqual(frame.sample_number, 9)
        self.assertEqual(frame.host_receive_monotonic_ns, 100)
        self.assertEqual(frame.channels_nv,
                         tuple(nanovolts_from_raw24(value) for value in raw))
        self.assertEqual(frame.auxiliary, b"\x01\x02\x03\x04\x05\x06")
        self.assertEqual(frame.footer, 0xCF)

    def test_reassembles_packets_split_across_arbitrary_reads(self):
        parser = CytonPacketParser(monotonic_ns=lambda: 1234)
        encoded = packet(11, list(range(8)))
        frames = []
        for chunk in (encoded[:1], encoded[1:7], encoded[7:20], encoded[20:]):
            frames.extend(parser.feed(chunk))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].sample_number, 11)
        self.assertEqual(frames[0].channels_nv[7], nanovolts_from_raw24(7))

    def test_discards_noise_and_recovers_after_bad_footer(self):
        parser = CytonPacketParser(monotonic_ns=lambda: 500)
        damaged = bytearray(packet(1, [0] * 8))
        damaged[-1] = 0x00
        good = packet(2, [2] * 8)
        frames = parser.feed(b"noise" + bytes(damaged) + good)
        self.assertEqual([frame.sample_number for frame in frames], [2])
        self.assertGreater(parser.stats.discarded_bytes, 0)
        self.assertGreater(parser.stats.invalid_packets, 0)
        self.assertEqual(parser.stats.accepted_packets, 1)

    def test_extends_counter_wrap_and_accounts_missing_samples(self):
        parser = CytonPacketParser(monotonic_ns=iter(range(1, 10)).__next__)
        frames = parser.feed(b"".join(packet(n, [n] * 8) for n in (254, 255, 0, 2)))
        self.assertEqual([frame.sequence for frame in frames], [254, 255, 256, 258])
        self.assertEqual(parser.stats.counter_gaps, 1)
        self.assertEqual(parser.stats.missing_samples, 1)

    def test_rejects_duplicate_and_stale_counters_without_corrupting_sequence(self):
        parser = CytonPacketParser(monotonic_ns=iter(range(1, 10)).__next__)
        frames = parser.feed(b"".join(packet(n, [n] * 8) for n in (8, 8, 7, 9)))
        self.assertEqual([frame.sequence for frame in frames], [8, 9])
        self.assertEqual(parser.stats.duplicate_packets, 1)
        self.assertEqual(parser.stats.out_of_order_packets, 1)

    def test_sign_extension_and_scaling_boundaries_are_exact(self):
        self.assertEqual(signed24(b"\x80\x00\x00"), -8388608)
        self.assertEqual(signed24(b"\xff\xff\xff"), -1)
        self.assertEqual(signed24(b"\x7f\xff\xff"), 8388607)
        self.assertEqual(nanovolts_from_raw24(-8388608), -187500022)
        self.assertEqual(nanovolts_from_raw24(8388607), 187500000)
        self.assertEqual(nanovolts_from_raw24(1), 22)
        self.assertEqual(nanovolts_from_raw24(-1), -22)

    def test_rejects_non_monotonic_receive_clock(self):
        times = iter((100, 100))
        parser = CytonPacketParser(monotonic_ns=lambda: next(times))
        with self.assertRaisesRegex(ValueError, "receive clock"):
            parser.feed(packet(1, [0] * 8) + packet(2, [0] * 8))


if __name__ == "__main__":
    unittest.main()
