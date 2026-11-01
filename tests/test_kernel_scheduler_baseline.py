"""Current scheduler source contract; no build or runtime acceptance."""
import hashlib
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HISTORY_SHA256 = "91a91027c4f4b2782c90afe49e86cdbdddf1d3da29d7d1f235158982759c0cde"


def current_callbacks():
    text = (ROOT / 'kernel/linux/kernel/sched/sched.h').read_text()
    body = text.split('struct sched_class {', 1)[1].split('\n};', 1)[0]
    guards = []
    callbacks = {}
    for line in body.splitlines():
        if line.startswith('#ifdef '):
            guards.append(line.split()[1])
        elif line.startswith('#endif'):
            guards.pop()
        match = re.search(r'\(\*(\w+)\)\s*\(', line)
        if match:
            callbacks[match[1]] = ' && '.join(guards) or 'always'
    return callbacks


class KernelSchedulerBaselineTests(unittest.TestCase):
    def test_history_is_preserved_as_exact_original_bytes(self):
        history = ROOT / 'docs/history/scheduler-class-baseline-6.6.0-rt15.json'
        self.assertEqual(hashlib.sha256(history.read_bytes()).hexdigest(), HISTORY_SHA256)
        self.assertEqual(json.loads(history.read_bytes())['bundled_kernel_source'], '6.6.0-rt15')

    def test_current_baseline_records_actual_version_callbacks_and_guards(self):
        baseline = json.loads((ROOT / 'docs/scheduler-class-baseline.json').read_text())
        pin = json.loads((ROOT / 'kernel/source-pin.json').read_text())
        makefile = (ROOT / 'kernel/linux/Makefile').read_text()
        version_fields = dict(re.findall(r'^(VERSION|PATCHLEVEL|SUBLEVEL) = (\d+)$', makefile, re.M))
        version = '.'.join(version_fields[name] for name in ('VERSION', 'PATCHLEVEL', 'SUBLEVEL'))
        self.assertEqual(baseline['bundled_kernel_source'], version)
        self.assertEqual(version, pin['version'])
        self.assertEqual(baseline['inspection_scope'], 'source_inspection_only')
        self.assertEqual(baseline['callbacks'], current_callbacks())
        self.assertEqual(baseline['rt_mode'], 'mainline')
        self.assertIsNone(baseline['rt_patch'])
        self.assertNotIn('kernel/linux/localversion-rt', baseline['version_evidence'])
        self.assertNotIn('kernel/patches/preempt-rt-6.6-rt15.patch', baseline['files'])
        self.assertEqual(baseline['verification_gates']['custom_class'], 'NOT_IMPLEMENTED')
        self.assertEqual(baseline['verification_gates']['kernel_rebuilt_this_attempt'], 'NOT_RUN')
        self.assertEqual(baseline['verification_gates']['exact_image_boot_this_attempt'], 'NOT_RUN')
        self.assertEqual(baseline['verification_gates']['hardware_latency'], 'NOT_VERIFIED')
        helper_config = (ROOT / 'kernel/linux/kernel/sched/xtos/Kconfig').read_text()
        self.assertRegex(helper_config, r'(?s)config XTOS_SCHED\s+tristate .*?\n\s*default n\b')
        self.assertEqual(baseline['config_defaults']['XTOS_SCHED'], 'tristate default n')
        self.assertIn('config PREEMPT_RT', (ROOT / 'kernel/linux/kernel/Kconfig.preempt').read_text())
        core = (ROOT / 'kernel/linux/kernel/sched/core.c').read_text()
        self.assertNotIn('pick_next_task_xtos(', core)
        self.assertFalse(baseline['helper_is_sched_class'])

    def test_documented_callback_rows_match_actual_declarations_exactly(self):
        callbacks = current_callbacks()
        matrix = (ROOT / 'docs/xtos_scheduler_lifecycle_matrix.md').read_text()
        rows = {}
        for line in matrix.splitlines():
            match = re.match(r'\| `([a-z_]+)` \| (.*?) \|', line)
            if match:
                self.assertNotIn(match[1], rows)
                rows[match[1]] = match[2].replace('`', '')
        self.assertEqual(rows, callbacks)
        rfc = (ROOT / 'docs/xtos_scheduler_class_rfc.md').read_text()
        section = rfc.split('当前 callback 共 ', 1)[1].split('上述清单', 1)[0]
        self.assertEqual(int(section.split(' 项', 1)[0]), len(callbacks))
        self.assertEqual(set(re.findall(r'`([a-z_]+)`', section)), set(callbacks))

    def test_current_docs_keep_physical_go_nogo_and_source_scope(self):
        for relative in ('docs/xtos_scheduler_lifecycle_matrix.md',
                         'docs/xtos_scheduler_class_rfc.md',
                         'docs/xtos_scheduler_class_design.md'):
            with self.subTest(path=relative):
                text = (ROOT / relative).read_text()
                self.assertIn('7.3.1', text)
                self.assertIn('source_inspection_only', text)
                self.assertIn('M0', text)
                self.assertIn('M1', text)
                self.assertIn('M2', text)
                self.assertIn('go/no-go', text)
                self.assertIn('ext', text)


if __name__ == '__main__':
    unittest.main()
