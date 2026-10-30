#!/usr/bin/env python3
"""Cyton fuzz inputs must exercise decoding and sustained ring writes."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('cyton_corpus', ROOT / 'tools/xtos-fuzz/gen_corpus.py')
corpus = importlib.util.module_from_spec(spec)
spec.loader.exec_module(corpus)


def frames(blob):
    # Independent structural oracle for aligned seed streams, not the parser.
    if not blob or len(blob) % 33:
        return []
    packets = [blob[i:i + 33] for i in range(0, len(blob), 33)]
    return packets if all(p[0] == 0xA0 and p[32] & 0xF0 == 0xC0 for p in packets) else []


class CytonFuzzTests(unittest.TestCase):
    def test_seeds_cover_all_footers_and_signed24_endpoints(self):
        packets = [p for seed in corpus.PACKET_SEEDS for p in frames(seed)]
        self.assertEqual({p[32] for p in packets}, set(range(0xC0, 0xD0)))
        self.assertTrue(any(b'\x80\x00\x00' in p[2:26] for p in packets))
        self.assertTrue(any(b'\x7f\xff\xff' in p[2:26] for p in packets))

    def test_sustained_stream_crosses_ring_capacity_and_counter_wrap(self):
        streams = [frames(seed) for seed in corpus.PACKET_SEEDS]
        stream = max(streams, key=len)
        self.assertGreaterEqual(len(stream), 300)
        for previous, current in zip(stream, stream[1:]):
            self.assertEqual(current[1], (previous[1] + 1) % 256)
        self.assertTrue(any(a[1] == 255 and b[1] == 0 for a, b in zip(stream, stream[1:])))

    def test_generated_corpus_is_reproducible_and_preserves_seeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / 'a', Path(tmp) / 'b'
            self.assertEqual(corpus.generate(a, 8, 7), corpus.generate(b, 8, 7))
            files = lambda root: {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            self.assertEqual(files(a), files(b))
            for i, expected in enumerate(corpus.PACKET_SEEDS):
                self.assertEqual((a / 'packet' / f'seed-{i:03d}').read_bytes(), expected)


if __name__ == '__main__':
    unittest.main()
