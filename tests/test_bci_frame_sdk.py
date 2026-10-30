"""Compile the real integer frame SDK at its OS syscall boundary."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
SDK = ROOT / "sdk/libxtos-bci"
EVIDENCE = ROOT / ".build/xtos-roadmap-v1/p2-sdk"


class FrameSDKTests(unittest.TestCase):
    def test_frame_sdk(self):
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        mode = "sanitized" if os.environ.get("BCI_SDK_SANITIZE") == "1" else "normal"
        binary = EVIDENCE / ("test_frame-" + mode)
        command = shlex.split(os.environ.get("CC", "cc")) + [
            "-std=c11", "-Wall", "-Wextra", "-Werror", "-g",
            "-I" + str(SDK / "include"),
        ]
        if mode == "sanitized":
            command += ["-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                        "-fno-omit-frame-pointer", "-no-pie"]
        command += [str(SDK / "tests/test_frame.c"), str(SDK / "src/session.c")]
        command += ["-Wl,--wrap=" + name for name in ["open", "close", "ioctl", "mmap", "munmap", "malloc", "calloc"]]
        command += ["-o", str(binary)]
        result = subprocess.run(command, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = subprocess.run([str(binary)], text=True, capture_output=True)
        (EVIDENCE / ("execution-" + mode + ".json")).write_text(json.dumps({
            "compile_command": command, "run_command": [str(binary)],
            "returncode": result.returncode, "stdout": result.stdout,
            "stderr": result.stderr,
        }, indent=2) + "\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"frame_sdk_cases=\d+ pass")
        print(result.stdout.strip())


if __name__ == "__main__":
    unittest.main()
