import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tools/xtos-ai-runtime"))
from xtos_ai_runtime.server import InferenceService, UnixInferenceServer  # noqa: E402
from ai_service_trust_fixtures import make_real_signed_service_fixture
from xtos_eeg_loop.loop import LoopError, fixture_features, run_fixture_loop  # noqa: E402

PIPELINE = ROOT / "tools/xtos-eeg-pipeline/xtos-eeg-pipeline"
FIXTURE = ROOT / "tools/xtos-eeg-pipeline/fixtures/valid.csv"
BAD_INTERVAL = ROOT / "tools/xtos-eeg-pipeline/fixtures/bad_interval.csv"
MANIFEST = ROOT / "tools/xtos-ai-runtime/model-manifest.example.json"


class EegLoopTests(unittest.TestCase):
    def test_feature_mapping_is_deterministic_and_bounded(self):
        frames = [{"samples": [3000.0, 4000.0]}, {"samples": [3000.0, 4000.0]}]
        self.assertEqual(fixture_features(frames), [3.0, 4.0, 3.0, 4.0])
        with self.assertRaisesRegex(LoopError, "feature"):
            fixture_features([{"samples": [1e12, 1e12]}])

    def test_valid_fixture_is_preprocessed_and_action_is_blocked(self):
        result = run_fixture_loop(FIXTURE, PIPELINE)
        self.assertEqual(result["status"], "preprocessing_only")
        self.assertEqual(result["safe_action"], "no_action")
        self.assertEqual(result["reason"], "inference_not_requested")
        self.assertEqual(result["source_kind"], "fixture")
        self.assertEqual(result["frames_in"], 4)
        self.assertEqual(result["frames_out"], 4)
        self.assertEqual(result["inference"]["status"], "not_called")
        self.assertEqual(result["feature_mapping"], "fixture-eeg-v1")
        self.assertEqual(result["features"][0], -result["features"][1])
        self.assertEqual(result["features"][2], result["features"][3])
        self.assertGreater(result["features"][2], 0)
        self.assertEqual(len(result["features"]), 4)
        self.assertTrue(all(isinstance(value, float) for value in result["features"]))

    def test_pipeline_frames_in_must_match_verified_input(self):
        with tempfile.TemporaryDirectory() as directory:
            pipeline = Path(directory) / "fake-pipeline"
            filtered_text = chr(10).join([
                "# xtos-filtered-v1,channels=2,sample_rate_hz=250,unit=nanovolt",
                "1000000000,1,-1", "1004000000,2,-2",
                "1008000000,3,-3", "1012000000,4,-4",
            ]) + chr(10)
            script = chr(10).join([
                "#!/usr/bin/env python3",
                "import json, pathlib, sys",
                "args = sys.argv",
                "output = pathlib.Path(args[args.index('--dump-filtered') + 1])",
                f"output.write_text({filtered_text!r})",
                "print(json.dumps({'status': 'ok', 'quality_rejected': 0, 'frames_in': 0, 'frames_out': 4}))",
            ]) + chr(10)
            pipeline.write_text(script, encoding="utf-8")
            pipeline.chmod(0o755)
            with self.assertRaisesRegex(LoopError, "frames_in"):
                run_fixture_loop(FIXTURE, pipeline)

    def test_missing_service_returns_safe_result(self):
        def missing_service(*args, **kwargs):
            raise ConnectionRefusedError("fixture service missing")
        result = run_fixture_loop(FIXTURE, PIPELINE, socket_path=Path("unused"),
                                  execute_inference=True, request_fn=missing_service)
        self.assertEqual(result["safe_action"], "no_action")
        self.assertEqual(result["reason"], "inference_unavailable")
        self.assertIsNone(result["inference"].get("output"))

    def test_frame_limit_prevents_unbounded_input(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "oversize.csv"
            source.write_text(
                "# xtos-eeg-v1,channels=2,sample_rate_hz=250,unit=nanovolt\n" +
                "".join(f"{1_000_000_000 + 4_000_000 * i},1,-1\n" for i in range(4097)))
            with self.assertRaisesRegex(LoopError, "limit"):
                run_fixture_loop(source, PIPELINE)

    def test_pipeline_rejects_non_integer_or_negative_summary_counters(self):
        with tempfile.TemporaryDirectory() as directory:
            pipeline = Path(directory) / "fake-pipeline"
            filtered_rows = (
                "1000000000,1,-1\n1004000000,2,-2\n"
                "1008000000,3,-3\n1012000000,4,-4\n"
            )
            counters = {"status": "ok", "frames_in": 4, "frames_out": 4, "quality_rejected": 0}
            cases = [
                ("frames_in", 4.0), ("frames_out", 4.0), ("quality_rejected", 0.0),
                ("frames_in", True), ("frames_out", False), ("quality_rejected", False),
                ("frames_in", -1), ("frames_out", -1), ("quality_rejected", -1),
            ]
            for name, value in cases:
                with self.subTest(counter=name, value=value):
                    summary = dict(counters, **{name: value})
                    pipeline.write_text(
                        "#!/usr/bin/env python3\n"
                        "import json, pathlib, sys\n"
                        "args = sys.argv\n"
                        "output = pathlib.Path(args[args.index('--dump-filtered') + 1])\n"
                        f"output.write_text({('# xtos-filtered-v1,channels=2,sample_rate_hz=250,unit=nanovolt\\n' + filtered_rows)!r})\n"
                        f"print({json.dumps(summary)!r})\n",
                        encoding="utf-8",
                    )
                    pipeline.chmod(0o755)
                    with self.assertRaisesRegex(LoopError, "counter"):
                        run_fixture_loop(FIXTURE, pipeline)

    def test_cli_rejects_non_object_summary_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            pipeline = Path(directory) / "fake-pipeline"
            environment = dict(os.environ, PYTHONPATH=str(ROOT / "tools"))
            for summary in (None, []):
                with self.subTest(summary=summary):
                    pipeline.write_text(
                        "#!/usr/bin/env python3\n"
                        f"print({json.dumps(summary)!r})\n",
                        encoding="utf-8",
                    )
                    pipeline.chmod(0o755)
                    result = subprocess.run(
                        [sys.executable, "-m", "xtos_eeg_loop.loop", "--input", str(FIXTURE),
                         "--pipeline", str(pipeline)],
                        capture_output=True, text=True, timeout=30, env=environment,
                    )
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stderr, "")
                    self.assertEqual(json.loads(result.stdout), {
                        "status": "rejected", "safe_action": "no_action",
                        "reason": "data_loop_failed", "diagnostics": "LoopError",
                    })

    def test_cli_reports_malformed_filtered_numeric_data_as_json(self):
        with tempfile.TemporaryDirectory() as directory:
            pipeline = Path(directory) / "fake-pipeline"
            filtered_text = chr(10).join([
                "# xtos-filtered-v1,channels=2,sample_rate_hz=250,unit=nanovolt",
                "1000000000,not-a-number,-1", "1004000000,2,-2",
                "1008000000,3,-3", "1012000000,4,-4",
            ]) + chr(10)
            script = chr(10).join([
                "#!/usr/bin/env python3",
                "import json, pathlib, sys",
                "args = sys.argv",
                "output = pathlib.Path(args[args.index('--dump-filtered') + 1])",
                f"output.write_text({filtered_text!r})",
                "print(json.dumps({'status': 'ok', 'quality_rejected': 0, 'frames_in': 4, 'frames_out': 4}))",
            ]) + chr(10)
            pipeline.write_text(script, encoding="utf-8")
            pipeline.chmod(0o755)
            environment = dict(os.environ, PYTHONPATH=str(ROOT / "tools"))
            result = subprocess.run(
                [sys.executable, "-m", "xtos_eeg_loop.loop", "--input", str(FIXTURE),
                 "--pipeline", str(pipeline)],
                capture_output=True, text=True, timeout=30, env=environment,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document, {
            "status": "rejected", "safe_action": "no_action",
            "reason": "data_loop_failed", "diagnostics": "ValueError",
        })

    def test_cli_emits_machine_readable_no_action_result(self):
        environment = dict(os.environ, PYTHONPATH=str(ROOT / "tools"))
        result = subprocess.run(
            [sys.executable, "-m", "xtos_eeg_loop.loop", "--input", str(FIXTURE),
             "--pipeline", str(PIPELINE)],
            capture_output=True, text=True, timeout=30, env=environment,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        document = json.loads(result.stdout)
        self.assertEqual(document["status"], "preprocessing_only")
        self.assertEqual(document["source_kind"], "fixture")
        self.assertEqual(document["safe_action"], "no_action")
        self.assertEqual(document["inference"]["status"], "not_called")

    def test_invalid_interval_is_rejected_before_pipeline(self):
        called = []
        def forbidden_pipeline(_input, _output):
            called.append(True)
            raise AssertionError("pipeline must not receive a rejected frame")
        with self.assertRaisesRegex(LoopError, "interval"):
            run_fixture_loop(BAD_INTERVAL, forbidden_pipeline)
        self.assertEqual(called, [])

    def test_unknown_header_is_rejected_before_downstream(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "wrong-unit.csv"
            source.write_text(
                "# xtos-eeg-v1,channels=2,sample_rate_hz=250,unit=microvolt\n"
                "1000000000,1,-1\n"
            )
            called = []
            with self.assertRaisesRegex(LoopError, "header"):
                run_fixture_loop(source, lambda *_: called.append(True))
            self.assertEqual(called, [])

    def test_default_inference_import_failure_is_safe(self):
        real_import = __import__

        def block_runtime_client(name, *args, **kwargs):
            if name == "xtos_ai_runtime.client":
                raise ModuleNotFoundError("blocked test import", name=name)
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=block_runtime_client):
            result = run_fixture_loop(
                FIXTURE, PIPELINE, socket_path=Path("/run/xtos-ai.sock"),
                execute_inference=True,
            )
        self.assertEqual(result["safe_action"], "no_action")
        self.assertEqual(result["reason"], "inference_unavailable")
        self.assertEqual(result["inference"]["status"], "unavailable")
        self.assertEqual(result["inference"]["diagnostics"], "ModuleNotFoundError")

    def test_inference_response_cannot_authorize_fixture_action(self):
        calls = []
        def fake_client(socket_path, request, timeout=2.0):
            calls.append((socket_path, request, timeout))
            return {
                "status": "success", "safe_action": "action_allowed",
                "reason": None, "output": [[0.9, 0.05, 0.05]],
                "authorization": "action_allowed",
                "metadata": {"safe_action": "action_allowed"},
            }
        result = run_fixture_loop(
            FIXTURE, PIPELINE, socket_path=Path("/run/xtos-ai.sock"),
            execute_inference=True, request_fn=fake_client,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["op"], "infer")
        self.assertEqual(calls[0][1]["input"], [result["features"]])
        self.assertEqual(result["safe_action"], "no_action")
        self.assertEqual(result["reason"], "fixture_action_blocked")
        self.assertEqual(result["inference"], {
            "status": "success", "reason": None,
            "output": [[0.9, 0.05, 0.05]], "safe_action": "no_action",
        })

    @unittest.skipUnless(os.environ.get("XTOS_TEST_AI_PYTHON"), "locked AI runtime not selected")
    def test_real_uds_inference_is_still_blocked_for_fixture(self):
        manifest = json.loads(MANIFEST.read_text())
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "eeg.sock"
            fx, envelope = make_real_signed_service_fixture(Path(directory))
            manifest = json.loads(envelope)
            service = InferenceService(fx.model_root, python_command=[os.environ["XTOS_TEST_AI_PYTHON"]],
                                       trust_policy=fx.policy)
            server = UnixInferenceServer(socket_path, service)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                loaded = service.dispatch({"id": "eeg-load", "op": "load", "manifest": manifest})
                self.assertEqual(loaded["status"], "ok", loaded)
                result = run_fixture_loop(
                    FIXTURE, PIPELINE, socket_path=socket_path,
                    execute_inference=True,
                )
                self.assertEqual(result["inference"]["status"], "success", result)
                self.assertEqual(result["safe_action"], "no_action")
                self.assertEqual(result["reason"], "fixture_action_blocked")
            finally:
                server.shutdown()
                server.server_close()
                service.close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
