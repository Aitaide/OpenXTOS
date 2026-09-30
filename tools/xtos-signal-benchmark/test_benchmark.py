import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[2]
TOOL = ROOT / "tools" / "xtos-signal-benchmark" / "xtos-signal-benchmark"


class SignalBenchmarkTests(unittest.TestCase):
    def test_benchmark_emits_reproducible_schema(self):
        with tempfile.NamedTemporaryFile(suffix=".json") as output:
            result = subprocess.run(
                [str(TOOL), "--iterations", "20", "--output", output.name],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            document = json.loads(Path(output.name).read_text())
        self.assertEqual(document["benchmark"], "xtos-signal-baseline-v1")
        self.assertEqual(document["window_length"], 256)
        self.assertEqual(document["sample_rate_hz"], 256)
        self.assertEqual(document["channels"], 2)
        self.assertEqual(document["iterations"], 20)
        self.assertEqual(document["threads"], 1)
        self.assertIn("compiler", document)
        self.assertIn("compiler_flags", document)
        self.assertEqual(
            set(document["operations"]), {"fft_power", "bandpass", "ssvep_feature"}
        )
        for values in document["operations"].values():
            self.assertGreater(values["p50_ns"], 0)
            self.assertLessEqual(values["p50_ns"], values["p95_ns"])
            self.assertLessEqual(values["p95_ns"], values["p99_ns"])
            self.assertLessEqual(values["p99_ns"], values["max_ns"])
            self.assertGreater(values["total_wall_ns"], 0)
            self.assertGreaterEqual(values["user_cpu_us"], 0)
            self.assertGreaterEqual(values["system_cpu_us"], 0)
            self.assertGreater(values["peak_rss_kb"], 0)

    def test_benchmark_requires_output(self):
        result = subprocess.run([str(TOOL)], check=False, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--output", result.stderr)


if __name__ == "__main__":
    unittest.main()
