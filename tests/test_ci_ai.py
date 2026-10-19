#!/usr/bin/env python3
"""Exercise the parsed strict-AI CI contract (PyYAML or Node yaml required)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/ci.yml"


def load_workflow():
    """Use an actual YAML parser, rejecting duplicate mapping keys."""
    try:
        import yaml
    except ImportError:
        node = shutil.which("node")
        if not node:
            raise RuntimeError("Use an isolated PyYAML environment or Node's yaml package")
        result = subprocess.run(
            [node, "-e", """
const fs = require('node:fs');
const yaml = require('yaml');
const document = yaml.parseDocument(fs.readFileSync(process.argv[1], 'utf8'),
                                    {version: '1.2', uniqueKeys: true});
if (document.errors.length) throw document.errors[0];
process.stdout.write(JSON.stringify(document.toJSON()));
""", str(WORKFLOW)],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode:
            raise RuntimeError("Node yaml parsing failed: " + result.stderr)
        return json.loads(result.stdout)

    class UniqueLoader(yaml.SafeLoader):
        pass

    def unique_mapping(loader, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in result:
                raise ValueError(f"Duplicate YAML key: {key!r}")
            result[key] = loader.construct_object(value_node, deep=deep)
        return result

    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=UniqueLoader)


class StrictAiCiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = load_workflow()

    def ai_job(self):
        self.assertIn("ai-acceptance", self.workflow["jobs"],
                      "AI acceptance must not be the optional userspace path")
        return self.workflow["jobs"]["ai-acceptance"]

    def test_strict_ai_is_independent_bounded_python311_job(self):
        job = self.ai_job()
        self.assertEqual(job["runs-on"], "ubuntu-24.04")
        self.assertIsInstance(job["timeout-minutes"], int)
        self.assertGreater(job["timeout-minutes"], 0)
        self.assertLessEqual(job["timeout-minutes"], 60)
        self.assertNotIn("needs", job)
        self.assertFalse(job.get("continue-on-error", False))
        checkout = [step for step in job["steps"] if step.get("uses") == "actions/checkout@v4"]
        self.assertEqual(len(checkout), 1)
        self.assertEqual(checkout[0]["with"]["ref"], "${{ github.sha }}")
        setup = [step for step in job["steps"] if step.get("uses") == "actions/setup-python@v5"]
        self.assertEqual(len(setup), 1)
        self.assertEqual(str(setup[0]["with"]["python-version"]), "3.11")

    def test_ai_dependencies_are_isolated_hash_locked_single_threaded(self):
        job = self.ai_job()
        self.assertEqual(job.get("defaults", {}).get("run", {}).get("shell"), "bash")
        expected = {
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
            "TF_CPP_MIN_LOG_LEVEL": "2", "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1", "VECLIB_MAXIMUM_THREADS": "1",
            "TF_NUM_INTRAOP_THREADS": "1", "TF_NUM_INTEROP_THREADS": "1",
        }
        self.assertEqual({key: str(job.get("env", {}).get(key)) for key in expected}, expected)
        step = next((step for step in job["steps"] if step.get("id") == "install-ai"), None)
        self.assertIsNotNone(step, "The locked install must be a mandatory standalone step")
        self.assertNotIn("if", step)
        self.assertFalse(step.get("continue-on-error", False))
        commands = [shlex.split(line) for line in step["run"].splitlines() if line.strip()]
        venv = ["python3", "-m", "venv", ".build/ai-acceptance/venv"]
        install = [".build/ai-acceptance/venv/bin/python", "-m", "pip", "install",
                   "--require-hashes", "-r", "tools/xtos-onnx-baseline/requirements-ai.lock"]
        self.assertTrue(any(command[:len(venv)] == venv for command in commands))
        self.assertTrue(any(command[:len(install)] == install for command in commands))
        self.assertLess(next(i for i, command in enumerate(commands) if command[:len(venv)] == venv),
                        next(i for i, command in enumerate(commands) if command[:len(install)] == install))
        for other in job["steps"]:
            self.assertFalse(other.get("continue-on-error", False))
            self.assertFalse(set(expected) & set(other.get("env", {})),
                             "Steps must not override the isolation/thread contract")
            for line in other.get("run", "").splitlines():
                command = shlex.split(line)
                self.assertFalse(command and command[0] in {"pip", "pip3", "uv", "sudo"}, line)
                self.assertNotIn("--system-site-packages", command)

    def required_step(self, step_id):
        steps = self.ai_job()["steps"]
        matching = [step for step in steps if step.get("id") == step_id]
        self.assertEqual(len(matching), 1, f"Missing or repeated mandatory step: {step_id}")
        self.assertFalse(matching[0].get("continue-on-error", False))
        return matching[0]

    def run_step(self, step, directory, **environment):
        env = os.environ.copy()
        env.update({key: str(value) for key, value in self.ai_job().get("env", {}).items()})
        env.update(environment)
        self.assertEqual(step.get("shell", "bash"), "bash")
        return subprocess.run(["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", step["run"]],
                              cwd=directory, env=env, capture_output=True, text=True, timeout=15)

    def git_fixture(self, directory):
        def git(*args):
            return subprocess.check_output(["git", *args], cwd=directory, text=True,
                                           stderr=subprocess.STDOUT).strip()
        git("init", "-q")
        (directory / ".gitignore").write_text("/.build/\n", encoding="utf-8")
        (directory / "source.py").write_text("original source\n", encoding="utf-8")
        git("add", ".gitignore", "source.py")
        # Only the disposable fixture is committed; the shared worktree is untouched.
        git("-c", "user.name=CI fixture", "-c", "user.email=ci-fixture@invalid.local",
            "-c", "commit.gpgsign=false", "commit", "-q", "-m", "fixture source")
        return git("rev-parse", "HEAD")

    def test_clean_sha_checks_reject_tracked_and_untracked_source_before_and_after(self):
        before = self.required_step("source-before")
        after = self.required_step("source-after")
        self.assertNotIn("if", before)
        self.assertIn(after.get("if"), ("always()", "${{ always() }}"))
        steps = self.ai_job()["steps"]
        self.assertLess(steps.index(before), steps.index(self.required_step("install-ai")))
        self.assertGreater(steps.index(after), steps.index(self.required_step("install-ai")))
        for step in (before, after):
            with self.subTest(step=step["id"]), tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
                directory = Path(tmp)
                head = self.git_fixture(directory)
                result = self.run_step(step, directory, GITHUB_SHA=head)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                result = self.run_step(step, directory, GITHUB_SHA="0" * 40)
                self.assertNotEqual(result.returncode, 0, "A stale checkout SHA was accepted")
                (directory / "source.py").write_text("modified source\n", encoding="utf-8")
                result = self.run_step(step, directory, GITHUB_SHA=head)
                self.assertNotEqual(result.returncode, 0, "Tracked source modifications were accepted")
                (directory / "source.py").write_text("original source\n", encoding="utf-8")
                (directory / "new_source.py").write_text("untracked source\n", encoding="utf-8")
                result = self.run_step(step, directory, GITHUB_SHA=head)
                self.assertNotEqual(result.returncode, 0, "Untracked source was accepted")

    def test_runner_and_verifier_use_venv_exact_sha_and_propagate_failures(self):
        runner = self.required_step("run-ai")
        verifier = self.required_step("verify-ai")
        self.assertNotIn("if", runner)
        self.assertIn(verifier.get("if"), ("always()", "${{ always() }}"))
        steps = self.ai_job()["steps"]
        self.assertLess(steps.index(self.required_step("install-ai")), steps.index(runner))
        self.assertLess(steps.index(runner), steps.index(verifier))
        self.assertLess(steps.index(verifier), steps.index(self.required_step("source-after")))
        report = ".build/ai-acceptance/results.json"
        cases = (
            (runner, "test_ai.py", ["--require-clean", "--expected-source", "ci-source-sha",
                                    "--report", report], "strict-run.log"),
            (verifier, "verify_ai_acceptance.py", ["--report", report, "--expected-source", "ci-source-sha"],
             "verify-report.log"),
        )
        for step, script, expected_args, log_name in cases:
            for exit_code in (0, 7):
                with self.subTest(step=step["id"], exit_code=exit_code), tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
                    directory = Path(tmp)
                    python = directory / ".build/ai-acceptance/venv/bin/python"
                    python.parent.mkdir(parents=True)
                    python.symlink_to(shutil.which("python3"))
                    (directory / "scripts").mkdir()
                    (directory / "scripts" / script).write_text(
                        "import json, os, sys\n"
                        "from pathlib import Path\n"
                        "Path('invocation.json').write_text(json.dumps({'args': sys.argv[1:], 'python': sys.executable}))\n"
                        "print('fixture stdout', flush=True)\n"
                        "print('fixture stderr', file=sys.stderr, flush=True)\n"
                        "raise SystemExit(int(os.environ['FIXTURE_EXIT']))\n", encoding="utf-8")
                    result = self.run_step(step, directory, GITHUB_SHA="ci-source-sha", FIXTURE_EXIT=str(exit_code))
                    self.assertEqual(result.returncode, exit_code, result.stderr + result.stdout)
                    invocation = json.loads((directory / "invocation.json").read_text())
                    self.assertEqual(invocation["args"], expected_args)
                    self.assertEqual(Path(invocation["python"]), python)
                    log = directory / ".build/ai-acceptance" / log_name
                    self.assertTrue(log.is_file(), "Actual stdout/stderr must survive an unsuccessful command")
                    self.assertIn("fixture stdout", log.read_text())
                    self.assertIn("fixture stderr", log.read_text())
                    self.assertFalse((directory / report).exists(), "Workflow must never synthesize results")

    def test_real_results_and_logs_are_always_uploaded_without_the_venv(self):
        steps = self.ai_job()["steps"]
        artifacts = [step for step in steps if step.get("uses") == "actions/upload-artifact@v4"]
        self.assertEqual(len(artifacts), 1, "Strict AI evidence must be retained on failure")
        artifact = artifacts[0]
        self.assertIn(artifact.get("if"), ("always()", "${{ always() }}"))
        self.assertFalse(artifact.get("continue-on-error", False))
        config = artifact["with"]
        self.assertEqual(config["if-no-files-found"], "error")
        self.assertIs(config["include-hidden-files"], True, ".build evidence is hidden by default")
        self.assertEqual(config["path"].splitlines(),
                         [".build/ai-acceptance/results.json", ".build/ai-acceptance/*.log"])
        self.assertLess(steps.index(self.required_step("source-after")), steps.index(artifact))

    def test_userspace_keeps_optional_path_without_installing_ai_dependencies(self):
        job = self.workflow["jobs"]["userspace"]
        commands = [step["run"] for step in job["steps"] if "run" in step]
        self.assertIn("./scripts/test.sh", commands)
        self.assertNotIn("needs", job)
        self.assertNotIn("XTOS_AI_PYTHON", job.get("env", {}))
        for step in job["steps"]:
            self.assertNotIn("XTOS_AI_PYTHON", step.get("env", {}))
            for line in step.get("run", "").splitlines():
                command = shlex.split(line)
                self.assertFalse(set(command) & {"pip", "pip3", "uv", "torch", "tensorflow-cpu",
                                                 "requirements-ai.lock", "scripts/test_ai.py"}, line)
                self.assertFalse(any("requirements-ai" in argument for argument in command), line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
