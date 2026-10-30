"""Compile independent kernel/SDK frame ABI contracts, native and 32-bit."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]

CHECKS=r'''
#define OFFSET(t,f,n) _Static_assert(__builtin_offsetof(struct t,f)==n,#t "." #f)
#define SIZE(t,n) _Static_assert(sizeof(struct t)==n,#t " size")
SIZE(xtos_bci_frame_v1,224);
_Static_assert(__alignof__(struct xtos_bci_frame_v1)==8,"frame alignment");
OFFSET(xtos_bci_frame_v1,version,0);
OFFSET(xtos_bci_frame_v1,host_rx_ns,32);
OFFSET(xtos_bci_frame_v1,sequence,40);
OFFSET(xtos_bci_frame_v1,stream_generation,48);
OFFSET(xtos_bci_frame_v1,config_generation,56);
OFFSET(xtos_bci_frame_v1,device_counter,64);
OFFSET(xtos_bci_frame_v1,missing_before,72);
OFFSET(xtos_bci_frame_v1,reserved,80);
OFFSET(xtos_bci_frame_v1,samples_nv,96);
SIZE(xtos_bci_frame_request,512);
_Static_assert(__alignof__(struct xtos_bci_frame_request)==8,"request alignment");
OFFSET(xtos_bci_frame_request,frames_ptr,16);
OFFSET(xtos_bci_frame_request,cursor,24);
OFFSET(xtos_bci_frame_request,next_cursor,32);
OFFSET(xtos_bci_frame_request,overwritten_frames,40);
OFFSET(xtos_bci_frame_request,token,48);
OFFSET(xtos_bci_frame_request,stream_generation,56);
OFFSET(xtos_bci_frame_request,record_bytes,64);
OFFSET(xtos_bci_frame_request,header_bytes,68);
OFFSET(xtos_bci_frame_request,mapping_bytes,72);
OFFSET(xtos_bci_frame_request,payload_bytes,80);
OFFSET(xtos_bci_frame_request,reserved,88);
OFFSET(xtos_bci_frame_request,config,128);
SIZE(xtos_bci_frame_snapshot_header,128);
_Static_assert(__alignof__(struct xtos_bci_frame_snapshot_header)==8,"header alignment");
OFFSET(xtos_bci_frame_snapshot_header,payload_bytes,32);
OFFSET(xtos_bci_frame_snapshot_header,cursor,40);
OFFSET(xtos_bci_frame_snapshot_header,next_cursor,48);
OFFSET(xtos_bci_frame_snapshot_header,token,56);
OFFSET(xtos_bci_frame_snapshot_header,stream_generation,64);
OFFSET(xtos_bci_frame_snapshot_header,config_generation,72);
OFFSET(xtos_bci_frame_snapshot_header,overwritten_frames,80);
OFFSET(xtos_bci_frame_snapshot_header,mapping_bytes,88);
OFFSET(xtos_bci_frame_snapshot_header,config_bytes,96);
OFFSET(xtos_bci_frame_snapshot_header,records_offset,100);
OFFSET(xtos_bci_frame_snapshot_header,reserved,104);
SIZE(xtos_bci_frame_consume,48);
SIZE(xtos_bci_frame_release,24);
SIZE(xtos_bci_config_v1,384);
_Static_assert(XTOS_BCI_FRAME_READ==0xc2005811UL,"frame read ioctl");
_Static_assert(XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE==0xc2005812UL,"frame acquire ioctl");
_Static_assert(XTOS_BCI_FRAME_SNAPSHOT_CONSUME==0x40305813UL,"frame consume ioctl");
_Static_assert(XTOS_BCI_FRAME_SNAPSHOT_RELEASE==0x40185814UL,"frame release ioctl");
_Static_assert(XTOS_BCI_GET_CONFIG==0xc1805810UL,"old config ioctl unchanged");
_Static_assert(XTOS_BCI_FRAME_VERSION==1,"version");
_Static_assert(XTOS_BCI_FRAME_RECORD_BYTES==224,"record byte constant");
_Static_assert(XTOS_BCI_FRAME_HEADER_BYTES==128,"header byte constant");
_Static_assert(XTOS_BCI_FRAME_CONFIG_BYTES==384,"config byte constant");
_Static_assert(XTOS_BCI_FRAME_RECORDS_OFFSET==512,"records offset constant");
_Static_assert(XTOS_BCI_FRAME_MAX_PAYLOAD==65536,"maximum payload");
_Static_assert(XTOS_BCI_FRAME_MAX_BATCH==65536/224,"bounded batch");
_Static_assert(XTOS_BCI_FRAME_CLOCK_MONOTONIC==1,"host clock");
_Static_assert(XTOS_BCI_FRAME_SOURCE_SERDEV==1 && XTOS_BCI_FRAME_SOURCE_SYNTHETIC==2,"source flags");
_Static_assert(XTOS_BCI_FRAME_QUALITY_KNOWN==31,"quality known flags");
_Static_assert(XTOS_BCI_FRAME_NONBLOCK==1,"nonblock flag");
int frame_layout_contract(void) { return 0; }
'''


class FrameAbiTests(unittest.TestCase):
    def test_kernel_sdk_native_and_32bit_layout_ioctl_contract(self):
        headers=['kernel/linux/include/uapi/linux/xtos_bci_frame.h',
                 'sdk/libxtos-bci/include/xtos/bci_frame_uapi.h']
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            base=Path(tmp);shim=base/'shim';(shim/'linux').mkdir(parents=True);(shim/'sys').mkdir()
            (shim/'stdint.h').write_text('typedef unsigned int uint32_t; typedef unsigned long long uint64_t; typedef unsigned char uint8_t; typedef int int32_t;\n')
            (shim/'linux/types.h').write_text('typedef unsigned int __u32; typedef int __s32; typedef unsigned char __u8;\n#define __aligned_u64 unsigned long long __attribute__((aligned(8)))\n')
            ioctl=('#define _IOWR(t,n,s) ((3UL<<30)|((unsigned long)sizeof(s)<<16)|((unsigned long)(t)<<8)|(n))\n'
                   '#define _IOW(t,n,s) ((1UL<<30)|((unsigned long)sizeof(s)<<16)|((unsigned long)(t)<<8)|(n))\n')
            (shim/'linux/ioctl.h').write_text(ioctl);(shim/'sys/ioctl.h').write_text(ioctl)
            for bits in (64,32):
                for index,header in enumerate(headers):
                    with self.subTest(bits=bits,header=header):
                        source=base/f'frame-{bits}-{index}.c'
                        source.write_text('#include "'+str(ROOT/header)+'"\n'+CHECKS)
                        command=['cc',f'-m{bits}','-std=c11','-Wall','-Wextra','-Werror']
                        if bits==32:command+=['-ffreestanding','-nostdinc','-I'+str(shim)]
                        result=subprocess.run([*command,'-c',str(source),'-o',str(source.with_suffix('.o'))],capture_output=True,text=True)
                        self.assertEqual(result.returncode,0,result.stderr)


if __name__=='__main__':unittest.main()
