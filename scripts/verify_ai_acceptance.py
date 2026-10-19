#!/usr/bin/env python3
"""Verify AI acceptance provenance without importing optional AI frameworks.

This is a consistency gate for trusted local runner output, not a report signature
or a substitute for executing the AI suites. Gate fixtures are not AI evidence.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = ("test_onnx_baseline.py", "test_torch_onnx_parity.py", "test_tf_onnx_parity.py")
CANONICAL_TESTS = ROOT / "tools/xtos-onnx-baseline"
DIRECT_PACKAGES = ("numpy", "onnx", "onnxruntime", "torch", "tensorflow-cpu", "keras", "protobuf")


def unique_object(pairs):
    document = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"duplicate JSON key: {key}")
        document[key] = value
    return document


def expected_suite_ids() -> dict[str, list[str]]:
    """Static, fail-closed inventory for these direct unittest.TestCase suites.

    Do not import suites: decorators probe optional AI runtimes at import time.
    Inheritance/dynamic discovery requires an explicit future inventory policy.
    """
    inventory = {}
    for name in PATTERNS:
        tree = ast.parse((CANONICAL_TESTS / name).read_text(encoding="utf-8"), filename=name)
        if any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "load_tests"
               for node in tree.body):
            raise ValueError(f"unsupported dynamic test inventory: {name}")
        ids = []
        for cls in (node for node in tree.body if isinstance(node, ast.ClassDef)):
            if len(cls.bases) != 1 or not isinstance(cls.bases[0], ast.Attribute) or (
                ast.unparse(cls.bases[0]) != "unittest.TestCase"
            ):
                raise ValueError(f"unsupported TestCase inventory: {name}:{cls.name}")
            for method in cls.body:
                if isinstance(method, ast.FunctionDef) and method.name.startswith("test"):
                    ids.append(f"{Path(name).stem}.{cls.name}.{method.name}")
        if not ids or len(ids) != len(set(ids)):
            raise ValueError(f"missing/duplicate suite test definitions: {name}")
        inventory[name] = ids
    return inventory


def validate_execution(document: dict) -> int:
    for field in ("skipped", "failures", "errors", "missing_suites", "provenance_errors"):
        if document.get(field) != []:
            raise ValueError(f"{field} must be present and empty")
    inventory = expected_suite_ids()
    expected = [test_id for tests in inventory.values() for test_id in tests]
    actual = document.get("executed_test_ids")
    if not isinstance(actual, list) or not all(isinstance(test_id, str) for test_id in actual):
        raise ValueError("executed_test_ids must be a list of test IDs")
    if Counter(actual) != Counter(expected):
        raise ValueError("executed_test_ids contain missing, unexpected or duplicate tests")
    if type(document.get("tests_run")) is not int or document["tests_run"] != len(expected):
        raise ValueError("tests_run does not match the source-derived inventory")
    counts = document.get("suite_counts")
    if not isinstance(counts, dict) or set(counts) != set(inventory) or any(
        type(counts[name]) is not int or counts[name] != len(tests)
        for name, tests in inventory.items()
    ):
        raise ValueError("suite_counts do not match the source-derived inventory")
    return len(expected)


def clean_source(expected_source: str | None) -> dict:
    def git(*arguments):
        result = subprocess.run(["git", "--no-optional-locks", "-C", str(ROOT), *arguments],
                                capture_output=True, timeout=30)
        if result.returncode:
            raise ValueError("Git source is unknown: " + result.stderr.decode("utf-8", "replace").strip())
        return result.stdout

    if expected_source is not None and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", expected_source):
        raise ValueError("expected source must be an exact full Git commit SHA")
    commit = git("rev-parse", "HEAD").decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise ValueError("Git source commit is unknown")
    if expected_source is not None and commit != expected_source:
        raise ValueError("current HEAD does not match expected source")
    if git("status", "--porcelain=v1", "-z", "--untracked-files=all"):
        raise ValueError("current source must be clean, including nonignored untracked files")
    digest = hashlib.sha256()
    for data in (commit.encode(), b"", b"", b""):
        digest.update(len(data).to_bytes(8, "big") + data)
    return {"commit": commit, "state": "clean", "status": [],
            "worktree_sha256": digest.hexdigest()}


def current_inputs() -> dict:
    def checksum(path):
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    runners = [ROOT / "scripts/test_ai.py", ROOT / "scripts/verify_ai_acceptance.py"]
    runners.extend(path for path in sorted(CANONICAL_TESTS.glob("*.py")) if path.name not in PATTERNS)
    return {
        "model_sha256": checksum(CANONICAL_TESTS / "fixtures/fixed_classifier.onnx"),
        "lock_sha256": checksum(CANONICAL_TESTS / "requirements-ai.lock"),
        "suite_sha256": {name: checksum(CANONICAL_TESTS / name) for name in PATTERNS},
        "runner_sha256": {str(path.relative_to(ROOT)): checksum(path) for path in runners},
    }


def validate_committed_inputs(inputs: dict, commit: str) -> None:
    """Git status can hide assume-unchanged/skip-worktree paths; bind bytes too."""
    paths = {
        "tools/xtos-onnx-baseline/fixtures/fixed_classifier.onnx": inputs["model_sha256"],
        "tools/xtos-onnx-baseline/requirements-ai.lock": inputs["lock_sha256"],
        **{f"tools/xtos-onnx-baseline/{name}": checksum
           for name, checksum in inputs["suite_sha256"].items()},
        **inputs["runner_sha256"],
    }
    for path, checksum in paths.items():
        result = subprocess.run(["git", "-C", str(ROOT), "show", f"{commit}:{path}"],
                                capture_output=True, timeout=30)
        if result.returncode or hashlib.sha256(result.stdout).hexdigest() != checksum:
            raise ValueError(f"input does not match committed HEAD: {path}")


def validate_provenance(document: dict, expected_source: str | None) -> tuple[dict, dict]:
    if document.get("tests_dir") != str(CANONICAL_TESTS.resolve()):
        raise ValueError("tests_dir must be the canonical repository tools/xtos-onnx-baseline directory")
    source = clean_source(expected_source)
    if document.get("source_commit") != source["commit"] or document.get("source_state") != "clean":
        raise ValueError("report source commit/state does not match current clean HEAD")
    if document.get("source_unchanged") is not True:
        raise ValueError("source_unchanged must be true")
    for boundary in ("source_start", "source_end"):
        if document.get(boundary) != source:
            raise ValueError(f"{boundary} does not match current clean source")
    inputs = current_inputs()
    for boundary in ("inputs_start", "inputs_end"):
        if document.get(boundary) != inputs:
            raise ValueError(f"{boundary} model/lock/suite/runner hashes do not match current files")
    if document.get("model_sha256") != inputs["model_sha256"]:
        raise ValueError("model_sha256 does not match current fixture")
    validate_committed_inputs(inputs, source["commit"])
    return source, inputs


def validate_versions(document: dict) -> None:
    """Compare actual metadata to the lock, including torch's +cpu local pin."""
    versions = document.get("versions")
    if not isinstance(versions, dict):
        raise ValueError("versions must record the seven direct locked dependencies")
    locked = re.findall(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)",
                        (CANONICAL_TESTS / "requirements-ai.lock").read_text(encoding="utf-8"),
                        re.MULTILINE)
    for package in DIRECT_PACKAGES:
        pins = [version for name, version in locked if name == package]
        if len(pins) != 1:
            raise ValueError(f"lock must contain exactly one pin for {package}")
        if versions.get(package) != pins[0]:
            raise ValueError(f"versions mismatch for {package}: expected {pins[0]}, got {versions.get(package)!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--expected-source", help="Exact full Git SHA (default: current HEAD)")
    args = parser.parse_args()
    try:
        document = json.loads(args.report.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
        if not isinstance(document, dict):
            raise ValueError("report must be a JSON object")
        if document.get("schema") != "xtos-ai-acceptance-v1" or document.get("status") != "pass":
            raise ValueError("report schema/status is not a passing AI acceptance report")
        count = validate_execution(document)
        source, inputs = validate_provenance(document, args.expected_source)
        validate_versions(document)
        if clean_source(args.expected_source) != source or current_inputs() != inputs:
            raise ValueError("source changed during verification")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"AI_ACCEPTANCE=FAIL report={args.report}: {error}", file=sys.stderr)
        return 1
    print(f"AI_ACCEPTANCE=PASS tests={count} report={args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
