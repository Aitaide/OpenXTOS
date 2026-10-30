"""Ganglion v2.0.1 offline codec contract; no BLE or physical-device evidence."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "xtos-devices"))

from ganglion_protocol import (  # noqa: E402
    GanglionControl,
    GanglionProtocolError,
    GanglionV2Decoder,
)


RAW_ZERO = bytes.fromhex("0000000000000000000000000041424344454647")
RAW_EXTREME = bytes.fromhex("008000007fffff000000ffffff41424344454647")
COMPRESSED_18_ZERO = bytes.fromhex("02000000000000000000000000000000000000a5")
COMPRESSED_19_ZERO = bytes.fromhex("6600000000000000000000000000000000000000")
COMPRESSED_18_EVEN = bytes.fromhex("030000bffff00403ff01ffff40004ff0040400fe")
COMPRESSED_18_ODD = bytes.fromhex("0a00003ffff0000bfffdfffec0004ffbfc010047")
COMPRESSED_19_ODD = bytes.fromhex("6e00001ffffc00017fffdffff600013ff7f80100")


class GanglionProtocolTests(unittest.TestCase):
    @staticmethod
    def _zero_packet(packet_id):
        if packet_id <= 100:
            return bytes([packet_id]) + b"\x00" * 18 + bytes([0x12 if packet_id % 10 == 1 else 0xA5 if packet_id % 10 == 2 else 0xFE if packet_id % 10 == 3 else 0])
        return bytes([packet_id]) + b"\x00" * 19

    def _advance_to(self, decoder, packet_id):
        target_ring = packet_id if packet_id <= 100 else packet_id - 100
        for ring in range(1, target_ring):
            prior_id = ring if packet_id <= 100 else ring + 100
            decoder.feed_notification(self._zero_packet(prior_id), host_receive_monotonic_ns=100)

    def test_raw_seed_is_one_exact_sample_with_nominal_integer_nv(self):
        decoder = GanglionV2Decoder()
        batch = decoder.feed_notification(RAW_EXTREME, host_receive_monotonic_ns=100)
        self.assertEqual(len(batch.samples), 1)
        sample = batch.samples[0]
        self.assertEqual(sample.channels_counts, (-8388608, 8388607, 0, -1))
        self.assertEqual(sample.channels_nv, (-15686001, 15686000, 0, -1))
        self.assertEqual((sample.packet_id, sample.ring_index, sample.sample_index), (0, 0, 0))
        self.assertEqual(sample.sequence, 0)
        self.assertEqual(sample.quality, ("raw_seed", "exact_counts", "nominal_scale"))
        self.assertIs(sample.physical_device_evidence, False)
        self.assertEqual(sample.action, "no_action")

    def test_official_18_and_19_compressed_vectors_reconstruct_quantized_samples(self):
        cases = (
            (18, RAW_ZERO, COMPRESSED_18_EVEN,
             ((-2, 1, -256, 255), (1, -3, 767, -769)), 3),
            (18, RAW_ZERO, COMPRESSED_18_ODD,
             ((0, 1, -2, 3), (5, -3, 255, -253)), 10),
            (19, RAW_ZERO, COMPRESSED_19_ODD,
             ((0, 1, -2, 3), (5, -3, 255, -253)), 110),
        )
        for width, raw, compressed, expected, packet_id in cases:
            with self.subTest(width=width):
                decoder = GanglionV2Decoder()
                decoder.feed_notification(raw, host_receive_monotonic_ns=100)
                self._advance_to(decoder, packet_id)
                batch = decoder.feed_notification(compressed, host_receive_monotonic_ns=100)
                self.assertEqual([sample.channels_counts for sample in batch.samples], list(expected))
                self.assertEqual([sample.packet_id for sample in batch.samples], [packet_id, packet_id])
                self.assertEqual([sample.sample_index for sample in batch.samples], [0, 1])
                self.assertTrue(all("compressed" in sample.quality for sample in batch.samples))
                self.assertTrue(all("delta_quantized" in sample.quality for sample in batch.samples))

    def test_mode_switch_keeps_normalized_ring_order(self):
        decoder = GanglionV2Decoder()
        decoder.feed_notification(RAW_ZERO, host_receive_monotonic_ns=100)
        decoder.feed_notification(bytes([1]) + b"\x00" * 18 + b"\x12", host_receive_monotonic_ns=100)
        batch = decoder.feed_notification(COMPRESSED_19_ZERO, host_receive_monotonic_ns=100)
        self.assertEqual([sample.ring_index for sample in batch.samples], [2, 2])
        self.assertEqual([sample.packet_id for sample in batch.samples], [102, 102])

    def test_gap_invalidates_delta_state_until_raw_reseed(self):
        decoder = GanglionV2Decoder()
        decoder.feed_notification(RAW_ZERO, host_receive_monotonic_ns=100)
        with self.assertRaisesRegex(GanglionProtocolError, "ring|reseed"):
            decoder.feed_notification(COMPRESSED_18_ZERO, host_receive_monotonic_ns=100)
        with self.assertRaisesRegex(GanglionProtocolError, "reseed"):
            decoder.feed_notification(COMPRESSED_18_ZERO, host_receive_monotonic_ns=100)
        batch = decoder.feed_notification(RAW_ZERO, host_receive_monotonic_ns=101)
        self.assertEqual(batch.samples[0].epoch, 2)

    def test_strict_lengths_padding_and_timestamps_fail_closed(self):
        decoder = GanglionV2Decoder()
        for packet in (RAW_ZERO[:-1], RAW_ZERO[:-1] + b"X", RAW_ZERO[:13] + b"ABCDEFH"):
            with self.subTest(packet=packet.hex()), self.assertRaises(GanglionProtocolError):
                decoder.feed_notification(packet, host_receive_monotonic_ns=100)
        decoder.feed_notification(RAW_ZERO, host_receive_monotonic_ns=100)
        with self.assertRaisesRegex(GanglionProtocolError, "timestamp"):
            decoder.feed_notification(RAW_ZERO, host_receive_monotonic_ns=99)

    def test_controls_are_bounded_and_do_not_change_eeg_seed(self):
        decoder = GanglionV2Decoder()
        empty_impedance = decoder.feed_notification(bytes([205]) + b"Z", host_receive_monotonic_ns=100)
        self.assertEqual(empty_impedance.controls[0].text, "Z")
        impedance = decoder.feed_notification(bytes([201]) + b"123Z", host_receive_monotonic_ns=100)
        self.assertEqual(len(impedance.controls), 1)
        self.assertEqual(impedance.controls[0].text, "123Z")
        self.assertIsNone(impedance.controls[0].numeric_value)
        first = decoder.feed_notification(bytes([206]) + b"A" * 19, host_receive_monotonic_ns=101)
        self.assertEqual(first.controls, ())
        final = decoder.feed_notification(bytes([207]) + b"OK", host_receive_monotonic_ns=101)
        self.assertEqual(len(final.controls), 1)
        self.assertEqual(final.controls[0].text, "A" * 19 + "OK")
        self.assertTrue(final.controls[0].complete)
        seed = decoder.feed_notification(RAW_ZERO, host_receive_monotonic_ns=102)
        self.assertEqual(seed.samples[0].channels_counts, (0, 0, 0, 0))

    def test_firmware_verbose_framing_accepts_lf_and_empty_final(self):
        decoder = GanglionV2Decoder()
        empty = decoder.feed_notification(bytes([207]), host_receive_monotonic_ns=99)
        self.assertEqual(empty.controls[0].text, "")
        first = decoder.feed_notification(bytes([206]) + b"A" * 18 + b"\n", host_receive_monotonic_ns=100)
        self.assertEqual(first.controls, ())
        final = decoder.feed_notification(bytes([207]), host_receive_monotonic_ns=100)
        self.assertEqual(final.controls[0].text, "A" * 18 + "\n")

    def test_nonfinal_verbose_fragment_requires_full_firmware_buffer(self):
        decoder = GanglionV2Decoder()
        with self.assertRaisesRegex(GanglionProtocolError, "20 bytes"):
            decoder.feed_notification(bytes([206]) + b"partial", host_receive_monotonic_ns=100)

    def test_interleaved_verbose_control_invalidates_pending_text(self):
        decoder = GanglionV2Decoder()
        decoder.feed_notification(bytes([206]) + b"partial" + b" " * 12, host_receive_monotonic_ns=100)
        with self.assertRaisesRegex(GanglionProtocolError, "interleaved"):
            decoder.feed_notification(RAW_ZERO, host_receive_monotonic_ns=101)

    def test_unknown_and_non_bytes_input_do_not_publish(self):
        decoder = GanglionV2Decoder()
        for data in ("not bytes", b"", b"\xd0" + b"\x00" * 19):
            with self.subTest(data=repr(data)), self.assertRaises(GanglionProtocolError):
                decoder.feed_notification(data, host_receive_monotonic_ns=100)
        self.assertEqual(decoder.events, ())

    def test_reset_increments_epoch_and_restarts_sequence(self):
        decoder = GanglionV2Decoder()
        decoder.feed_notification(RAW_ZERO, host_receive_monotonic_ns=100)
        decoder.reset()
        batch = decoder.feed_notification(RAW_ZERO, host_receive_monotonic_ns=101)
        self.assertEqual(batch.samples[0].epoch, 2)
        self.assertEqual(batch.samples[0].sequence, 0)

    def test_reset_does_not_allow_host_monotonic_clock_regression(self):
        decoder = GanglionV2Decoder()
        decoder.feed_notification(RAW_ZERO, host_receive_monotonic_ns=100)
        decoder.reset()
        with self.assertRaisesRegex(GanglionProtocolError, "timestamp"):
            decoder.feed_notification(RAW_ZERO, host_receive_monotonic_ns=99)

    def test_signed24_extreme_compressed_alias_is_explicitly_qualified(self):
        decoder = GanglionV2Decoder()
        decoder.feed_notification(RAW_EXTREME, host_receive_monotonic_ns=100)
        batch = decoder.feed_notification(bytes.fromhex(
            "0100007fffe00003ffffffff8000100000000012"),
            host_receive_monotonic_ns=100)
        self.assertIn("reconstructed_outside_signed24", batch.samples[-1].quality)

    def test_nominal_scale_is_explicitly_not_calibration(self):
        decoder = GanglionV2Decoder()
        sample = decoder.feed_notification(RAW_EXTREME, host_receive_monotonic_ns=100).samples[0]
        self.assertEqual(decoder.scale_numerator_nv, 15686000)
        self.assertEqual(decoder.scale_denominator_counts, 8388607)
        self.assertIn("nominal_scale", sample.quality)
        self.assertNotIn("calibrated", sample.quality)


if __name__ == "__main__":
    unittest.main()
