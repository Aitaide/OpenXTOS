"""Standalone diagnostic PTY tests, pinned official firmware format.

Protocol emulator derived from OpenBCI_Cyton_Library commit
24e1c4269b60c3bec6d831bca03cae42f267b3c6, printAllRegisters/RREGS,
processIncomingChannelSettings/processIncomingLeadOffSettings. Synthetic,
nominal 250 Hz data does not establish physical accuracy or device safety.
"""
import errno
import json
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import threading
import time
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
EVIDENCE = ROOT / '.build/xtos-roadmap-v1/diagnostic-transport'
NAMES = ['ADS_ID', 'CONFIG1', 'CONFIG2', 'CONFIG3', 'LOFF'] + [f'CH{i}SET' for i in range(1,9)] + ['BIAS_SENSP','BIAS_SENSN','LOFF_SENSP','LOFF_SENSN','LOFF_FLIP','LOFF_STATP','LOFF_STATN','GPIO','MISC1','MISC2','CONFIG4']
DEFAULT = [0x3e,0x96,0xc0,0x60,2,0x68,0x18,0x28,0x38,0x48,0x58,0x60,0xe8,0x55,0x55,0x04,0x80,0,0,0,0x0f,0x20,0,0]

def dump(regs):
    return (b'\r\nBoard ADS Registers\r\n' + b''.join((f'{name}, {i:02X}, {v:02X}, ' + ', '.join(str((v>>b)&1) for b in range(7,-1,-1)) + '\r\n').encode() for i,(name,v) in enumerate(zip(NAMES,regs))) + b'\r\nLIS3DH Registers\r\n$$$')

def packet(counter, value):
    return b'\xa0' + bytes([counter%256]) + (value & 0xffffff).to_bytes(3,'big') * 8 + b'\0'*6 + b'\xc0'

