"""Executable adversarial tests including the actual OpenBCI driver C."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CASES = (
    "frame_counter_boundaries", "frame_raw_rails", "frame_cancel_start", "frame_cancel_arm", "frame_time_config_lifetime", "frame_enable_failure", "frame_legacy_independent",
    "frame_cancel_idle", "frame_cancel_rx", "frame_terminal_wake", "frame_sync_fault", "frame_publish_fault", "frame_stream_exhaustion", "frame_resync", "frame_gap_saturation", "frame_provenance",
    "probe", "start", "controls", "stats", "failed_start", "reopen",
    "concurrent_control", "cleanup_receive", "config_transaction", "gain_refresh",
    "conversion_fault", "atomic_gain", "enable_mismatch", "cancel_transaction", "rate_mismatch",
    "failed_command_invalidates", "missing_rows", "reserved_gain", "wrong_ack",
    "bias_asymmetry", "generation_overflow", "reply_timeout", "rate_preserves",
    "banner_min", "banner_max", "banner_sync", "banner_version", "banner_id_suffix",
    "banner_prefix", "banner_4ch", "banner_empty", "banner_long", "banner_daisy",
    "banner_nul", "banner_control", "banner_high", "banner_tab", "fault_stats_lock",
    "wait_signal", "wait_error", "wait_budget", "wait_expired", "wait_wrap", "wait_delayed", "wait_cancel",
    "control_admission_gain", "control_admission_rate", "control_admission_enable",
    "control_fault_gain", "control_fault_rate", "control_fault_enable",
)


class CytonDriverHostTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="cyton-driver-")
        cls.binary = Path(cls.directory.name) / "driver_test"
        command = [
            os.environ.get("CC", "cc"), "-std=gnu11", "-Wall", "-Wextra",
            "-Werror", "-Wno-unused-parameter", "-pthread",
            "-I" + str(ROOT / "tests/cyton_driver_host"),
            str(ROOT / "tests/cyton_driver_host/driver_test.c"),
            "-o", str(cls.binary),
        ]
        sanitizer = os.environ.get("BCI_SANITIZE", "")
        if sanitizer:
            command[1:1] = ["-fsanitize=" + sanitizer, "-fno-omit-frame-pointer", "-g", "-no-pie"]
        subprocess.run(command, check=True, capture_output=True, text=True)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_compiles_from_independent_source_export(self):
        # A relocated build must consume its own exported headers, even when
        # the original checkout still exists and could hide an absolute include.
        paths = (
            "kernel/linux/drivers/xtos/bci/devices/openbci_cyton.c",
            "kernel/linux/drivers/xtos/bci/xtos_cyton_packet.h",
            "kernel/linux/drivers/xtos/bci/xtos_cyton_config.h",
            "kernel/linux/include/uapi/linux/xtos_bci_config.h",
            "kernel/linux/include/uapi/linux/xtos_bci_frame.h",
            "tests/cyton_driver_host/driver_test.c",
            "tests/cyton_driver_host/shim.h",
        )
        with tempfile.TemporaryDirectory(prefix="cyton-export-") as tmp:
            export = Path(tmp)
            for relative in paths:
                target = export / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, target)
            binary = export / "driver"
            dependencies = export / "driver.d"
            command = [os.environ.get("CC", "cc"), "-std=gnu11", "-Wall", "-Wextra",
                       "-Werror", "-Wno-unused-parameter", "-pthread", "-MMD",
                       "-MF", str(dependencies),
                       "-I" + str(export / "tests/cyton_driver_host"),
                       str(export / "tests/cyton_driver_host/driver_test.c"),
                       "-o", str(binary)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            used = dependencies.read_text().replace("\\\n", " ").split()[1:]
            used_paths = {Path(name).resolve() for name in used}
            self.assertNotIn(ROOT / "kernel/linux/include/uapi/linux/xtos_bci_frame.h", used_paths)
            self.assertIn(export / "kernel/linux/include/uapi/linux/xtos_bci_frame.h", used_paths)
            result = subprocess.run([str(binary), "frame_provenance"],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.strip(), "PASS frame_provenance")


def _case(name):
    def test(self):
        result = subprocess.run(
            [str(self.binary), name], capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "PASS " + name)
    return test


for _name in CASES:
    setattr(CytonDriverHostTest, "test_" + _name, _case(_name))

if __name__ == "__main__":
    unittest.main()
