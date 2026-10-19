#!/usr/bin/env python3
"""Provenance gate fixtures, not evidence of actual AI inference acceptance."""
from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = ("test_onnx_baseline.py", "test_torch_onnx_parity.py", "test_tf_onnx_parity.py")


class IsolatedRepositoryTests(unittest.TestCase):
    """Small disposable Git repositories; never commit the shared working tree."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.canonical = self.root / "tools/xtos-onnx-baseline"
        shutil.copytree(ROOT / "tools/xtos-onnx-baseline", self.canonical,
                        ignore=shutil.ignore_patterns("__pycache__"))
        (self.root / "scripts").mkdir()
        for name in ("test_ai.py", "verify_ai_acceptance.py"):
            if (ROOT / "scripts" / name).exists():
                shutil.copyfile(ROOT / "scripts" / name, self.root / "scripts" / name)
        (self.root / ".gitignore").write_text(".build/\n__pycache__/\n*.pyc\n")
        self.git("-c", "init.defaultBranch=main", "init", "--quiet")
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "--quiet", "-m", "synthetic provenance test fixture")
        self.commit = self.git("rev-parse", "HEAD").strip()
        self.report = self.root / ".build/results.json"
        self.report.parent.mkdir()

    def git(self, *arguments):
        result = subprocess.run(["git", "-C", str(self.root), *arguments],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def write_passing_suites(self, body="self.assertTrue(True)"):
        for name in PATTERNS:
            (self.canonical / name).write_text(
                "import unittest\nfrom pathlib import Path\nclass Fixture(unittest.TestCase):\n"
                "    def test_behavior(self):\n        " + body + "\n", encoding="utf-8")

    def run_runner(self, *arguments):
        result = subprocess.run(
            [sys.executable, str(self.root / "scripts/test_ai.py"),
             "--report", str(self.report), *arguments],
            capture_output=True, text=True, timeout=30)
        self.assertTrue(self.report.exists(), result.stderr)
        return result, json.loads(self.report.read_text(encoding="utf-8"))


class StrictRunnerTests(IsolatedRepositoryTests):
    def test_require_clean_rejects_nonignored_untracked_source(self):
        self.write_passing_suites()
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "--quiet", "-m", "passing test fixture")
        (self.root / "untracked-source.txt").write_text("untracked input")
        result, document = self.run_runner("--require-clean")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(document["source_start"]["state"], "dirty")
        self.assertTrue(any("untracked-source.txt" in entry
                            for entry in document["source_start"]["status"]))
        self.assertIn("clean source required", document["provenance_errors"])

    def test_unknown_git_source_never_claims_stable_provenance(self):
        self.write_passing_suites()
        (self.root / ".git").rename(self.root / ".build/hidden-git")
        result, document = self.run_runner()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(document["source_start"]["state"], "unknown")
        self.assertFalse(document["source_unchanged"])
        self.assertIn("source provenance unavailable", document["provenance_errors"])

    def test_missing_lock_cannot_produce_a_passing_report(self):
        self.write_passing_suites()
        (self.canonical / "requirements-ai.lock").unlink()
        result, document = self.run_runner()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(document["status"], "fail")
        self.assertIsNone(document["inputs_start"]["lock_sha256"])
        self.assertIn("acceptance input unavailable", document["provenance_errors"])

    def test_detects_changes_to_already_dirty_untracked_source_contents(self):
        self.write_passing_suites(
            "path = Path(__file__).resolve().parents[2] / 'untracked-source.txt'; "
            "path.write_text(path.read_text() + 'changed\\n')")
        (self.root / "untracked-source.txt").write_text("already dirty\n")
        result, document = self.run_runner()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(document["source_start"]["status"], document["source_end"]["status"])
        self.assertNotEqual(document["source_start"]["worktree_sha256"],
                            document["source_end"]["worktree_sha256"])
        self.assertFalse(document["source_unchanged"])

    def test_clean_source_with_ignored_outputs_is_accepted_by_strict_runner(self):
        self.write_passing_suites()
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "--quiet", "-m", "passing test fixture")
        revision = self.git("rev-parse", "HEAD").strip()
        result, document = self.run_runner("--require-clean", "--expected-source", revision)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(document["source_start"]["state"], "clean")
        self.assertTrue(document["source_unchanged"])


class AcceptanceVerifierTests(IsolatedRepositoryTests):
    def synthetic_report(self):
        """Only a gate fixture: no AI tests have run to produce this document."""
        spec = importlib.util.spec_from_file_location("fixture_runner", self.root / "scripts/test_ai.py")
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        source = runner.source_snapshot()
        suite_ids = {}
        for name in PATTERNS:
            tree = ast.parse((self.canonical / name).read_text())
            suite_ids[name] = [f"{Path(name).stem}.{cls.name}.{method.name}"
                               for cls in tree.body if isinstance(cls, ast.ClassDef)
                               for method in cls.body if isinstance(method, ast.FunctionDef)
                               and method.name.startswith("test")]
        inputs = runner.input_snapshot(self.canonical)
        pins = dict(re.findall(r"^([\w.-]+)==([^\s\\]+)",
                               (self.canonical / "requirements-ai.lock").read_text(),
                               re.MULTILINE))
        return {
            "schema": "xtos-ai-acceptance-v1", "status": "pass",
            "tests_run": sum(map(len, suite_ids.values())),
            "executed_test_ids": [test for tests in suite_ids.values() for test in tests],
            "suite_counts": {name: len(tests) for name, tests in suite_ids.items()},
            "skipped": [], "failures": [], "errors": [], "missing_suites": [],
            "provenance_errors": [], "tests_dir": str(self.canonical.resolve()),
            "source_commit": self.commit, "source_state": "clean",
            "source_start": source, "source_end": copy.deepcopy(source),
            "source_unchanged": True,
            "inputs_start": inputs, "inputs_end": copy.deepcopy(inputs),
            "model_sha256": inputs["model_sha256"],
            "versions": {name: pins[name] for name in
                         ("numpy", "onnx", "onnxruntime", "torch", "tensorflow-cpu", "keras", "protobuf")},
        }

    def verify(self, document, *arguments):
        self.report.write_text(json.dumps(document), encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(self.root / "scripts/verify_ai_acceptance.py"),
             "--report", str(self.report), *arguments],
            capture_output=True, text=True, timeout=30)

    def test_accepts_complete_current_clean_report_without_importing_ai_suites(self):
        document = self.synthetic_report()
        self.assertEqual(document["tests_run"], 11)
        result = self.verify(document)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("AI_ACCEPTANCE=PASS", result.stdout)
        self.assertIn("tests=11", result.stdout)

    def test_rejects_incomplete_duplicate_and_inconsistent_execution(self):
        original = self.synthetic_report()
        mutations = [
            ("missing suite", lambda doc: doc["missing_suites"].append(PATTERNS[0])),
            ("skip", lambda doc: doc["skipped"].append({"test": "fixture", "reason": "missing"})),
            ("failure", lambda doc: doc["failures"].append({"test": "fixture"})),
            ("error", lambda doc: doc["errors"].append({"test": "fixture"})),
            ("missing ids", lambda doc: doc.pop("executed_test_ids")),
            ("short run", lambda doc: doc["executed_test_ids"].pop()),
            ("duplicate", lambda doc: doc["executed_test_ids"].__setitem__(0, doc["executed_test_ids"][1])),
            ("wrong id", lambda doc: doc["executed_test_ids"].__setitem__(0, "fixture.Fake.test_false")),
            ("total", lambda doc: doc.__setitem__("tests_run", 3)),
            ("suite total", lambda doc: doc["suite_counts"].__setitem__(PATTERNS[0], 6)),
            ("suite omitted", lambda doc: doc["suite_counts"].pop(PATTERNS[2])),
            ("old report", lambda doc: doc.pop("suite_counts")),
        ]
        for label, mutate in mutations:
            with self.subTest(label=label):
                document = copy.deepcopy(original)
                mutate(document)
                result = self.verify(document)
                self.assertNotEqual(result.returncode, 0, label + result.stdout)
                self.assertIn("AI_ACCEPTANCE=FAIL", result.stderr)

    def test_suites_are_never_imported_for_inventory(self):
        for name in PATTERNS:
            path = self.canonical / name
            # A future import must remain first, so insert after it if present.
            text = path.read_text()
            anchor = "from __future__ import annotations\n"
            blocker = "raise AssertionError('verifier must not import AI test modules')\n"
            if anchor in text:
                text = text.replace(anchor, anchor + blocker, 1)
            else:
                text = blocker + text
            path.write_text(text)
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "--quiet", "-m", "AST-only gate fixture")
        self.commit = self.git("rev-parse", "HEAD").strip()
        result = self.verify(self.synthetic_report())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_wrong_directory_stale_source_and_unbound_reports(self):
        original = self.synthetic_report()
        mutations = [
            ("fixture directory", lambda doc: doc.__setitem__("tests_dir", str(self.root / ".build"))),
            ("relative directory", lambda doc: doc.__setitem__("tests_dir", "tools/xtos-onnx-baseline")),
            ("old commit", lambda doc: doc.__setitem__("source_commit", "0" * 40)),
            ("dirty", lambda doc: doc.__setitem__("source_state", "dirty")),
            ("unknown", lambda doc: doc.__setitem__("source_state", "unknown")),
            ("missing start", lambda doc: doc.pop("source_start")),
            ("missing end", lambda doc: doc.pop("source_end")),
            ("changed flag", lambda doc: doc.__setitem__("source_unchanged", False)),
            ("changed end", lambda doc: doc["source_end"].__setitem__("worktree_sha256", "0" * 64)),
            ("untracked at start", lambda doc: doc["source_start"]["status"].append("?? source.py")),
            ("old inputs", lambda doc: doc.pop("inputs_start")),
            ("wrong model", lambda doc: doc.__setitem__("model_sha256", "0" * 64)),
        ]
        for label, mutate in mutations:
            with self.subTest(label=label):
                document = copy.deepcopy(original)
                mutate(document)
                result = self.verify(document)
                self.assertNotEqual(result.returncode, 0, label + result.stdout)
                self.assertIn("AI_ACCEPTANCE=FAIL", result.stderr)
    def test_rejects_missing_or_mismatched_direct_locked_versions(self):
        original = self.synthetic_report()
        for package in original["versions"]:
            for value in (None, "wrong-version"):
                with self.subTest(package=package, value=value):
                    document = copy.deepcopy(original)
                    document["versions"][package] = value
                    result = self.verify(document)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn(package, result.stderr)
        document = copy.deepcopy(original)
        document.pop("versions")
        result = self.verify(document)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("versions", result.stderr)

    def test_rejects_each_missing_or_changed_input_hash_at_either_boundary(self):
        original = self.synthetic_report()
        for boundary in ("inputs_start", "inputs_end"):
            for field in ("model_sha256", "lock_sha256", "suite_sha256", "runner_sha256"):
                with self.subTest(boundary=boundary, field=field):
                    document = copy.deepcopy(original)
                    value = document[boundary][field]
                    if isinstance(value, dict):
                        value[next(iter(value))] = "0" * 64
                    else:
                        document[boundary][field] = "0" * 64
                    result = self.verify(document)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn("hash", result.stderr)
                with self.subTest(boundary=boundary, field=field, missing=True):
                    document = copy.deepcopy(original)
                    document[boundary].pop(field)
                    result = self.verify(document)
                    self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_expected_source_is_exact_and_cannot_override_current_head(self):
        document = self.synthetic_report()
        result = self.verify(document, "--expected-source", self.commit)
        self.assertEqual(result.returncode, 0, result.stderr)
        for sha in ("0" * 40, self.commit[:7], "HEAD", self.commit + "\n"):
            with self.subTest(sha=sha):
                result = self.verify(document, "--expected-source", sha)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("expected source", result.stderr)

    def test_current_nonignored_untracked_source_invalidates_clean_report(self):
        document = self.synthetic_report()
        (self.root / "new-source.txt").write_text("not ignored")
        result = self.verify(document)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("clean", result.stderr)

    def test_current_input_file_change_invalidates_report(self):
        document = self.synthetic_report()
        (self.canonical / "baseline.py").write_text("# changed after report\n")
        result = self.verify(document)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("clean", result.stderr)

    def test_clean_git_status_cannot_hide_inputs_that_differ_from_head(self):
        self.git("update-index", "--assume-unchanged", "tools/xtos-onnx-baseline/baseline.py")
        (self.canonical / "baseline.py").write_text("# hidden dirty input\n")
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=all"), "")
        document = self.synthetic_report()
        result = self.verify(document)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("HEAD", result.stderr)

    def test_missing_invalid_json_and_nonobject_reports_fail_with_diagnostics(self):
        for payload in (None, "{not-json", "null", "[]", '"scalar"', "{}"):
            with self.subTest(payload=payload):
                if self.report.exists():
                    self.report.unlink()
                if payload is not None:
                    self.report.write_text(payload)
                result = subprocess.run(
                    [sys.executable, str(self.root / "scripts/verify_ai_acceptance.py"),
                     "--report", str(self.report)], capture_output=True, text=True, timeout=30)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("AI_ACCEPTANCE=FAIL", result.stderr)
                self.assertIn(str(self.report), result.stderr)
                if payload is not None:
                    self.assertEqual(self.report.read_text(), payload)

    def test_rejects_ambiguous_duplicate_json_keys(self):
        document = self.synthetic_report()
        payload = json.dumps(document)
        self.report.write_text('{"source_state":"dirty",' + payload[1:], encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(self.root / "scripts/verify_ai_acceptance.py"),
             "--report", str(self.report)], capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("duplicate", result.stderr)


class RunnerProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.tests_dir = self.directory / "fixtures"
        self.tests_dir.mkdir()
        for name in PATTERNS:
            (self.tests_dir / name).write_text(
                "import unittest\nclass Fixture(unittest.TestCase):\n"
                "    def test_behavior(self):\n        self.assertTrue(True)\n",
                encoding="utf-8",
            )

    def run_fixture(self, *arguments):
        report = self.directory / "results.json"
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/test_ai.py"),
             "--tests-dir", str(self.tests_dir), "--report", str(report), *arguments],
            capture_output=True, text=True, timeout=30,
        )
        self.assertTrue(report.is_file(), result.stderr)
        return result, json.loads(report.read_text(encoding="utf-8"))

    def test_records_executed_ids_and_per_suite_counts(self):
        result, document = self.run_fixture()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(document.get("executed_test_ids"), [
            f"{Path(name).stem}.Fixture.test_behavior" for name in PATTERNS
        ])
        self.assertEqual(document.get("suite_counts"), {name: 1 for name in PATTERNS})

    def test_nested_package_tests_dir_remains_supported_and_hashed(self):
        nested = self.tests_dir / "package"
        nested.mkdir()
        (nested / "__init__.py").write_text("")
        for name in PATTERNS:
            (self.tests_dir / name).rename(nested / name)
        result, document = self.run_fixture()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(document["suite_counts"], {name: 1 for name in PATTERNS})
        self.assertEqual(document["inputs_start"]["suite_sha256"], {
            f"package/{name}": hashlib.sha256((nested / name).read_bytes()).hexdigest()
            for name in PATTERNS
        })

    def test_binds_suite_lock_model_and_runner_sources_at_both_boundaries(self):
        result, document = self.run_fixture()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("source_start", document)
        self.assertEqual(document["source_start"], document["source_end"])
        self.assertTrue(document["source_unchanged"])
        self.assertEqual(document["source_commit"], document["source_end"]["commit"])
        self.assertEqual(document["source_state"], document["source_end"]["state"])
        inputs = document["inputs_start"]
        self.assertEqual(inputs, document["inputs_end"])
        self.assertEqual(inputs["suite_sha256"], {
            name: hashlib.sha256((self.tests_dir / name).read_bytes()).hexdigest()
            for name in PATTERNS
        })
        canonical = ROOT / "tools/xtos-onnx-baseline"
        self.assertEqual(inputs["lock_sha256"], hashlib.sha256(
            (canonical / "requirements-ai.lock").read_bytes()).hexdigest())
        self.assertEqual(inputs["model_sha256"], document["model_sha256"])
        self.assertEqual(inputs["runner_sha256"]["scripts/test_ai.py"], hashlib.sha256(
            (ROOT / "scripts/test_ai.py").read_bytes()).hexdigest())
        self.assertIn("tools/xtos-onnx-baseline/baseline.py", inputs["runner_sha256"])

    def test_source_change_during_run_is_nonzero_even_if_all_tests_pass(self):
        path = self.tests_dir / PATTERNS[0]
        path.write_text(
            "import unittest\nfrom pathlib import Path\nclass Fixture(unittest.TestCase):\n"
            "    def test_behavior(self):\n"
            "        path = Path(__file__)\n"
            "        path.write_text(path.read_text() + '# changed during run\\n')\n",
            encoding="utf-8",
        )
        result, document = self.run_fixture()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(document["status"], "fail")
        self.assertFalse(document["source_unchanged"])
        self.assertIn("source changed during run", document["provenance_errors"])

    def test_expected_source_mismatch_keeps_a_failed_report(self):
        result, document = self.run_fixture("--expected-source", "0" * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(document["status"], "fail")
        self.assertTrue(any("expected source" in error for error in document["provenance_errors"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
