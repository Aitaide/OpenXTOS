import hashlib
import json
import math
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "tools/xtos-ai-runtime"))

from xtos_ai_runtime.protocol import (  # noqa: E402
    ManifestError,
    RequestError,
    safe_result,
    success_result,
    validate_input,
    validate_manifest,
    validate_output,
)


class ManifestValidationTests(unittest.TestCase):
    def test_production_manifest_and_signed_claim_provider_contract_remain_cpu_only(self):
        runtime_source = (ROOT / 'tools/xtos-ai-runtime/xtos_ai_runtime/protocol.py').read_text()
        trust_source = (ROOT / 'tools/xtos-ai-runtime/xtos_ai_runtime/trust.py').read_text()
        example = json.loads((ROOT / 'tools/xtos-ai-runtime/model-manifest.example.json').read_text())
        self.assertEqual(example['provider'], 'CPUExecutionProvider')
        self.assertIn('if provider != "CPUExecutionProvider":', runtime_source)
        self.assertIn('if claims["provider"] != "CPUExecutionProvider":', trust_source)
        package = ROOT / 'tools/xtos-ai-runtime/xtos_ai_runtime'
        self.assertFalse((package / 'provider_experiment.py').exists())

    def make_manifest(self, root: Path, payload: bytes) -> dict:
        model = root / "models" / "fixed.onnx"
        model.parent.mkdir(exist_ok=True)
        model.write_bytes(payload)
        return {
            "name": "fixed-classifier",
            "version": "1.0.0",
            "model_path": str(model),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "provider": "CPUExecutionProvider",
            "threads": {"intra_op": 1, "inter_op": 1},
            "input": {"name": "input", "dtype": "float32", "shape": [1, 4]},
            "output": {"name": "probabilities", "dtype": "float32", "shape": [1, 3]},
        }

    def test_valid_manifest_is_normalized_and_hash_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = self.make_manifest(root, b"model")
            manifest = validate_manifest(raw, root)
            self.assertEqual(manifest.name, "fixed-classifier")
            self.assertEqual(manifest.version, "1.0.0")
            self.assertEqual(manifest.provider, "CPUExecutionProvider")
            self.assertEqual(manifest.model_path, (root / "models/fixed.onnx").resolve())
            self.assertEqual(manifest.sha256, hashlib.sha256(b"model").hexdigest())
            self.assertEqual((manifest.intra_op_threads, manifest.inter_op_threads), (1, 1))
            self.assertEqual((manifest.input_name, manifest.output_name), ("input", "probabilities"))
            self.assertEqual((manifest.input_dtype, manifest.output_dtype), ("float32", "float32"))
            self.assertEqual(manifest.input_shape, (1, 4))
            self.assertEqual(manifest.output_shape, (1, 3))
            self.assertEqual((manifest.input.name, manifest.input.dtype, manifest.input.shape),
                             ("input", "float32", (1, 4)))
            self.assertEqual((manifest.output.name, manifest.output.dtype, manifest.output.shape),
                             ("probabilities", "float32", (1, 3)))
            self.assertEqual((manifest.threads.intra_op, manifest.threads.inter_op), (1, 1))
            raw["input"]["shape"][1] = 99
            self.assertEqual(manifest.input_shape, (1, 4))
            with self.assertRaises(FrozenInstanceError):
                manifest.name = "mutated"

    def test_hash_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.make_manifest(root, b"model")
            manifest["sha256"] = "0" * 64
            with self.assertRaisesRegex(ManifestError, "sha256"):
                validate_manifest(manifest, root)

    def test_path_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.make_manifest(root, b"model")
            manifest["model_path"] = str(root.parent / "outside.onnx")
            with self.assertRaisesRegex(ManifestError, "root"):
                validate_manifest(manifest, root)


    def test_model_paths_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            raw = self.make_manifest(root, b"model")
            outside = Path(directory) / "outside.onnx"
            outside.write_bytes(b"model")
            (root / "models/escape.onnx").symlink_to(outside)
            (root / "models/loop.onnx").symlink_to("loop.onnx")
            for path in (str(outside), "../outside.onnx", "models/escape.onnx",
                         "missing.onnx", "models", "models/loop.onnx", "bad\x00path"):
                with self.subTest(path=path), self.assertRaises(ManifestError):
                    validate_manifest(dict(raw, model_path=path), root)
            relative = validate_manifest(dict(raw, model_path="models/fixed.onnx"), root)
            self.assertEqual(relative.model_path, (root / "models/fixed.onnx").resolve())
            with self.assertRaises(ManifestError):
                validate_manifest(raw, root / "missing-root")
    def test_manifest_schema_and_bounds_are_rejected(self):
        cases = [
            ("extra", lambda m: m.update(extra=True)),
            ("provider", lambda m: m.update(provider="CUDAExecutionProvider")),
            ("dtype", lambda m: m["input"].update(dtype="float64")),
            ("rank", lambda m: m["input"].update(shape=[1, 4, 1])),
            ("zero_dim", lambda m: m["input"].update(shape=[1, 0])),
            ("batch", lambda m: m["input"].update(shape=[65, 1])),
            ("elements", lambda m: m["input"].update(shape=[256, 257])),
            ("dimension", lambda m: m["input"].update(shape=[1, 65537])),
            ("bool_shape", lambda m: m["output"].update(shape=[True, 3])),
            ("bool_thread", lambda m: m["threads"].update(intra_op=True)),
            ("thread_low", lambda m: m["threads"].update(inter_op=0)),
            ("thread_high", lambda m: m["threads"].update(inter_op=65)),
            ("thread_extra", lambda m: m["threads"].update(extra=1)),
            ("input_extra", lambda m: m["input"].update(extra=1)),
            ("missing_field", lambda m: m.pop("version")),
            ("bad_name", lambda m: m.update(name=3)),
            ("empty_version", lambda m: m.update(version="")),
            ("bad_hash", lambda m: m.update(sha256="not-a-digest")),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for label, mutate in cases:
                with self.subTest(case=label):
                    manifest = self.make_manifest(root, b"model")
                    mutate(manifest)
                    with self.assertRaises(ManifestError):
                        validate_manifest(manifest, root)

    def test_example_manifest_hash_is_bound_to_the_real_fixture(self):
        example = ROOT / "tools/xtos-ai-runtime/model-manifest.example.json"
        manifest = json.loads(example.read_text(encoding="utf-8"))
        bound = validate_manifest(manifest, ROOT)
        fixture = ROOT / "tools/xtos-onnx-baseline/fixtures/fixed_classifier.onnx"
        self.assertEqual(bound.model_path, fixture.resolve())
        self.assertEqual(
            bound.sha256,
            "6e6226cf772934a3f7a398ecb55ffd290503497d91803ff499a7aa28bf90a113",
        )


class TensorValidationTests(unittest.TestCase):
    def make_bound_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "model.onnx"
            model.write_bytes(b"model")
            raw = {
                "name": "m", "version": "1", "model_path": str(model),
                "sha256": hashlib.sha256(b"model").hexdigest(),
                "provider": "CPUExecutionProvider",
                "threads": {"intra_op": 1, "inter_op": 1},
                "input": {"name": "x", "dtype": "float32", "shape": [2, 2]},
                "output": {"name": "y", "dtype": "float32", "shape": [2, 1]},
            }
            return validate_manifest(raw, root)

    def test_input_and_output_are_shape_checked_and_normalized(self):
        manifest = self.make_bound_manifest()
        self.assertEqual(validate_input(manifest, [[1, 2], [3, 4]]), [[1.0, 2.0], [3.0, 4.0]])
        self.assertEqual(validate_output(manifest, [[0], [1]]), [[0.0], [1.0]])
        for value in ([[1, 2]], [[1, 2, 3], [4, 5, 6]], [1, 2], [[1, "2"], [3, 4]]):
            with self.subTest(value=value), self.assertRaises(RequestError):
                validate_input(manifest, value)

    def test_input_and_output_reject_bool_nonfinite_and_float32_overflow(self):
        manifest = self.make_bound_manifest()
        for value in ([[True, 0], [0, 0]], [[math.nan, 0], [0, 0]],
                      [[math.inf, 0], [0, 0]], [[1e100, 0], [0, 0]]):
            with self.subTest(value=repr(value)), self.assertRaises(RequestError):
                validate_input(manifest, value)
        with self.assertRaises(RequestError):
            validate_output(manifest, [[0], [math.nan]])

    def test_response_constructors_are_fail_closed_and_validate_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = ManifestValidationTests().make_manifest(root, b"model")
            model = validate_manifest(raw, root)
            failed = safe_result("req-1", "invalid", "bad input", model.name)
            self.assertEqual(failed, {
                "schema": "xtos-ai-runtime-v1", "id": "req-1", "status": "invalid",
                "safe_action": "no_action", "reason": "bad input", "model": model.name,
                "output": None,
            })
            succeeded = success_result("req-2", model, [[0.2, 0.3, 0.5]], 1.25)
            self.assertEqual(succeeded["safe_action"], "action_allowed")
            self.assertEqual(succeeded["output"], [[0.20000000298023224, 0.30000001192092896, 0.5]])
            self.assertEqual(succeeded["latency_ms"], 1.25)
            invalid_results = (
                ([[math.nan, 0, 1]], 1),
                ([[0, 0]], 1),
                ([[0, 0, 1]], math.inf),
                ([[0, 0, 1]], -1),
                ([[0, 0, 1]], True),
            )
            for bad_output, latency in invalid_results:
                with self.subTest(output=bad_output, latency=latency), self.assertRaises(RequestError):
                    success_result("req-3", model, bad_output, latency)

    def test_request_ids_are_bounded_strings(self):
        for request_id in ("", "x" * 129, 1, None):
            with self.subTest(request_id=request_id), self.assertRaises(RequestError):
                safe_result(request_id, "invalid", "bad request")


if __name__ == "__main__":
    unittest.main()
