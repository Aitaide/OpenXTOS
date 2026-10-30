"""Fixture-only Ganglion v2 stream integration tests."""
from pathlib import Path
import json
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "xtos-devices"))
from ganglion_protocol import GanglionV2Decoder  # noqa: E402


class GanglionStreamTests(unittest.TestCase):
    @staticmethod
    def raw_packet(counts):
        return (b"\x00" + b"".join((value & 0xFFFFFF).to_bytes(3, "big")
                                     for value in counts) + b"ABCDEFG")

    @staticmethod
    def zero_packet(width, ring):
        packet_id = ring if width == 18 else ring + 100
        if width == 18:
            tail = {1: 0x12, 2: 0xA5, 3: 0xFE}.get(ring % 10, ord("G"))
            return bytes([packet_id]) + b"\x00" * 18 + bytes([tail])
        return bytes([packet_id]) + b"\x00" * 19

    def test_official_encoder_vectors_match_decoder_for_all_safe_rows(self):
        vectors = json.loads((ROOT / "tests/fixtures/ganglion-v2/encoder-vectors.json").read_text())
        checks = json.loads((ROOT / "tests/fixtures/ganglion-v2/oracle-checks.json").read_text())
        for row in vectors:
            with self.subTest(label=row["label"]):
                decoder = GanglionV2Decoder()
                batch = decoder.feed_notification(self.raw_packet(row["seed_counts"]),
                                                  host_receive_monotonic_ns=100)
                if row["kind"] == "raw":
                    self.assertEqual(batch.samples[0].channels_counts, tuple(row["seed_counts"]))
                else:
                    for ring in range(1, row["ring_index"]):
                        decoder.feed_notification(self.zero_packet(row["width"], ring),
                                                  host_receive_monotonic_ns=100)
                    batch = decoder.feed_notification(bytes.fromhex(row["notification_hex"]),
                                                      host_receive_monotonic_ns=100)
                    expectation = next(item for item in checks if item["label"] == row["label"])
                    self.assertEqual([sample.channels_counts for sample in batch.samples],
                                     [tuple(values) for values in expectation["expected_reconstructed_counts_pair"]])

    def test_full_ring_host_sequence_matches_all_404_sample_expectations(self):
        rows = json.loads((ROOT / "tests/fixtures/ganglion-v2/continuous-vectors.json").read_text())
        for width in (18, 19):
            decoder = GanglionV2Decoder()
            with self.subTest(width=width):
                for row in rows:
                    if row["width"] != width:
                        continue
                    batch = decoder.feed_notification(bytes.fromhex(row["notification_hex"]),
                                                      host_receive_monotonic_ns=100)
                    if row["kind"] == "raw":
                        self.assertEqual(len(batch.samples), 1)
                    else:
                        self.assertEqual(len(batch.samples), 2)
                    self.assertEqual([sample.channels_counts for sample in batch.samples],
                                     [tuple(values) for values in row["expected_counts"]])
                self.assertEqual(decoder.epoch, 2)


if __name__ == "__main__":
    unittest.main()
