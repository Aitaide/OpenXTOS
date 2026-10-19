#!/usr/bin/env python3
"""Require tools used by maintained source regressions before CI runs them."""
from pathlib import Path
import shlex
import unittest

from test_ci_ai import load_workflow

ROOT = Path(__file__).resolve().parents[1]


class SourceCiPrerequisiteTests(unittest.TestCase):
    def test_userspace_provisions_iso_and_yaml_tools_before_regressions(self):
        steps = load_workflow()['jobs']['userspace']['steps']
        runner = next(i for i, step in enumerate(steps)
                      if step.get('run') == './scripts/test.sh')
        installed = set()
        for step in steps[:runner]:
            for line in step.get('run', '').splitlines():
                words = shlex.split(line)
                if words[:3] == ['sudo', 'apt-get', 'install']:
                    self.assertFalse(step.get('continue-on-error', False))
                    self.assertNotIn('if', step)
                    installed.update(word for word in words[3:] if not word.startswith('-'))
        self.assertIn('xorriso', installed,
                      'Live provenance tests create real ISO fixtures; provision xorriso explicitly')
        self.assertIn('python3-yaml', installed,
                      'The CI workflow regressions require an actual YAML parser')
        checks = [i for i, step in enumerate(steps)
                  if 'tests/test_ci_ai.py' in step.get('run', '')
                  and 'tests/test_ci_live.py' in step.get('run', '')]
        self.assertEqual(len(checks), 1, 'CI contract regressions must run once')
        self.assertLess(checks[0], runner)
        for step in steps[:runner]:
            if 'tests/test_ci_ai.py' in step.get('run', ''):
                self.assertFalse(step.get('continue-on-error', False))
                self.assertNotIn('if', step)


if __name__ == '__main__':
    unittest.main(verbosity=2)