class Board:
    def __init__(self, regs=None, **kw):
        self.regs = list(DEFAULT if regs is None else regs)
        self.original = self.regs.copy()
        self.kw = kw
        self.master,self.slave = pty.openpty()
        self.path = os.ttyname(self.slave)
        self.commands=[]; self.errors=[]; self.done=threading.Event()
        self.started=threading.Event(); self.proc=None
        self.thread=threading.Thread(target=self.serve); self.thread.start()
    def send(self,data):
        chunks=[data[i:i+13] for i in range(0,len(data),13)] if self.kw.get('split') else [data]
        for chunk in chunks:
            if chunk: os.write(self.master,chunk)
    def command(self,c):
        self.commands.append(c)
        stage=self.kw.get('cancel')
        if stage and self.proc and ((stage=='version' and c==b'V') or (stage=='disable' and c==b'z100Z') or (stage=='channel' and c.startswith(b'x')) or (stage=='excite' and c in (b'z110Z',b'z101Z')) or (stage=='stream' and c==b'b')):
            self.kw.pop('cancel'); os.kill(self.proc.pid,signal.SIGINT)
        if c==b'V': self.send(self.kw.get('version',b'v3.1.5$$$'))
        elif c==b'?':
            data=dump(self.regs)
            if self.kw.get('malformed'): data=self.kw['malformed'](data)
            if self.kw.get('restore_mismatch') and self.started.is_set():
                data=data.replace(b'CONFIG2, 02, C0, 1, 1, 0, 0, 0, 0, 0, 0',b'CONFIG2, 02, C1, 1, 1, 0, 0, 0, 0, 0, 1')
            self.send(data)
        elif c.startswith(b'z'):
            ch=c[1]-49; bit=1<<ch
            if not self.kw.get('no_z_effect'):
                for a,v in zip((15,16),c[2:4]): self.regs[a]=(self.regs[a]&~bit) | (bit if v==49 else 0)
            self.send(b'Success: Lead off set for '+c[1:2]+b'$$$')
        elif c.startswith(b'x'):
            ch=c[1]-49; bit=1<<ch
            if not self.kw.get('no_x_effect') and not (self.kw.get('restore_x_no_effect') and self.started.is_set()):
                self.regs[5+ch]=((c[2]-48)<<7)|((c[3]-48)<<4)|((c[6]-48)<<3)|(c[4]-48)
                for a in (13,14): self.regs[a]=(self.regs[a]&~bit)|(bit if c[5]==49 else 0)
                self.regs[21]=0x20 if c[7]==49 else 0
            ack=b'Success: Channel set for '+c[1:2]+b'$$$'
            self.send(b'Wrong$$$' if self.kw.get('bad_ack') else ack)
        elif c==b'b':
            self.started.set()
            if self.kw.get('loss'):
                os.close(self.master); self.master=-1; self.done.set(); return
            if self.kw.get('silent'): return
            frames=[]
            for i in range(self.kw.get('frame_count',1100)):
                value=self.kw.get('value',1000) if i%2 else -self.kw.get('value',1000)
                if self.kw.get('rail') and i==5: value=8388607
                p=packet(i+(1 if self.kw.get('gap') and i>=5 else 0),value)
                if self.kw.get('bad_frame') and i==5: p=p[:-1]+b'\x00'
                frames.append(p)
            # Nominal-vector bursts are accelerated host delivery, not 250 Hz wall-time.
            self.send(b''.join(frames))
    def serve(self):
        buf=bytearray(); began=0
        try:
            while not self.done.is_set():
                if buf and time.monotonic()-began>1.0:
                    buf.clear(); self.send(b'Timeout processing multi byte message - please send all commands at once as of v2$$$')
                if not select.select([self.master],[],[],.01)[0]: continue
                data=os.read(self.master,4096)
                for b in data:
                    if buf:
                        buf.append(b)
                        if len(buf)==(9 if buf[0]==120 else 5): self.command(bytes(buf)); buf.clear()
                    elif b in (120,122): buf.append(b); began=time.monotonic()
                    else: self.command(bytes([b]))
        except OSError as exc:
            if not self.done.is_set() and exc.errno not in (errno.EIO,errno.EBADF): self.errors.append(repr(exc))
    def close(self):
        self.done.set(); self.thread.join(2)
        assert not self.thread.is_alive()
        for fd in (self.master,self.slave):
            if fd>=0:
                try: os.close(fd)
                except OSError: pass
        assert not self.errors,self.errors

class DiagnosticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        EVIDENCE.mkdir(parents=True,exist_ok=True)
        cls.exe=EVIDENCE/('fixture-san' if os.environ.get('DIAGNOSTIC_SAN') else 'fixture')
        flags=['-fsanitize=address,undefined','-fno-sanitize-recover=all','-fno-omit-frame-pointer','-no-pie'] if os.environ.get('DIAGNOSTIC_SAN') else []
        p=subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-pedantic','-O1','-g',*flags,str(HERE/'diagnostic_serial_fixture.c'),str(HERE/'cyton_diagnostic_serial.c'),'-Wl,--wrap=write','-o',str(cls.exe)],capture_output=True,text=True)
        if p.returncode: raise AssertionError('Missing diagnostic API/implementation:\n'+p.stderr)
    def run_board(self,board,mode=1,ch=0,terminal='P',settle=2,samples=32,fault=0,command=180,operation=3000):
        try:
            start=time.monotonic()
            p=subprocess.Popen([str(self.exe),board.path,str(mode),str(ch),terminal,str(settle),str(samples),str(command),str(operation),str(fault)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            board.proc=p
            out,err=p.communicate(timeout=13)
            self.assertFalse(err,err)
            return json.loads(out),time.monotonic()-start
        finally: board.close()
    def test_nonreset_snapshot_unsupported_bias_never_mutates(self):
        regs=DEFAULT.copy(); regs[14]^=1
        board=Board(regs)
        r,elapsed=self.run_board(board)
        self.assertEqual(r['before_mask'],0xffffff)
        self.assertEqual(r['before'],regs)
        self.assertEqual(r['error'],errno.EOPNOTSUPP)
        self.assertEqual(r['cleanup'],0)
        self.assertEqual(board.commands,[b's',b'V',b'?'])
        self.assertLess(elapsed,1)

    def test_shorted_clean_window_restores_all_channels_and_leadoff(self):
        board=Board(split=True)
        r,elapsed=self.run_board(board)
        self.assertEqual(r['rc'],0,r)
        self.assertEqual(r['cleanup'],1)
        self.assertEqual(r['samples'],32)
        self.assertEqual(r['settled'],2)
        self.assertEqual(r['frames'],34)
        self.assertEqual(r['before'],board.original)
        self.assertEqual(r['after'],board.original)
        self.assertEqual(board.regs,board.original)
        expected=board.original.copy(); expected[5]=0x61; expected[15]=expected[16]=0; expected[21]=0
        self.assertEqual(r['test'],expected)
        self.assertEqual(r['nv'],[-22351,22351]*16)
        self.assertIn(b'x1061100X',board.commands)
        self.assertNotIn(b'v',board.commands)
        self.assertLess(elapsed,2)

    def test_impedance_one_input_gain1_bias_and_global_srb_restored(self):
        for terminal in ('P','N'):
            with self.subTest(terminal=terminal):
                board=Board()
                r,_=self.run_board(board,mode=2,ch=1,terminal=terminal)
                self.assertEqual(r['rc'],0,r)
                expected=board.original.copy(); expected[6]=0; expected[13]|=2; expected[14]|=2; expected[21]=0
                expected[15]=2 if terminal=='P' else 0; expected[16]=2 if terminal=='N' else 0
                self.assertEqual(r['test'],expected)
                self.assertEqual(r['nv'],[-536441,536441]*16)
                self.assertEqual(board.regs,board.original)
                self.assertEqual(r['cleanup'],1)
                self.assertIn(b'x2000100X',board.commands)
                self.assertIn(b'z210Z' if terminal=='P' else b'z201Z',board.commands)

    def test_partial_command_retries_or_waits_firmware_timeout_before_cleanup(self):
        for fault in (1,2):
            with self.subTest(fault=fault):
                board=Board()
                r,elapsed=self.run_board(board,fault=fault)
                if fault==1: self.assertEqual(r['rc'],0,r)
                else:
                    self.assertEqual(r['error'],errno.EIO,r)
                    self.assertGreater(elapsed,1.1)
                self.assertEqual(r['cleanup'],1,r)
                self.assertEqual(board.regs,board.original)
                self.assertLess(elapsed,3)

    def test_version_requires_exact_three_components(self):
        board=Board(version=b'v3.1$$$')
        r,_=self.run_board(board)
        self.assertNotEqual(r['rc'],0)
        self.assertEqual(board.commands,[b's',b'V'])

    def test_faults_keep_partial_evidence_and_restore(self):
        for name,error,field in [('gap',errno.EPROTO,'gaps'),('bad_frame',errno.EPROTO,'bad_frames'),('rail',errno.ERANGE,'rails')]:
            with self.subTest(name=name):
                board=Board(**{name:True})
                r,elapsed=self.run_board(board,mode=2)
                self.assertEqual(r['error'],error,r)
                self.assertEqual(r[field],1,r)
                self.assertEqual(r['samples'],3,r)
                self.assertEqual(r['cleanup'],1,r)
                self.assertEqual(board.regs,board.original)
                self.assertLess(elapsed,2)

    def test_ack_without_effect_and_wrong_ack_never_start(self):
        for kw in ({'no_x_effect':True},{'no_z_effect':True},{'bad_ack':True}):
            with self.subTest(kw=kw):
                board=Board(**kw)
                r,_=self.run_board(board,mode=2)
                self.assertEqual(r['error'],errno.EPROTO,r)
                self.assertNotIn(b'b',board.commands)
                self.assertNotEqual(r['rc'],0)

    def test_cancel_each_transition_cleanup_ignores_stop(self):
        for stage in ('version','disable','channel','excite','stream'):
            with self.subTest(stage=stage):
                board=Board(cancel=stage)
                r,elapsed=self.run_board(board,mode=2)
                self.assertEqual(r['error'],errno.ECANCELED,r)
                self.assertEqual(r['cleanup'],0 if stage=='version' else 1,r)
                self.assertEqual(board.regs,board.original)
                self.assertLess(elapsed,2)

    def test_timeout_restore_mismatch_and_loss_never_success(self):
        for kw in ({'silent':True},{'restore_mismatch':True},{'loss':True}):
            with self.subTest(kw=kw):
                board=Board(**kw)
                r,elapsed=self.run_board(board,mode=2,operation=350)
                self.assertNotEqual(r['rc'],0,r)
                if 'silent' in kw:
                    self.assertEqual(r['error'],errno.ETIMEDOUT)
                    self.assertEqual(r['cleanup'],1,r)
                elif 'restore_mismatch' in kw:
                    self.assertEqual(r['cleanup'],2,r)
                else: self.assertEqual(r['cleanup'],3,r)
                self.assertLess(elapsed,2)

    def test_complete_preflight_profile_and_representability(self):
        for address,value in ((0,0x1e),(1,0x95),(4,3),(17,1),(21,1),(5,0x78)):
            with self.subTest(address=address):
                regs=DEFAULT.copy(); regs[address]=value
                board=Board(regs)
                r,_=self.run_board(board,mode=2)
                self.assertNotEqual(r['rc'],0,r)
                self.assertFalse(any(c[:1] in (b'x',b'z',b'b',b'v') for c in board.commands))
        malformed=[lambda b:b.replace(b'CONFIG4,',b'Unknown,'),lambda b:b.replace(b'CONFIG2, 02,',b'CONFIG2, 03,'),lambda b:b.replace(b'LIS3DH Registers',b'Daisy ADS Registers'),lambda b:b.replace(b'CH1SET, 05, 68, 0,',b'CH1SET, 05, 68, 1,')]
        for mutate in malformed:
            board=Board(malformed=mutate)
            r,_=self.run_board(board)
            self.assertNotEqual(r['rc'],0,r)
            self.assertFalse(any(c[:1] in (b'x',b'z',b'b',b'v') for c in board.commands))

    def test_counter_wrap_and_maximum_sample_bound(self):
        board=Board()
        r,_=self.run_board(board,samples=1024,settle=0)
        self.assertEqual(r['rc'],0,r)
        self.assertEqual(r['samples'],1024)
        self.assertEqual(r['frames'],1024)
        self.assertEqual(r['gaps'],0)

    def test_cleanup_never_reenables_saved_excitation_if_routing_not_restored(self):
        board=Board(restore_x_no_effect=True)
        r,_=self.run_board(board,mode=2)
        self.assertNotEqual(r['rc'],0,r)
        self.assertEqual(r['cleanup'],2,r)
        self.assertEqual(board.regs[15:17],[0,0])

    def test_maximum_settle_samples_and_int32_overflow(self):
        board=Board(frame_count=5120)
        r,_=self.run_board(board,samples=4096,settle=1024)
        self.assertEqual(r['rc'],0,r)
        self.assertEqual((r['samples'],r['settled'],r['frames']),(4096,1024,5120))
        board=Board(value=5000000)
        r,_=self.run_board(board,mode=2,settle=0)
        self.assertEqual(r['error'],errno.ERANGE,r)
        self.assertEqual(r['samples'],0)
        self.assertEqual(r['cleanup'],1)

    def test_invalid_options_never_open_or_command(self):
        for options in ({'samples':31},{'samples':4097},{'settle':1025},{'ch':8},{'mode':3},{'mode':2,'terminal':'B'}):
            with self.subTest(options=options):
                board=Board()
                r,_=self.run_board(board,**options)
                self.assertEqual(r['error'],errno.EINVAL,r)
                self.assertEqual(board.commands,[])

class CountedReplyTests(unittest.TestCase):
    """SPEC-001: no valid prefix can hide bytes from text validation."""
    setUpClass = classmethod(DiagnosticTests.setUpClass.__func__)
    run_board = DiagnosticTests.run_board
    def test_spec001_binary_reply_rejected(self):
        for bad in (b'\x00', b'\x01', b'\x7f', b'\x80'):
            for target in ('version', 'query', 'ack'):
                with self.subTest(bad=bad, target=target):
                    class BinaryBoard(Board):
                        def send(self, data):
                            if target == 'ack' and data.startswith(b'Success: '):
                                data = data[:-3] + bad + b'INVALID$$$'
                            super().send(data)
                    options = ({'version': b'v3.1.5' + bad + b'INVALID$$$'}
                               if target == 'version' else
                               {'malformed': lambda b: b[:-3] + bad + b'Daisy ADS Registers\r\n$$$'}
                               if target == 'query' else {})
                    board = BinaryBoard(**options)
                    r, elapsed = self.run_board(board)
                    self.assertEqual(r['error'], errno.EPROTO, r)
                    self.assertNotIn(b'b', board.commands)
                    self.assertLess(elapsed, 2)
                    if target == 'ack':
                        self.assertNotEqual(r['cleanup'], 1, r)
                        self.assertEqual(board.regs[15:17], [0, 0])
                    else:
                        self.assertEqual(board.commands, [b's', b'V'] + ([b'?'] if target == 'query' else []))

    def test_spec002_query_rejects_unconsumed_text(self):
        for suffix in (b'INVALID_TRAILING_GARBAGE', b'\r\nUNKNOWN, 00, 00\r\n',
                       b'0x07 00junk\r\n', b'0x10 00\r\n', b'LIS_STUB\r\n'):
            with self.subTest(suffix=suffix):
                board = Board(malformed=lambda b: b[:-3] + suffix + b'$$$')
                r, _ = self.run_board(board)
                self.assertEqual(r['error'], errno.EPROTO, r)
                self.assertEqual(board.commands, [b's', b'V', b'?'])
        board = Board(malformed=lambda b: b.replace(b'CONFIG1,', b'UNKNOWN\r\nCONFIG1,'))
        r, _ = self.run_board(board)
        self.assertEqual(r['error'], errno.EPROTO, r)
        self.assertEqual(board.commands, [b's', b'V', b'?'])

    def test_spec002_official_lis_rows_are_accepted(self):
        # Pinned LIS3DH_readAllRegs: 07..0f, 1f..33, 38..3d;
        # printHex/printlnHex emit two lowercase hex digits with no 0x on value.
        rows = b''.join(f'0x{a:02x} {(a ^ 0xa5):02x}\r\n'.encode()
                        for a in (*range(0x07, 0x10), *range(0x1f, 0x34), *range(0x38, 0x3e)))
        board = Board(malformed=lambda b: b[:-3] + rows + b'$$$')
        r, _ = self.run_board(board)
        self.assertEqual(r['rc'], 0, r)
        self.assertEqual(r['cleanup'], 1, r)
        self.assertEqual(board.regs, board.original)

    def test_spec002_bad_query_after_mutation_fails_closed(self):
        class BadReadbackBoard(Board):
            def send(self, data):
                if b'Board ADS Registers' in data and any(c.startswith(b'z') for c in self.commands):
                    data = data[:-3] + b'INVALID_TRAILING_GARBAGE$$$'
                super().send(data)
        board = BadReadbackBoard()
        r, elapsed = self.run_board(board)
        self.assertEqual(r['error'], errno.EPROTO, r)
        self.assertNotEqual(r['cleanup'], 1, r)
        self.assertEqual(board.regs[15:17], [0, 0])
        self.assertNotIn(b'b', board.commands)
        self.assertLess(elapsed, 2)

if __name__=='__main__': unittest.main(verbosity=2)
