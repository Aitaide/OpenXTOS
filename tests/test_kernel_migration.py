"""Current-kernel migration checks; runtime and hardware need separate gates."""
from pathlib import Path
import json
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


class KernelMigrationTests(unittest.TestCase):
    def test_live_gpu_config_preserves_rt_and_declares_supported_intel_driver(self):
        config = (ROOT/'kernel/xtos_live_x86.config').read_text().splitlines()
        self.assertIn('CONFIG_PREEMPT_RT=y', config)
        self.assertIn('CONFIG_DRM_XE=m', config)
        self.assertIn('CONFIG_DRM_AMDGPU=m', config)
        self.assertIn('CONFIG_DRM_NOUVEAU=m', config)
        i915 = (ROOT/'kernel/linux/drivers/gpu/drm/i915/Kconfig').read_text()
        self.assertIn('depends on !PREEMPT_RT', i915)

    def test_image_verifiers_use_shipped_rt_status(self):
        for name in ('verify_live.py', 'verify_live_gpu.py',
                     'verify_baremetal.py', 'verify_baremetal_ssh.py'):
            with self.subTest(verifier=name):
                text = (ROOT/'scripts'/name).read_text()
                self.assertIn('/usr/bin/xtos-rt-status', text)
                self.assertNotIn('cat /sys/kernel/realtime', text)

    def test_bundled_source_matches_selected_stable_version(self):
        pin = json.loads((ROOT / 'kernel/source-pin.json').read_bytes())
        self.assertEqual(pin['version'], '7.3.1')
        source = ROOT / 'kernel/linux'
        makefile = (source / 'Makefile').read_text()
        actual = '.'.join(re.search(r'^' + name + r' = (\d+)$', makefile, re.M).group(1)
                          for name in ('VERSION', 'PATCHLEVEL', 'SUBLEVEL'))
        self.assertEqual(actual, pin['version'])
        self.assertEqual(pin['rt_mode'], 'mainline')
        self.assertIsNone(pin['rt_patch'])
        self.assertIn('config PREEMPT_RT\n', (source / 'kernel/Kconfig.preempt').read_text())
        self.assertFalse((source / 'localversion-rt').exists())


if __name__ == '__main__':
    unittest.main()
