"""Bounded calibration arithmetic fixtures; no physical-source acceptance.

Run: python3 -B tests/test_bci_calibration.py -v
Only the owned module/harness is built, below .build/xtos-roadmap-v1/calibration/.
The oracle uses exact Fraction raw sums (not C's centered long-double method).
"""
import ctypes as C
import errno
from fractions import Fraction
import hashlib
import json
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
BUILD = ROOT / ".build/xtos-roadmap-v1/calibration"


class Identity(C.Structure):
    _fields_ = [("source_kind", C.c_int), ("reserved", C.c_uint32 * 3),
                ("session_sha256", C.c_char * 65),
                ("firmware_sha256", C.c_char * 65),
                ("reference_id", C.c_char * 128)]


class Point(C.Structure):
    _fields_ = [("observed_nv", C.c_int32 * 8), ("reference_nv", C.c_int32 * 8)]


class Options(C.Structure):
    _fields_ = [("selected_mask", C.c_uint32), ("min_span_nv", C.c_uint32),
                ("max_residual_nv", C.c_uint32), ("identity", Identity)]


class Coefficients(C.Structure):
    _fields_ = [("version", C.c_uint32), ("valid", C.c_uint32),
                ("selected_mask", C.c_uint32), ("point_count", C.c_uint32),
                ("min_span_nv", C.c_uint32), ("residual_limit_nv", C.c_uint32),
                ("reserved", C.c_uint32 * 4), ("identity", Identity),
                ("scale", C.c_double * 8), ("offset_nv", C.c_double * 8),
                ("max_residual_nv", C.c_double * 8)]


class Frame(C.Structure):
    _fields_ = [("channel_nv", C.c_int32 * 8)]


def options(mask=255, span=1, residual=10000, source=1):
    identity = Identity(source, (C.c_uint32 * 3)(), b"a" * 64, b"b" * 64,
                        b"fixture:exact-rational-oracle-v1")
    return Options(mask, span, residual, identity)


def point_array(rows):
    return (Point * len(rows))(*(Point((C.c_int32 * 8)(*x),
                                      (C.c_int32 * 8)(*y)) for x, y in rows))


