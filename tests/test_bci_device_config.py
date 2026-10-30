"""Versioned device readback contract; hardware acceptance is separate."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DeviceConfigTests(unittest.TestCase):
    def test_kernel_sdk_layout_and_ioctl_match(self):
        body = r'''
#include <stddef.h>
#include <stdio.h>
int main(void) {
    printf("%zu %zu %zu %zu %lu\n", sizeof(struct xtos_bci_config_v1),
           offsetof(struct xtos_bci_config_v1, generation),
           offsetof(struct xtos_bci_config_v1, gain),
           offsetof(struct xtos_bci_config_v1, firmware),
           (unsigned long)XTOS_BCI_GET_CONFIG);
    return 0;
}
'''
        headers = ["kernel/linux/include/uapi/linux/xtos_bci_config.h",
                   "sdk/libxtos-bci/include/xtos/bci_config_uapi.h"]
        outputs = []
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            for index, header in enumerate(headers):
                source = Path(tmp) / f"layout-{index}.c"
                binary = Path(tmp) / f"layout-{index}"
                source.write_text('#include "' + str(ROOT / header) + '"\n' + body)
                result = subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                                         str(source), "-o", str(binary)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                outputs.append(subprocess.check_output([str(binary)], text=True).split())
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0][:4], ["384", "32", "40", "96"])

    def test_32bit_kernel_sdk_layout_compile(self):
        # Freestanding declarations avoid requiring a 32-bit libc runtime.
        # Compile-time layout/ioctl equality does not claim compat_ioctl execution.
        body = r'''
_Static_assert(sizeof(struct xtos_bci_config_v1) == 384, "config size");
_Static_assert(__builtin_offsetof(struct xtos_bci_config_v1,generation) == 32, "generation");
_Static_assert(__builtin_offsetof(struct xtos_bci_config_v1,gain) == 40, "gain");
_Static_assert(__builtin_offsetof(struct xtos_bci_config_v1,registers) == 72, "registers");
_Static_assert(__builtin_offsetof(struct xtos_bci_config_v1,firmware) == 96, "firmware");
_Static_assert(__builtin_offsetof(struct xtos_bci_config_v1,reserved) == 352, "reserved");
_Static_assert(XTOS_BCI_GET_CONFIG == 0xc1805810UL, "config ioctl");
int layout_contract(void) { return 0; }
'''
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            base = Path(tmp)
            (base/'linux').mkdir()
            (base/'sys').mkdir()
            (base/'stdint.h').write_text('typedef unsigned int uint32_t; typedef unsigned long long uint64_t; typedef unsigned char uint8_t;\n')
            (base/'linux/types.h').write_text('typedef unsigned int __u32; typedef unsigned char __u8;\n#define __aligned_u64 unsigned long long __attribute__((aligned(8)))\n')
            ioctl='#define _IOWR(t,n,s) ((3UL<<30)|((unsigned long)sizeof(s)<<16)|((unsigned long)(t)<<8)|(n))\n'
            (base/'linux/ioctl.h').write_text(ioctl)
            (base/'sys/ioctl.h').write_text(ioctl)
            for index, header in enumerate(("kernel/linux/include/uapi/linux/xtos_bci_config.h",
                                             "sdk/libxtos-bci/include/xtos/bci_config_uapi.h")):
                source=base/f'layout32-{index}.c'
                source.write_text('#include "'+str(ROOT/header)+'"\n'+body)
                result=subprocess.run(['cc','-m32','-std=c11','-Wall','-Wextra','-Werror',
                    '-ffreestanding','-nostdinc','-I'+str(base),'-c',str(source),
                    '-o',str(base/f'layout32-{index}.o')],capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)


if __name__ == "__main__":
    unittest.main()
