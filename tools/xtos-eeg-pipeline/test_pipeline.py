import json
import math
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent
TOOL = ROOT / "xtos-eeg-pipeline"
FIXTURES = ROOT / "fixtures"


class PipelineTests(unittest.TestCase):
    def run_pipeline(self, name):
        return subprocess.run(
            [str(TOOL), "--input", str(FIXTURES / name)],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_valid_data_round_trip(self):
        result = self.run_pipeline("valid.csv")
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["frames_in"], 4)
        self.assertEqual(summary["frames_out"], 4)
        self.assertEqual(summary["interval_anomalies"], 0)
        self.assertEqual(summary["timestamp_anomalies"], 0)
        self.assertEqual(summary["rejected_full"], 0)

    def test_interval_anomaly_is_degraded(self):
        result = self.run_pipeline("bad_interval.csv")
        self.assertEqual(result.returncode, 3)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["status"], "degraded")
        self.assertEqual(summary["frames_in"], 4)
        self.assertEqual(summary["frames_out"], 4)
        self.assertEqual(summary["interval_anomalies"], 1)

    def test_timestamp_order_is_rejected(self):
        result = self.run_pipeline("bad_order.csv")
        self.assertEqual(result.returncode, 3)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["status"], "degraded")
        self.assertEqual(summary["timestamp_anomalies"], 1)
        self.assertIn("not strictly increasing", result.stderr)

    def test_bandpass_matches_python_reference(self):
        sample_rate = 250.0
        low_cut = 5.0
        high_cut = 40.0
        rows = [(1000000000, 10.0, -10.0),
                (1004000000, 11.0, -11.0),
                (1008000000, 12.0, -12.0),
                (1012000000, 13.0, -13.0)]
        with tempfile.NamedTemporaryFile(suffix=".csv") as output:
            result = subprocess.run(
                [str(TOOL), "--input", str(FIXTURES / "valid.csv"),
                 "--bandpass", str(low_cut), str(high_cut),
                 "--dump-filtered", output.name],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = Path(output.name).read_text().splitlines()
        self.assertEqual(lines[0], "# xtos-filtered-v1,channels=2,sample_rate_hz=250,unit=nanovolt")
        actual = [tuple(float(value) for value in line.split(",")) for line in lines[1:]]
        self.assertEqual(len(actual), 4)

        def biquad(cutoff_hz, highpass):
            quality = 0.7071067811865475
            omega = 2.0 * math.pi * cutoff_hz / sample_rate
            sine = math.sin(omega)
            cosine = math.cos(omega)
            alpha = sine / (2.0 * quality)
            normalization = 1.0 + alpha
            if highpass:
                b0 = (1.0 + cosine) / (2.0 * normalization)
                b1 = -(1.0 + cosine) / normalization
                b2 = (1.0 + cosine) / (2.0 * normalization)
            else:
                b0 = (1.0 - cosine) / (2.0 * normalization)
                b1 = (1.0 - cosine) / normalization
                b2 = (1.0 - cosine) / (2.0 * normalization)
            return [b0, b1, b2, -2.0 * cosine / normalization,
                    (1.0 - alpha) / normalization, 0.0, 0.0, 0.0, 0.0]

        references = []
        states = [[biquad(low_cut, True), biquad(high_cut, False)] for _ in range(2)]
        for timestamp, first, second in rows:
            filtered = []
            for channel, value in enumerate((first, second)):
                high = states[channel][0]
                low = states[channel][1]
                high_value = high[0] * value + high[1] * high[5] + high[2] * high[6] - high[3] * high[7] - high[4] * high[8]
                high[6], high[5], high[8], high[7] = high[5], value, high[7], high_value
                low_value = low[0] * high_value + low[1] * low[5] + low[2] * low[6] - low[3] * low[7] - low[4] * low[8]
                low[6], low[5], low[8], low[7] = low[5], high_value, low[7], low_value
                filtered.append(low_value)
            references.append((timestamp, filtered))
        for actual_row, (timestamp, expected) in zip(actual, references):
            self.assertEqual(int(actual_row[0]), timestamp)
            self.assertAlmostEqual(actual_row[1], expected[0], places=12)
            self.assertAlmostEqual(actual_row[2], expected[1], places=12)

    def test_bandpass_requires_output_and_rejects_invalid_cutoff(self):
        missing_output = subprocess.run(
            [str(TOOL), "--input", str(FIXTURES / "valid.csv"), "--bandpass", "5", "40"],
            check=False, capture_output=True, text=True)
        self.assertEqual(missing_output.returncode, 2)
        invalid = subprocess.run(
            [str(TOOL), "--input", str(FIXTURES / "valid.csv"), "--bandpass", "40", "5",
             "--dump-filtered", "/tmp/xtos-invalid.csv"],
            check=False, capture_output=True, text=True)
        self.assertEqual(invalid.returncode, 2)


if __name__ == "__main__":
    unittest.main()
