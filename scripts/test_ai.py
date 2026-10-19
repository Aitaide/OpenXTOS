#!/usr/bin/env python3
"""Run all optional AI behavior suites; reject missing, failed or skipped tests."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import stat
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = ("test_onnx_baseline.py", "test_torch_onnx_parity.py", "test_tf_onnx_parity.py")
CANONICAL_TESTS = ROOT / "tools/xtos-onnx-baseline"


def file_sha256(path: Path) -> str | None:
    try:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        return None


def selected_suite_paths(tests_dir: Path) -> dict[str, Path]:
    """Return every selected suite source, keyed relative to the test root."""
    selected = {}
    for pattern in PATTERNS:
        for path in sorted(tests_dir.rglob(pattern)):
            if path.is_file() and "__pycache__" not in path.parts:
                selected[path.relative_to(tests_dir).as_posix()] = path
    return selected


def input_snapshot(tests_dir: Path) -> dict:
    """Hash the selected suites and the real tools they exercise before import."""
    runners = [ROOT / "scripts/test_ai.py"]
    verifier = ROOT / "scripts/verify_ai_acceptance.py"
    if verifier.exists():
        runners.append(verifier)
    runners.extend(path for path in sorted(CANONICAL_TESTS.glob("*.py"))
                   if path.name not in PATTERNS)
    return {
        "model_sha256": file_sha256(CANONICAL_TESTS / "fixtures/fixed_classifier.onnx"),
        "lock_sha256": file_sha256(CANONICAL_TESTS / "requirements-ai.lock"),
        "suite_sha256": {name: file_sha256(path) for name, path in selected_suite_paths(tests_dir).items()},
        "runner_sha256": {str(path.relative_to(ROOT)): file_sha256(path) for path in runners},
    }


def source_snapshot() -> dict:
    """Bind HEAD, tracked changes and nonignored untracked *contents*."""
    def git(*arguments):
        result = subprocess.run(["git", "--no-optional-locks", "-C", str(ROOT), *arguments],
                                capture_output=True, timeout=30)
        if result.returncode:
            raise OSError(result.stderr.decode("utf-8", "replace").strip())
        return result.stdout

    try:
        commit = git("rev-parse", "HEAD").decode("ascii").strip()
        changes = git("status", "--porcelain=v1", "-z", "--untracked-files=all")
        digest = hashlib.sha256()
        for data in (
            commit.encode(), changes,
            git("diff", "HEAD", "--binary", "--no-ext-diff", "--no-textconv"),
            git("diff", "--cached", "HEAD", "--binary", "--no-ext-diff", "--no-textconv"),
        ):
            digest.update(len(data).to_bytes(8, "big") + data)
        for name in sorted(git("ls-files", "--others", "--exclude-standard", "-z").split(b"\0")):
            if not name:
                continue
            path = ROOT / os.fsdecode(name)
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                content = os.fsencode(os.readlink(path))
            elif stat.S_ISREG(mode):
                checksum = file_sha256(path)
                if checksum is None:
                    raise OSError(f"cannot hash untracked source: {path}")
                content = checksum.encode()
            else:
                raise OSError(f"unsupported untracked source: {path}")
            for data in (name, str(mode).encode(), content):
                digest.update(len(data).to_bytes(8, "big") + data)
        return {"commit": commit, "state": "dirty" if changes else "clean",
                "status": changes.decode("utf-8", "surrogateescape").split("\0")[:-1],
                "worktree_sha256": digest.hexdigest()}
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        return {"commit": "unknown", "state": "unknown", "status": [],
                "worktree_sha256": None, "error": str(error)}


class RecordingResult(unittest.TextTestResult):
    """Record actual test starts, not the discovered suite's advertised size."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.executed_test_ids = []

    def startTest(self, test):
        self.executed_test_ids.append(test.id())
        super().startTest(test)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tests-dir", type=Path, default=ROOT / "tools/xtos-onnx-baseline")
    parser.add_argument("--report", type=Path, default=ROOT / ".build/ai-acceptance/results.json")
    parser.add_argument("--expected-source", help="Require an exact full Git commit SHA")
    parser.add_argument("--require-clean", action="store_true", help="Reject dirty/unknown Git state")
    args = parser.parse_args()
    source_start = source_snapshot()
    inputs_start = input_snapshot(args.tests_dir)
    suite = unittest.TestSuite()
    missing = []
    for pattern in PATTERNS:
        found = unittest.TestLoader().discover(str(args.tests_dir), pattern=pattern)
        if not found.countTestCases() or not any(
            Path(name).name == pattern for name in inputs_start["suite_sha256"]
        ):
            missing.append(pattern)
        suite.addTests(found)
    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(suite)
    passed = bool(result.testsRun) and result.wasSuccessful() and not result.skipped and not missing
    versions = {}
    for package in ("numpy", "onnx", "onnxruntime", "torch", "tensorflow-cpu", "keras", "protobuf"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    inputs_end = input_snapshot(args.tests_dir)
    source_end = source_snapshot()
    source_known = all(snapshot["state"] in ("clean", "dirty") for snapshot in (source_start, source_end))
    source_unchanged = source_known and source_start == source_end and inputs_start == inputs_end
    provenance_errors = []
    for inputs in (inputs_start, inputs_end):
        hashes = [inputs["model_sha256"], inputs["lock_sha256"],
                  *inputs["suite_sha256"].values(), *inputs["runner_sha256"].values()]
        if any(checksum is None for checksum in hashes):
            provenance_errors.append("acceptance input unavailable")
            break
    if not source_known:
        provenance_errors.append("source provenance unavailable")
    if args.require_clean and any(snapshot["state"] != "clean" for snapshot in (source_start, source_end)):
        provenance_errors.append("clean source required")
    if args.expected_source is not None and (
        not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", args.expected_source)
        or any(snapshot["commit"] != args.expected_source for snapshot in (source_start, source_end))
    ):
        provenance_errors.append(f"expected source does not match: {args.expected_source}")
    if not source_unchanged:
        provenance_errors.append("source changed during run")
    passed = passed and not provenance_errors
    document = {
        "schema": "xtos-ai-acceptance-v1",
        "status": "pass" if passed else "fail",
        "tests_run": result.testsRun,
        "executed_test_ids": result.executed_test_ids,
        "suite_counts": {
            name: sum(test_id.rsplit(".", 3)[-3] == Path(name).stem
                      for test_id in result.executed_test_ids)
            for name in PATTERNS
        },
        "skipped": [{"test": test.id(), "reason": reason} for test, reason in result.skipped],
        "failures": [{"test": test.id(), "traceback": trace} for test, trace in result.failures],
        "errors": [{"test": test.id(), "traceback": trace} for test, trace in result.errors],
        "missing_suites": missing,
        "python": sys.executable,
        "python_version": sys.version,
        "versions": versions,
        "tests_dir": str(args.tests_dir.resolve()),
        "source_commit": source_end["commit"],
        "source_state": source_end["state"],
        "source_start": source_start,
        "source_end": source_end,
        "source_unchanged": source_unchanged,
        "provenance_errors": provenance_errors,
        "inputs_start": inputs_start,
        "inputs_end": inputs_end,
        "model_sha256": inputs_end["model_sha256"],
        "scope": "CPU framework correctness and degradation tests; no hardware latency or GPU proof",
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"AI_TESTS={'PASS' if passed else 'FAIL'} tests={result.testsRun} skipped={len(result.skipped)} report={args.report}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
