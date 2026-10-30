import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_DOCS = (
    'docs/scheduler_design.md',
    'docs/scheduler_implementation.md',
    'docs/scheduler_test_report.md',
)


class SchedulerDocumentationStatusTests(unittest.TestCase):
    def test_legacy_scheduler_docs_disclose_they_are_historical(self):
        for relative in HISTORICAL_DOCS:
            with self.subTest(path=relative):
                text = (ROOT / relative).read_text(encoding='utf-8')
                opening = '\n'.join(text.splitlines()[:12])
                self.assertRegex(opening, r'(?i)(historical|历史|过期|早期设计)')
                self.assertIn('sched_class', opening)
                self.assertIn('scheduler_integration_audit.md', opening)

    def test_recorded_kernel_baseline_matches_current_source(self):
        import hashlib
        import json

        baseline = json.loads((ROOT / 'docs/scheduler-class-baseline.json').read_text())
        self.assertFalse(baseline['helper_is_sched_class'])
        self.assertEqual(baseline['class_order'], ['stop', 'dl', 'rt', 'fair', 'idle'])
        for relative, evidence in baseline['files'].items():
            with self.subTest(path=relative):
                self.assertEqual(hashlib.sha256((ROOT / relative).read_bytes()).hexdigest(), evidence['sha256'])


    def test_new_design_states_helper_is_not_an_active_linux_scheduler_class(self):
        text = (ROOT / 'docs/xtos_scheduler_class_design.md').read_text(encoding='utf-8')
        self.assertIn('Linux 调度核心', text)
        self.assertIn('没有调用 XTOS `pick_next_task_xtos()`', text)
        self.assertIn('它不是 `sched_class`', text)

    def test_m0_closeout_links_explicitly_unfilled_inputs_and_real_gates(self):
        import json

        intake = json.loads((ROOT / 'docs/scheduler-workloads/m0-target-intake.json').read_text())
        self.assertEqual(intake['status'], 'needs_operator_input')
        self.assertIsNone(intake['target']['physical_platform_attestation'])
        self.assertEqual(intake['workload']['task_set'], [])
        self.assertEqual(intake['comparison_protocol']['valid_repetitions_per_policy_minimum'], 10)
        self.assertIsNone(intake['comparison_protocol']['primary_metric'])

        closeout = (ROOT / 'docs/scheduler-workloads/m0-closure.md').read_text(encoding='utf-8')
        self.assertIn('M0 产品/实机 go/no-go 尚未完成', closeout)
        self.assertIn('不把当前 KVM 数据称为 RT 性能基线', closeout)
        self.assertIn('M1 仅开展用户态参考工具开发，阶段收益门尚未通过', closeout)
        self.assertIn('M2 内核调度类保持未开始', closeout)
        self.assertIn('pre-closeout', closeout)
        self.assertIn('当前收尾候选', closeout)
        self.assertIn('当前收尾候选的完整维护结果以验收台账和 attempt 报告为准', closeout)
        self.assertIn('538 项 unittest', closeout)
        self.assertIn('hardware-observation-2026-10-10.json', closeout)
        self.assertIn('hardware-cyclictest-parent.json', closeout)
        self.assertIn('不是当前候选', closeout)

        model_doc = (ROOT / 'docs/scheduler-workloads/m1-model-v1.md').read_text(encoding='utf-8')
        model_input = json.loads((ROOT / 'docs/scheduler-workloads/m1-model-input-v1.json').read_text())
        self.assertIn('xtos-scheduler-model-input-v1', model_doc)
        self.assertIn('model_only', model_doc)
        self.assertEqual(model_input['schema'], 'xtos-scheduler-model-input-v1')
        self.assertFalse(model_input['production_workload_approved'])
        self.assertIn('tools/xtos-sched-model/replay.py', model_doc)
        self.assertIn('finite_diagnostic_only', model_doc)
        self.assertIn('reservation_edf', model_doc)
        self.assertIn('不是 Linux CBS', model_doc)

        workload_index = (ROOT / 'docs/scheduler-workloads/README.md').read_text(encoding='utf-8')
        plan = (ROOT / 'plans/xtos-scheduler-class-development-plan.md').read_text(encoding='utf-8')
        self.assertIn('m0-closure.md', workload_index)
        self.assertIn('m0-closure.md', plan)
        self.assertIn('m0-target-intake.json', plan)
        self.assertIn('pre-closeout', plan)
        self.assertIn('当前收尾候选', plan)
        self.assertIn('当前收尾候选的完整维护结果以验收台账和 attempt 报告为准', plan)
        self.assertIn('M1 用户态参考模型', plan)
        test_entry = (ROOT / 'scripts/test.sh').read_text(encoding='utf-8')
        self.assertIn("test_xtos_sched_model*.py", test_entry)

    def test_partitioned_tool_is_explicitly_logical_and_keeps_stage_gates(self):
        import json
        document=(ROOT/'docs/scheduler-workloads/partitioned-model-v1.md').read_text()
        fixture=json.loads((ROOT/'docs/scheduler-workloads/partitioned-model-input-v1.json').read_text())
        self.assertEqual(fixture['schema'],'xtos-scheduler-partitioned-input-v1')
        self.assertGreater(len({t['cpu'] for t in fixture['tasks']}),1)
        for required in ['model_only','finite_diagnostic_only','不是 Linux SMP 模拟器',
                         'M2 内核调度类未开始','所有 CPU 合计最多 4096 jobs',
                         '保守限制全局枚举量','100000 replay events']:
            self.assertIn(required,document)
        self.assertIn('partitioned.py',document)
        self.assertIn('独占创建前完成报告序列化和 UTF-8 编码',document)
        workload_index = (ROOT / 'docs/scheduler-workloads/README.md').read_text(encoding='utf-8')
        self.assertIn('partitioned-model-v1', workload_index)
        self.assertIn('device_contract.py', (ROOT / 'docs/eeg_device_validation_contract-v1.md').read_text(encoding='utf-8'))
        self.assertIn('cyton_capture.py', (ROOT / 'docs/eeg_user_pipeline.md').read_text(encoding='utf-8'))
        self.assertIn('cyton_replay.py', (ROOT / 'docs/eeg_user_pipeline.md').read_text(encoding='utf-8'))
        self.assertIn('PTY', (ROOT / 'docs/eeg_user_pipeline.md').read_text(encoding='utf-8'))
        self.assertIn('usr/share/xtos/eeg-pipeline', (ROOT / 'docs/eeg_user_pipeline.md').read_text(encoding='utf-8'))
        self.assertIn('staged rootfs', (ROOT / 'docs/eeg_user_pipeline.md').read_text(encoding='utf-8'))
        self.assertIn('test_cyton_capture.py', (ROOT / 'scripts/test.sh').read_text(encoding='utf-8'))
        self.assertIn('verify_cyton_runtime.py', (ROOT / 'docs/eeg_user_pipeline.md').read_text(encoding='utf-8'))
        self.assertIn('source-staged', (ROOT / 'docs/eeg_user_pipeline.md').read_text(encoding='utf-8'))
        self.assertIn('test_cyton_runtime.py', (ROOT / 'scripts/test.sh').read_text(encoding='utf-8'))
        self.assertIn('test_cyton_replay.py', (ROOT / 'scripts/test.sh').read_text(encoding='utf-8'))
        self.assertIn('test_openbci_cyton.py', (ROOT / 'tools/xtos-eeg-pipeline/Makefile').read_text(encoding='utf-8'))
        self.assertIn('OpenBCI Cyton 用户态串口适配切片', (ROOT / 'docs/scheduler-workloads/README.md').read_text(encoding='utf-8'))
        self.assertIn('PTY', (ROOT / 'README.md').read_text(encoding='utf-8'))
        self.assertIn('struct sched_class', (ROOT / 'docs/xtos_scheduler_lifecycle_matrix.md').read_text(encoding='utf-8'))

    def test_openbci_kernel_parser_fixture_is_wired_into_sanitizer_and_maintenance_tests(self):
        fuzz = (ROOT / 'tools/xtos-fuzz/fuzz_openbci_packet.c').read_text()
        make = (ROOT / 'tools/xtos-fuzz/Makefile').read_text()
        script = (ROOT / 'scripts/test.sh').read_text()
        self.assertIn('xtos_cyton_packet.h', fuzz)
        self.assertIn('drivers/xtos/bci/xtos_cyton_packet.h', make)
        self.assertIn('test_cyton_kernel_parser.sh', script)
        harness = ROOT / 'kernel/linux/drivers/xtos/bci/test_cyton_packet.c'
        self.assertTrue(harness.is_file())
        self.assertIn('test_counter_gap', harness.read_text())

    def test_gap_closure_plan_binds_local_and_external_stages(self):
        plan = (ROOT / 'plans/xtos-scheduler-gap-closure-plan.md').read_text(encoding='utf-8')
        self.assertIn('G3 本地用户态切片', plan)
        self.assertIn('G3 用户态真实设备准备', plan)
        self.assertIn('G4 实机 M0/M1', plan)
        self.assertIn('blocked_external_inputs', plan)
        self.assertIn('m0_gate.py', plan)
        self.assertIn('device_contract.py', plan)
        self.assertIn('OpenBCI Cyton 33-byte', plan)
        self.assertIn('G4–G6 保持开放', plan)
        evidence = (ROOT / 'docs/scheduler-workloads/m0-evidence-v1.md').read_text(encoding='utf-8')
        self.assertIn('ready_for_independent_review', evidence)
        self.assertIn('不认证操作人', evidence)
        self.assertIn('m0-evidence-v1', (ROOT / 'docs/scheduler-workloads/README.md').read_text(encoding='utf-8'))
        device = (ROOT / 'docs/eeg_device_validation_contract-v1.md').read_text(encoding='utf-8')
        self.assertIn('live_device_report_consistent', device)
        self.assertIn('固定为 false', device)

        text = (ROOT / 'docs/stage4a_realtime_hardware_validation.md').read_text(encoding='utf-8')
        opening = '\n'.join(text.splitlines()[:18])
        self.assertRegex(opening, r'(?i)(historical|历史)')
        self.assertIn('不是本计划 M0 调度基线的验收门槛', opening)


if __name__ == '__main__':
    unittest.main()
