import ctypes
import math
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[3]
LIB_DIR = ROOT / "sdk" / "libxtos-signal"
LIB = LIB_DIR / "libxtos-signal.so.1.0"


class SSVEPFeature(ctypes.Structure):
    _fields_ = [
        ("signal_power", ctypes.c_double),
        ("noise_power", ctypes.c_double),
        ("snr", ctypes.c_double),
        ("snr_db", ctypes.c_double),
        ("target_bin", ctypes.c_size_t),
        ("harmonics_used", ctypes.c_size_t),
    ]


class SSVEPReferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run(["make", "clean", "all"], cwd=LIB_DIR, check=True)
        cls.library = ctypes.CDLL(str(LIB))
        cls.library.xtos_signal_ssvep_feature.argtypes = [
            ctypes.POINTER(ctypes.c_double),
            ctypes.c_size_t,
            ctypes.c_uint32,
            ctypes.c_double,
            ctypes.c_size_t,
            ctypes.c_size_t,
            ctypes.POINTER(SSVEPFeature),
        ]
        cls.library.xtos_signal_ssvep_feature.restype = ctypes.c_int

    @staticmethod
    def reference(values, sample_rate, target_frequency, harmonic_count, neighbor_bins):
        n = len(values)
        powers = []
        for k in range(n // 2 + 1):
            real = sum(
                value * math.cos(2.0 * math.pi * k * index / n)
                for index, value in enumerate(values)
            )
            imag = sum(
                -value * math.sin(2.0 * math.pi * k * index / n)
                for index, value in enumerate(values)
            )
            powers.append(real * real + imag * imag)
        target_bin = round(target_frequency * n / sample_rate)
        target_bins = {target_bin * harmonic for harmonic in range(1, harmonic_count + 1)}
        signal_power = sum(powers[bin_index] for bin_index in target_bins)
        noise_values = []
        for center in target_bins:
            for offset in range(1, neighbor_bins + 1):
                for candidate in (center - offset, center + offset):
                    if 0 < candidate < len(powers) and candidate not in target_bins:
                        noise_values.append(powers[candidate])
        noise_power = sum(noise_values) / len(noise_values)
        snr = signal_power / noise_power
        return target_bin, signal_power, noise_power, snr, 10.0 * math.log10(snr)

    def test_c_matches_python_ssvep_reference(self):
        sample_rate = 256
        n = 256
        values = [
            math.sin(2.0 * math.pi * 10.0 * index / sample_rate)
            + 0.5 * math.sin(2.0 * math.pi * 20.0 * index / sample_rate)
            + 0.25 * math.sin(2.0 * math.pi * 9.0 * index / sample_rate)
            for index in range(n)
        ]
        expected = self.reference(values, sample_rate, 10.0, 2, 1)
        input_array = (ctypes.c_double * n)(*values)
        actual = SSVEPFeature()
        result = self.library.xtos_signal_ssvep_feature(
            input_array, n, sample_rate, 10.0, 2, 1, ctypes.byref(actual)
        )
        self.assertEqual(result, 0)
        self.assertEqual(actual.target_bin, expected[0])
        self.assertAlmostEqual(actual.signal_power, expected[1], places=8)
        self.assertAlmostEqual(actual.noise_power, expected[2], places=8)
        self.assertAlmostEqual(actual.snr, expected[3], places=10)
        self.assertAlmostEqual(actual.snr_db, expected[4], places=10)


if __name__ == "__main__":
    unittest.main()
