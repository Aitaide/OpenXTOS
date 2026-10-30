"""Standalone synthetic numeric checks; no shared Cyton or transport dependencies.

Run: python3 -B tests/test_bci_diagnostic.py
Builds only the task-owned C module/harness beneath diagnostic-numeric/.
The oracle is a two-pass Python math.fsum population calculation, independent
of the C long-double Welford recurrence. These are arithmetic fixtures only.
"""
import ctypes
import math
import os
from pathlib import Path
import random
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tools/xtos-cyton"
BUILD = ROOT / ".build/xtos-roadmap-v1/diagnostic-numeric"


class Frame(ctypes.Structure):
    _fields_ = [("channel_nv", ctypes.c_int32 * 8)]


class Options(ctypes.Structure):
    _fields_ = [("mode", ctypes.c_int), ("method", ctypes.c_int),
                ("source_assumption", ctypes.c_int),
                ("selected_channel", ctypes.c_uint),
                ("nominal_current_na", ctypes.c_uint32),
                ("series_resistance_ohm", ctypes.c_uint32)]


class Channel(ctypes.Structure):
    _fields_ = [("count", ctypes.c_size_t), ("mean_nv", ctypes.c_double),
                ("stddev_nv", ctypes.c_double),
                ("observed_peak_nv", ctypes.c_double),
                ("nominal_rms_estimate_ohm", ctypes.c_double),
                ("unclamped_estimate_ohm", ctypes.c_double),
                ("estimate_present", ctypes.c_int)]


class Result(ctypes.Structure):
    _fields_ = [("assumptions", Options), ("channels", Channel * 8)]


class DiagnosticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        BUILD.mkdir(parents=True, exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(prefix="focused-", dir=BUILD)
        cls.work = Path(cls.temp.name)
        cls.cc = shlex.split(os.environ.get("CC", "cc"))
        cls.flags = ["-std=c11", "-Wall", "-Wextra", "-Werror", "-pedantic"]
        cls.run_command(cls.cc + cls.flags + ["-O2", "-fPIC", "-shared",
                        str(SOURCE / "bci_diagnostic.c"), "-lm", "-o",
                        str(cls.work / "diagnostic.so")], "shared-build")
        cls.lib = ctypes.CDLL(str(cls.work / "diagnostic.so"))
        cls.analyze = cls.lib.bci_diagnostic_analyze
        cls.analyze.argtypes = [ctypes.POINTER(Frame), ctypes.c_size_t,
                               ctypes.POINTER(Options), ctypes.POINTER(Result)]
        cls.analyze.restype = ctypes.c_int

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @classmethod
    def run_command(cls, command, label, env=None):
        result = subprocess.run(command, text=True, capture_output=True,
                                timeout=60, env=env)
        (cls.work / (label + ".log")).write_text(
            shlex.join(command) + "\nexit=" + str(result.returncode) + "\n"
            + result.stdout + result.stderr)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        return result.stdout

    def c_harness(self, label, flags):
        binary = self.work / label
        self.run_command(self.cc + self.flags + flags +
                         [str(SOURCE / "bci_diagnostic.c"),
                          str(SOURCE / "test_bci_diagnostic.c"), "-lm", "-o",
                          str(binary)], label + "-build")
        env = dict(os.environ, ASAN_OPTIONS="detect_leaks=1:halt_on_error=1",
                   UBSAN_OPTIONS="halt_on_error=1:print_stacktrace=1")
        output = self.run_command([str(binary)], label + "-run", env)
        self.assertRegex(output, r"bci_diagnostic: [1-9][0-9]* cases passed")
        print(output.strip())

    def test_strict_c_harness(self):
        self.c_harness("strict", ["-O2"])

    def test_asan_ubsan_harness(self):
        self.c_harness("asan-ubsan", ["-O1", "-g", "-fno-omit-frame-pointer",
                       "-fno-pie", "-no-pie", "-fsanitize=address,undefined",
                       "-fno-sanitize-recover=all"])

    def check_oracle(self, rows, selected=None):
        frames = (Frame * len(rows))(*(Frame((ctypes.c_int32 * 8)(*r)) for r in rows))
        options = (Options(1, 1, 0, 0, 0, 0) if selected is None else
                   Options(2, 2, 1, selected, 6, 2200))
        before_frames, before_options = bytes(frames), bytes(options)
        out = Result()
        self.assertEqual(self.analyze(frames, len(rows), ctypes.byref(options),
                                     ctypes.byref(out)), 0)
        self.assertEqual(bytes(frames), before_frames)
        self.assertEqual(bytes(options), before_options)
        self.assertEqual(bytes(out.assumptions), before_options)
        for c in range(8):
            values = [r[c] for r in rows]
            mean = math.fsum(values) / len(rows)
            stddev = math.sqrt(math.fsum((x - mean) ** 2 for x in values) / len(rows))
            peak = max(abs(x - mean) for x in values)
            got = out.channels[c]
            self.assertEqual(got.count, len(rows))
            for actual, expected in [(got.mean_nv, mean), (got.stddev_nv, stddev),
                                     (got.observed_peak_nv, peak)]:
                self.assertTrue(math.isclose(actual, expected, rel_tol=2e-12,
                                             abs_tol=2e-6), (c, actual, expected))
            self.assertEqual(got.estimate_present, int(c == selected))
            raw = math.sqrt(2) * stddev / 6 - 2200 if c == selected else 0
            self.assertTrue(math.isclose(got.unclamped_estimate_ohm, raw,
                                         rel_tol=2e-12, abs_tol=2e-6))
            self.assertTrue(math.isclose(got.nominal_rms_estimate_ohm, max(0, raw),
                                         rel_tol=2e-12, abs_tol=2e-6))

    def test_synthetic_eight_sample_cycle_each_channel(self):
        wave = [0, 7071, 10000, 7071, 0, -7071, -10000, -7071]
        rows = [[wave[i % 8] * (c + 1) + c * 1234567 for c in range(8)]
                for i in range(32)]
        for selected in range(8):
            with self.subTest(selected=selected):
                self.check_oracle(rows, selected)
        self.check_oracle(rows)

    def test_double_two_pass_oracle_bounded_random_windows(self):
        rng = random.Random(0xBC113)
        for n in [32, 33, 255, 1000, 4096]:
            rows = [[rng.randint(-(2**31), 2**31 - 1) for _ in range(8)]
                    for _ in range(n)]
            with self.subTest(n=n):
                self.check_oracle(rows, 3)
                self.check_oracle(rows)

    def test_square_dc_extremes_and_small_variance(self):
        fixtures = [
            [[c * 2000 for c in range(8)] for _ in range(32)],
            [[(2**31 - 1 if i % 2 else -(2**31)) for _ in range(8)]
             for i in range(4096)],
            [[2**31 - 1 - (i % 2) for _ in range(8)] for i in range(32)],
            [[100000 + (30000 if i % 2 else -30000) for _ in range(8)]
             for i in range(32)],
        ]
        for index, rows in enumerate(fixtures):
            with self.subTest(fixture=index):
                self.check_oracle(rows, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
