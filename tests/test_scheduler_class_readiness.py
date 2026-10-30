import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SchedulerClassReadinessTests(unittest.TestCase):
    def setUp(self):
        self.rfc = (ROOT / "docs/xtos_scheduler_class_rfc.md").read_text(encoding="utf-8")
        self.matrix = (ROOT / "docs/xtos_scheduler_lifecycle_matrix.md").read_text(encoding="utf-8")
        self.design = (ROOT / "docs/xtos_scheduler_class_design.md").read_text(encoding="utf-8")
        self.readme = (ROOT / "README.md").read_text(encoding="utf-8")

    def test_rfc_is_explicitly_unapproved_and_covers_kernel_boundaries(self):
        self.assertIn("状态：RFC 候选，未批准实施", self.rfc)
        for phrase in (
            "不是已实现功能声明",
            "M0",
            "sched_class",
            "rq lock",
            "PI",
            "UAPI",
            "默认关闭",
            "不可卸载",
            "SCHED_DEADLINE",
            "回退",
        ):
            self.assertIn(phrase, self.rfc)

    def test_lifecycle_matrix_covers_each_required_lifecycle_and_verdict(self):
        self.assertIn("状态：审查矩阵，未进入 M2", self.matrix)
        self.assertIn("enqueue_task", self.matrix)
        self.assertIn("dequeue_task", self.matrix)
        required = (
            "fork/exec/exit", "wake/sleep", "yield",
            "pick_next_task", "task_tick", "update_curr", "prio_changed", "PI",
            "cgroup", "affinity/migration", "rq_offline", "NOHZ",
            "SMP balance", "core scheduling", "signal", "throttling",
            "recovery",            "证据", "负责人", "判定",
        )
        for phrase in required:
            self.assertIn(phrase, self.matrix)

    def test_design_keeps_helper_and_candidate_separate(self):
        self.assertIn("设计候选", self.design)
        self.assertIn("不是 `sched_class`", self.design)
        self.assertIn("M0", self.design)
        self.assertIn("M2", self.design)

    def test_public_readme_does_not_claim_custom_scheduler_is_complete(self):
        self.assertIn("可选 XTOS 调度辅助层", self.readme)
        self.assertIn("不是 Linux `sched_class`", self.readme)
        self.assertNotIn("自定义 XTOS 调度器（`CONFIG_XTOS_SCHED=y`）", self.readme)


if __name__ == "__main__":
    unittest.main()
