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


class P300Feature(ctypes.Structure):
    _fields_ = [
        ("baseline_mean", ctypes.c_double),
        ("baseline_stddev", ctypes.c_double),
        ("peak_amplitude", ctypes.c_double),
        ("peak_latency_ms", ctypes.c_double),
        ("peak_z_score", ctypes.c_double),
        ("window_energy", ctypes.c_double),
        ("peak_index", ctypes.c_size_t),
        ("detected", ctypes.c_int),
    ]


class SignalReferenceTests(unittest.TestCase):
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
        cls.library.xtos_signal_p300_feature.argtypes = [
            ctypes.POINTER(ctypes.c_double),
            ctypes.c_size_t,
            ctypes.c_uint32,
            ctypes.c_size_t,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_double,
            ctypes.POINTER(P300Feature),
        ]
        cls.library.xtos_signal_p300_feature.restype = ctypes.c_int

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

    def test_c_matches_python_p300_reference(self):
        sample_rate = 256
        values = [0.0] * 256
        for index in range(32):
            values[index] = 1.0 if index % 2 else -1.0
        values[128] = 5.0
        baseline = values[:32]
        baseline_mean = sum(baseline) / len(baseline)
        baseline_stddev = math.sqrt(
            sum((value - baseline_mean) ** 2 for value in baseline) / len(baseline)
        )
        corrected = [value - baseline_mean for value in values]
        window = corrected[96:160]
        peak_amplitude = max(window)
        peak_index = 96 + window.index(peak_amplitude)
        expected = (
            baseline_mean,
            baseline_stddev,
            peak_amplitude,
            (peak_index - 32) * 1000.0 / sample_rate,
            peak_amplitude / baseline_stddev,
            sum(value * value for value in window),
            peak_index,
            1,
        )
        input_array = (ctypes.c_double * len(values))(*values)
        actual = P300Feature()
        result = self.library.xtos_signal_p300_feature(
            input_array, len(values), sample_rate, 32, 250, 500, 4.0,
            ctypes.byref(actual)
        )
        self.assertEqual(result, 0)
        self.assertAlmostEqual(actual.baseline_mean, expected[0], places=12)
        self.assertAlmostEqual(actual.baseline_stddev, expected[1], places=12)
        self.assertAlmostEqual(actual.peak_amplitude, expected[2], places=12)
        self.assertAlmostEqual(actual.peak_latency_ms, expected[3], places=12)
        self.assertAlmostEqual(actual.peak_z_score, expected[4], places=12)
        self.assertAlmostEqual(actual.window_energy, expected[5], places=12)
        self.assertEqual(actual.peak_index, expected[6])
        self.assertEqual(actual.detected, expected[7])

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
