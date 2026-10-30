"""Kernel UAPI and exported SDK must have identical versioned snapshot layout."""
from pathlib import Path
import subprocess
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
class BciUapiTests(unittest.TestCase):
    def test_snapshot_kernel_and_sdk_layout_and_ioctl_match(self):
        body='''
#include <stddef.h>
#include <stdio.h>
int main(void) {
 printf("%zu %zu %zu %zu %zu %zu %lu %lu %lu\\n",
  sizeof(struct xtos_bci_snapshot_request),sizeof(struct xtos_bci_snapshot_header),
  sizeof(struct xtos_bci_snapshot_consume),sizeof(struct xtos_bci_snapshot_release),
  offsetof(struct xtos_bci_snapshot_request,token),offsetof(struct xtos_bci_snapshot_header,payload_bytes),
  (unsigned long)XTOS_BCI_SNAPSHOT_ACQUIRE,(unsigned long)XTOS_BCI_SNAPSHOT_CONSUME,
  (unsigned long)XTOS_BCI_SNAPSHOT_RELEASE); return 0;
}
'''
        results=[]
        with tempfile.TemporaryDirectory() as tmp:
            for name,header in [('kernel','kernel/linux/include/uapi/linux/xtos_bci_snapshot.h'),
                                ('sdk','sdk/libxtos-bci/include/xtos/bci_snapshot_uapi.h')]:
                source=Path(tmp)/(name+'.c');binary=Path(tmp)/name
                source.write_text('#include "'+str(ROOT/header)+'"\n'+body)
                subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror',str(source),'-o',str(binary)],check=True,capture_output=True)
                results.append(subprocess.check_output([str(binary)],text=True).strip())
        self.assertEqual(results[0],results[1])
        self.assertEqual(results[0].split()[:6],['72','64','48','24','40','16'])
if __name__=='__main__':unittest.main()