class CalibrationTests(unittest.TestCase):
    oracle_fits = 0
    oracle_channels = 0
    oracle_apply_frames = 0
    oracle_apply_channels = 0

    @classmethod
    def setUpClass(cls):
        BUILD.mkdir(parents=True, exist_ok=True)
        cls.work = Path(tempfile.mkdtemp(prefix="focused-", dir=BUILD))
        cls.cc = shlex.split(os.environ.get("CC", "cc"))
        cls.flags = ["-std=c11", "-Wall", "-Wextra", "-Werror", "-pedantic"]
        cls.command(cls.cc + cls.flags + ["-O2", "-fPIC", "-shared",
                    str(SOURCE / "bci_calibration.c"), "-lm", "-o",
                    str(cls.work / "calibration.so")], "shared-build")
        cls.lib = C.CDLL(str(cls.work / "calibration.so"))
        cls.fit = cls.lib.bci_calibration_fit
        cls.fit.argtypes = [C.POINTER(Point), C.c_size_t, C.POINTER(Options),
                           C.POINTER(Coefficients)]
        cls.fit.restype = C.c_int
        cls.apply = cls.lib.bci_calibration_apply
        cls.apply.argtypes = [C.POINTER(Coefficients), C.POINTER(Identity),
                             C.POINTER(Frame), C.POINTER(Frame)]
        cls.apply.restype = C.c_int

    @classmethod
    def tearDownClass(cls):
        source_paths = [SOURCE / "bci_calibration.h", SOURCE / "bci_calibration.c",
                        SOURCE / "test_bci_calibration.c", Path(__file__).resolve()]
        summary = {"oracle_fits": cls.oracle_fits,
                   "oracle_channels": cls.oracle_channels,
                   "oracle_apply_frames": cls.oracle_apply_frames,
                   "oracle_apply_channels": cls.oracle_apply_channels,
                   "source_sha256": {str(p.relative_to(ROOT)):
                       hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}}
        (cls.work / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        print("calibration evidence:", cls.work)
        print("oracle:", cls.oracle_fits, "fits,", cls.oracle_channels, "channels")
        print("apply oracle:", cls.oracle_apply_frames, "frames,",
              cls.oracle_apply_channels, "channels")

    @classmethod
    def command(cls, cmd, label, env=None):
        result = subprocess.run(cmd, text=True, capture_output=True,
                                timeout=60, env=env)
        (cls.work / (label + ".json")).write_text(json.dumps({
            "command": cmd, "exit": result.returncode,
            "stdout": result.stdout, "stderr": result.stderr}, indent=2) + "\n")
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        return result.stdout

    def check_oracle(self, rows, mask=255, span=1, residual=10000, source=1):
        pts = point_array(rows)
        opts = options(mask, span, residual, source)
        before_pts, before_opts = bytes(pts), bytes(opts)
        out = Coefficients()
        self.assertEqual(self.fit(pts, len(rows), C.byref(opts), C.byref(out)), 0)
        self.assertEqual(bytes(pts), before_pts)
        self.assertEqual(bytes(opts), before_opts)
        self.assertEqual(bytes(out.identity), bytes(opts.identity))
        self.assertEqual((out.version, out.valid, out.selected_mask, out.point_count),
                         (1, 1, mask, len(rows)))
        self.assertEqual(list(out.reserved), [0] * 4)
        for c in range(8):
            if not (mask & (1 << c)):
                self.assertEqual((out.scale[c], out.offset_nv[c],
                                  out.max_residual_nv[c]), (1, 0, 0))
                continue
            x = [row[0][c] for row in rows]
            y = [row[1][c] for row in rows]
            n, sx, sy = len(rows), sum(x), sum(y)
            slope = Fraction(n * sum(a * b for a, b in zip(x, y)) - sx * sy,
                             n * sum(a * a for a in x) - sx * sx)
            offset = (Fraction(sy) - slope * sx) / n
            self.assertTrue(math.isclose(out.scale[c], float(slope),
                                        rel_tol=2e-15, abs_tol=1e-15))
            self.assertTrue(math.isclose(out.offset_nv[c], float(offset),
                                        rel_tol=2e-15, abs_tol=3e-6))
            stored_slope = Fraction.from_float(out.scale[c])
            stored_offset = Fraction.from_float(out.offset_nv[c])
            maximum = max(abs(stored_slope * a + stored_offset - b)
                          for a, b in zip(x, y))
            self.assertTrue(math.isclose(out.max_residual_nv[c], float(maximum),
                                        rel_tol=2e-12, abs_tol=5e-9),
                            (c, out.max_residual_nv[c], float(maximum)))
            type(self).oracle_channels += 1
        type(self).oracle_fits += 1
        return out

    def test_exact_rational_numeric_oracle(self):
        rng = random.Random(0xBC113B)
        for n in (3, 4, 7, 32, 64):
            rows = []
            for i in range(n):
                x = [(-500000 + i * 14000) + c * 30000000 for c in range(8)]
                y = [v * (1, 2, 4, 1, 2, 4, 2, 4)[c] // 2 + c * 12345
                     + rng.randint(-9, 9) for c, v in enumerate(x)]
                rows.append((x, y))
            for mask in (1, 0x80, 0x55, 0xAA, 0xFF):
                with self.subTest(n=n, mask=mask):
                    self.check_oracle(rows, mask=mask)
        for start in (-(2**31), 2**31 - 3):
            rows = [([start + i] * 8, [start + i] * 8) for i in range(3)]
            self.check_oracle(rows, residual=0)
        rows = [([x] * 8, [x] * 8) for x in (-(2**31), 0, 2**31 - 1)]
        self.check_oracle(rows, span=2**32 - 1, residual=0)

    def test_residual_threshold_is_inclusive_and_transactional(self):
        rows = [([x] * 8, [y] * 8) for x, y in ((-100, -100), (0, 3), (100, 100))]
        pts, opts, out = point_array(rows), options(residual=1), Coefficients()
        C.memset(C.byref(out), 0xA5, C.sizeof(out))
        before = bytes(out)
        self.assertEqual(self.fit(pts, 3, C.byref(opts), C.byref(out)), -errno.ERANGE)
        self.assertEqual(bytes(out), before)
        good = self.check_oracle(rows, residual=2)
        self.assertEqual(list(good.scale), [1] * 8)
        self.assertEqual(list(good.offset_nv), [1] * 8)
        self.assertEqual(list(good.max_residual_nv), [2] * 8)

    def c_harness(self, label, flags):
        binary = self.work / label
        self.command(self.cc + self.flags + flags +
                     [str(SOURCE / "bci_calibration.c"),
                      str(SOURCE / "test_bci_calibration.c"), "-lm", "-o",
                      str(binary)], label + "-build")
        env = dict(os.environ, ASAN_OPTIONS="detect_leaks=1:halt_on_error=1",
                   UBSAN_OPTIONS="halt_on_error=1:print_stacktrace=1")
        output = self.command([str(binary)], label + "-run", env=env)
        self.assertRegex(output, r"bci_calibration: [1-9][0-9]* cases, [1-9][0-9]* checks passed")
        print(label + ":", output.strip())

    def test_strict_c_harness(self):
        self.c_harness("strict", ["-O2"])

    def test_asan_ubsan_c_harness(self):
        self.c_harness("asan-ubsan", ["-O1", "-g", "-fno-omit-frame-pointer",
                       "-fno-pie", "-no-pie", "-fsanitize=address,undefined",
                       "-fno-sanitize-recover=all"])

    def test_exact_fraction_apply_oracle(self):
        rng = random.Random(0xA9913B)
        for mask in (1, 0x80, 0x55, 0xAA, 0xFF):
            rows = []
            for i in range(7):
                x = [(-60000 + i * 20000) + c * 5000000 for c in range(8)]
                y = [v * (1, 2, 4, 1, 2, 4, 2, 4)[c] // 2 + c * 12345
                     + rng.randint(-7, 7) for c, v in enumerate(x)]
                rows.append((x, y))
            coeff = self.check_oracle(rows, mask=mask)
            before_coeff = bytes(coeff)
            id_before = bytes(coeff.identity)
            for _ in range(100):
                raw = Frame((C.c_int32 * 8)(*(rng.randint(-100000000, 100000000)
                                            for _ in range(8))))
                out = Frame()
                before_raw = bytes(raw)
                self.assertEqual(self.apply(C.byref(coeff), C.byref(coeff.identity),
                                            C.byref(raw), C.byref(out)), 0)
                for c in range(8):
                    expected = raw.channel_nv[c]
                    if mask & (1 << c):
                        value = (Fraction.from_float(coeff.scale[c]) * expected
                                 + Fraction.from_float(coeff.offset_nv[c]))
                        magnitude = abs(value)
                        rounded = ((2 * magnitude.numerator + magnitude.denominator)
                                   // (2 * magnitude.denominator))
                        expected = -rounded if value < 0 else rounded
                    self.assertEqual(out.channel_nv[c], expected)
                    type(self).oracle_apply_channels += 1
                self.assertEqual(bytes(raw), before_raw)
                self.assertEqual(bytes(coeff), before_coeff)
                self.assertEqual(bytes(coeff.identity), id_before)
                type(self).oracle_apply_frames += 1

    def test_bounded_scattered_level_oracle(self):
        rng = random.Random(0x64BC1)
        for n in (3, 5, 31, 63, 64):
            for repetition in range(8):
                rows = []
                for _ in range(n):
                    x = [rng.randint(-200000000, 200000000) for _ in range(8)]
                    y = [v * (1, 2, 4, 1, 2, 4, 2, 4)[c] // 2 + c * 1234567
                         + rng.randint(-100, 100) for c, v in enumerate(x)]
                    rows.append((x, y))
                with self.subTest(n=n, repetition=repetition):
                    self.check_oracle(rows, residual=1000,
                                      source=1 + repetition % 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
