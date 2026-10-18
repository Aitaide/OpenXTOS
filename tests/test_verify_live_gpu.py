#!/usr/bin/env python3
"""The GPU verifier is opt-in and refuses existing evidence directories."""
import subprocess
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/verify_live_gpu.py'


class GpuVerifierTests(unittest.TestCase):
    def load_verifier(self):
        sys.path.insert(0, str(ROOT/'scripts'))
        self.addCleanup(sys.path.remove, str(ROOT/'scripts'))
        spec=importlib.util.spec_from_file_location('gpu_verifier',SCRIPT)
        assert spec and spec.loader
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module

    def test_tune_validation_rejects_all_failed_or_skipped_irqs(self):
        module=self.load_verifier()
        self.assertTrue(hasattr(module, 'validate_tune_output'))
        for text in [
            'RT_TUNE housekeeping_cpus=0 verified=0 changes=1 skipped=5 failed=0\nIRQ_TUNE_RC=0',
            'RT_TUNE housekeeping_cpus=0 verified=0 changes=1 skipped=0 failed=5\nIRQ_TUNE_RC=1',
        ]:
            with self.subTest(text=text), self.assertRaises(RuntimeError):
                module.validate_tune_output(text)

    def test_tune_validation_accepts_partial_migration_only_when_readback_agrees(self):
        module=self.load_verifier()
        self.assertTrue(hasattr(module, 'validate_tune_output'))
        text=('RT_TUNE irq 31 requested="0" effective="0"\n'
              'RT_TUNE irq 32 failed: requested="0-3" effective="3"\n'
              'RT_TUNE housekeeping_cpus=0 verified=1 changes=1 skipped=0 failed=1\nIRQ_TUNE_RC=1')
        result=module.validate_tune_output(text)
        self.assertEqual(result,{'verified':1,'failed':1,'skipped':0,'partial':True})
        with self.assertRaises(RuntimeError):
            module.validate_tune_output(text.replace('IRQ_TUNE_RC=1','IRQ_TUNE_RC=0'))

    def test_gpu_irq_readback_requires_migration_of_actual_gpu_interrupts(self):
        module=self.load_verifier()
        self.assertTrue(hasattr(module,'validate_gpu_irq_readback'))
        before=[{'node':'/dev/dri/card0','irqs':[
            {'irq':24,'requested_affinity':'0-3','effective_affinity':'2'}]}]
        unchanged=before
        # An unrelated NIC interrupt moving is not proof that GPU IRQs moved.
        with self.assertRaises(RuntimeError):
            module.validate_gpu_irq_readback(before,unchanged,
                'RT_TUNE irq 31 requested="0" effective="0"')
        after=[{'node':'/dev/dri/card0','irqs':[
            {'irq':24,'requested_affinity':'0','effective_affinity':'0'}]}]
        result=module.validate_gpu_irq_readback(before,after,
                'RT_TUNE irq 24 requested="0" effective="0"')
        self.assertEqual(result,{'verified_irqs':[24],'unmoved_irqs':[],'unavailable_irqs':[]})

    def test_gpu_irq_readback_retains_explicit_partial_failures(self):
        module=self.load_verifier()
        self.assertTrue(hasattr(module,'validate_gpu_irq_readback'))
        before=[{'node':'/dev/dri/card0','irqs':[
            {'irq':24,'requested_affinity':'0-3','effective_affinity':'2'},
            {'irq':25,'requested_affinity':'0-3','effective_affinity':'3'}]}]
        after=[{'node':'/dev/dri/card0','irqs':[
            {'irq':24,'requested_affinity':'0','effective_affinity':'0'},
            {'irq':25,'requested_affinity':'0-3','effective_affinity':'3'}]}]
        text=('RT_TUNE irq 24 requested="0" effective="0"\n'
              'RT_TUNE irq 25 failed: requested="0-3" effective="3"')
        result=module.validate_gpu_irq_readback(before,after,text)
        self.assertEqual(result['verified_irqs'],[24])
        self.assertEqual(result['unmoved_irqs'],[25])
        with self.assertRaises(RuntimeError):
            module.validate_gpu_irq_readback(before,after,text.splitlines()[0])

    def test_unregistered_legacy_irq_is_explicitly_unavailable(self):
        module=self.load_verifier()
        before=[{'irqs':[
            {'irq':23,'requested_affinity':'unknown','effective_affinity':'unknown'},
            {'irq':26,'requested_affinity':'0-3','effective_affinity':'0'}]}]
        after=[{'irqs':[
            {'irq':23,'requested_affinity':'unknown','effective_affinity':'unknown'},
            {'irq':26,'requested_affinity':'0','effective_affinity':'0'}]}]
        result=module.validate_gpu_irq_readback(before,after,
            'RT_TUNE irq 26 requested="0" effective="0"')
        self.assertEqual(result['verified_irqs'],[26])
        self.assertEqual(result['unavailable_irqs'],[23])
        after[0]['irqs'][1]['effective_affinity']='unknown'
        with self.assertRaises(RuntimeError):
            module.validate_gpu_irq_readback(before,after,
                'RT_TUNE irq 26 skipped: affinity readback unavailable')

    def test_rt_budget_is_read_back_independently_of_tune_output(self):
        module=self.load_verifier()
        self.assertTrue(hasattr(module,'validate_rt_budget_readback'))
        module.validate_rt_budget_readback({'rt_runtime_us':-1})
        for state in [{},{'rt_runtime_us':500000},{'rt_runtime_us':None},{'rt_runtime_us':-2}]:
            with self.subTest(state=state), self.assertRaises(RuntimeError):
                module.validate_rt_budget_readback(state)

    def test_help_identifies_candidate_key_and_evidence_inputs(self):
        result = subprocess.run([sys.executable, str(SCRIPT), '--help'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for flag in ('--candidate', '--key', '--evidence'):
            self.assertIn(flag, result.stdout)

    def test_existing_evidence_is_refused_without_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            evidence = Path(temp) / 'evidence'
            evidence.mkdir()
            sentinel = evidence / 'keep'
            sentinel.write_text('unchanged')
            result = subprocess.run([
                sys.executable, str(SCRIPT), '--candidate', temp,
                '--key', str(Path(temp) / 'key'), '--evidence', str(evidence),
            ], capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('fresh evidence', result.stderr)
            self.assertEqual(sentinel.read_text(), 'unchanged')
            self.assertEqual(sorted(p.name for p in evidence.iterdir()), ['keep'])


if __name__ == '__main__':
    unittest.main()
