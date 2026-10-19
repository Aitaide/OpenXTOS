#!/usr/bin/env python3
"""Run all optional AI behavior suites; reject missing, failed or skipped tests."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = ("test_onnx_baseline.py", "test_torch_onnx_parity.py", "test_tf_onnx_parity.py")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tests-dir", type=Path, default=ROOT / "tools/xtos-onnx-baseline")
    parser.add_argument("--report", type=Path, default=ROOT / ".build/ai-acceptance/results.json")
    args = parser.parse_args()
    suite = unittest.TestSuite()
    missing = []
    for pattern in PATTERNS:
        found = unittest.TestLoader().discover(str(args.tests_dir), pattern=pattern)
        if not found.countTestCases():
            missing.append(pattern)
        suite.addTests(found)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    passed = bool(result.testsRun) and result.wasSuccessful() and not result.skipped and not missing
    versions = {}
    for package in ("numpy", "onnx", "onnxruntime", "torch", "tensorflow-cpu", "keras", "protobuf"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    revision = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True,
    )
    changes = subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True, text=True,
    )
    fixture = ROOT / "tools/xtos-onnx-baseline/fixtures/fixed_classifier.onnx"
    document = {
        "schema": "xtos-ai-acceptance-v1",
        "status": "pass" if passed else "fail",
        "tests_run": result.testsRun,
        "skipped": [{"test": test.id(), "reason": reason} for test, reason in result.skipped],
        "failures": [{"test": test.id(), "traceback": trace} for test, trace in result.failures],
        "errors": [{"test": test.id(), "traceback": trace} for test, trace in result.errors],
        "missing_suites": missing,
        "python": sys.executable,
        "python_version": sys.version,
        "versions": versions,
        "tests_dir": str(args.tests_dir.resolve()),
        "source_commit": revision.stdout.strip() if revision.returncode == 0 else "unknown",
        "source_state": ("dirty" if changes.stdout.strip() else "clean") if changes.returncode == 0 else "unknown",
        "model_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
        "scope": "CPU framework correctness and degradation tests; no hardware latency or GPU proof",
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"AI_TESTS={'PASS' if passed else 'FAIL'} tests={result.testsRun} skipped={len(result.skipped)} report={args.report}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
