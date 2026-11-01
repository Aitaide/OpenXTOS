import re
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCHED_H = ROOT / "kernel/linux/kernel/sched/sched.h"
RFC = ROOT / "docs/xtos_scheduler_class_rfc.md"
MATRIX = ROOT / "docs/xtos_scheduler_lifecycle_matrix.md"


def callbacks_from_pinned_kernel():
    source = SCHED_H.read_text(encoding="utf-8")
    start = source.index("struct sched_class {")
    end = source.index("\n};", start)
    body = source[start:end]
    return set(re.findall(r"\(\*(\w+)\)\s*\(", body))


class SchedulerHookCoverageTests(unittest.TestCase):
    def test_rfc_and_lifecycle_matrix_cover_every_pinned_sched_class_callback(self):
        callbacks = callbacks_from_pinned_kernel()
        rfc = RFC.read_text(encoding="utf-8")
        matrix = MATRIX.read_text(encoding="utf-8")
        missing = sorted(name for name in callbacks
                         if f"`{name}`" not in rfc or f"`{name}`" not in matrix)
        self.assertFalse(missing, f"sched_class callback hooks omitted from RFC/matrix: {missing}")

    def test_smp_and_optional_callbacks_are_marked_with_their_config_guards(self):
        matrix = MATRIX.read_text(encoding="utf-8")
        for callback, guard in (
            ("balance", "always"), ("select_task_rq", "always"),
            ("pick_task", "always"), ("migrate_task_rq", "always"),
            ("task_change_group", "CONFIG_FAIR_GROUP_SCHED"),
            ("task_is_throttled", "CONFIG_SCHED_CORE"),
        ):
            with self.subTest(callback=callback):
                line = next((line for line in matrix.splitlines() if f"`{callback}`" in line), "")
                self.assertIn(guard, line)

    def test_hook_coverage_is_bound_to_kernel_file(self):
        matrix = MATRIX.read_text(encoding="utf-8")
        self.assertIn("kernel/linux/kernel/sched/sched.h", matrix)
        self.assertIn("struct sched_class", matrix)
        self.assertIn("覆盖基线", matrix)


if __name__ == "__main__":
    unittest.main()
