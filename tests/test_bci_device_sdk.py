"""Exercise the real SDK config query at its wrapped OS boundary."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SDK = ROOT / "sdk/libxtos-bci"
SCRATCH = Path(os.environ.get("TMPDIR", "/root/.hermes/cache/scratch"))


class ConfigSDKTests(unittest.TestCase):
    def test_real_session_config_query_and_validation(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="bci-config-sdk-", dir=SCRATCH) as tmp:
            binary = Path(tmp) / "test_config"
            command = shlex.split(os.environ.get("CC", "cc")) + [
                "-std=c11", "-Wall", "-Wextra", "-Werror", "-g",
                "-I" + str(SDK / "include"),
            ]
            if os.environ.get("BCI_SDK_SANITIZE") == "1":
                command += ["-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                            "-fno-omit-frame-pointer", "-no-pie"]
            command += [str(SDK / "tests/test_config.c"), str(SDK / "src/session.c")]
            command += ["-Wl,--wrap=" + name for name in ["open", "close", "ioctl", "read"]]
            command += ["-o", str(binary)]
            result = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([str(binary)], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("config_sdk_cases=25 pass", result.stdout)
            print(result.stdout.strip())

    def test_kernel_and_sdk_config_v1_layout_and_ioctl(self):
        body = r'''
#include <stddef.h>
#include <stdio.h>
int main(void) {
    printf("%zu %zu %zu %zu %zu %zu %lu\n",
           sizeof(struct xtos_bci_config_v1),
           offsetof(struct xtos_bci_config_v1, generation),
           offsetof(struct xtos_bci_config_v1, gain),
           offsetof(struct xtos_bci_config_v1, registers),
           offsetof(struct xtos_bci_config_v1, firmware),
           offsetof(struct xtos_bci_config_v1, reserved),
           (unsigned long)XTOS_BCI_GET_CONFIG);
    return 0;
}
'''
        headers = [ROOT / "kernel/linux/include/uapi/linux/xtos_bci_config.h",
                   SDK / "include/xtos/bci_config_uapi.h"]
        outputs = []
        SCRATCH.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="bci-config-layout-", dir=SCRATCH) as tmp:
            for index, header in enumerate(headers):
                source = Path(tmp) / f"layout-{index}.c"
                binary = Path(tmp) / f"layout-{index}"
                source.write_text('#include "' + str(header) + '"\n' + body)
                result = subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                                         str(source), "-o", str(binary)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                result = subprocess.run([str(binary)], text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                outputs.append(result.stdout.split())
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0][:6], ["384", "32", "40", "72", "96", "352"])
        self.assertEqual(int(outputs[0][6]) & 0xff, 16)


if __name__ == "__main__":
    unittest.main()
