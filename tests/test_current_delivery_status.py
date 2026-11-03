"""Current delivery docs must separate the accepted image from new work."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
ACCEPTED_COMMIT = '32a8bd3903eda24bd44d7e1a19dea4156014d062'
ISO_SHA256 = '96df3627bf79364192d502453904f0d78f112d3ba83a934bc28c0084a3b0c53f'


class CurrentDeliveryStatusTests(unittest.TestCase):
    def test_status_records_the_exact_accepted_kernel_slice(self):
        text = (ROOT / 'STATUS.md').read_text()
        self.assertIn('## Linux 7.3.1 历史已验收快照', text)
        for required in (ACCEPTED_COMMIT, ISO_SHA256,
                         '133 组、1656 次', 'candidate-62',
                         'ACCEPTED_LOCAL_KERNEL_UPGRADE_AND_QEMU_LIVE_IMAGE'):
            self.assertIn(required, text)

    def test_migration_does_not_leave_completed_gates_in_progress(self):
        text = (ROOT / 'docs/kernel-7.3.1-migration.md').read_text()
        self.assertIn('状态：内核升级与本地 Live 镜像切片已验收', text)
        self.assertNotIn('状态：in_progress。', text)
        for required in (ACCEPTED_COMMIT, ISO_SHA256, '干净提交',
                         '四模式', '物理', '不继承'):
            self.assertIn(required, text)

    def test_new_work_cannot_borrow_the_previous_acceptance(self):
        for relative in ('STATUS.md', 'docs/kernel-7.3.1-migration.md'):
            with self.subTest(path=relative):
                text = (ROOT / relative).read_text()
                self.assertIn('后续新增源码不继承该 PASS', text)
                self.assertIn('XR', text)
                self.assertIn('厂商', text)
                self.assertIn('sched_class', text)


if __name__ == '__main__':
    unittest.main()
