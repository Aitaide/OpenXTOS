#!/usr/bin/env python3
"""Cold-plug regression: exercise the exact boot loop with the shipped BusyBox."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ColdplugTests(unittest.TestCase):
    def test_boot_loop_passes_each_present_modalias_to_kmod(self):
        busybox = ROOT / '.build/live-busybox-gpu-x86_64/busybox'
        if not busybox.is_file():
            self.skipTest('build the maintained live BusyBox before integration test')
        startup = (ROOT / 'system/live/xtos-start').read_text()
        start = startup.index('modloaded_file=/run/xtos-modalias.count\n')
        end = startup.index('\n"$BB" hostname', start)
        block = startup[start:end]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            devices = root / 'devices'
            for name, alias in [('gpu0', 'pci:v00001002d000067DF'),
                                ('Fixed MDIO bus.0', 'platform:fixed-mdio'),
                                ('camera0', 'usb:v1234p5678')]:
                path = devices / name
                path.mkdir(parents=True)
                (path / 'modalias').write_text(alias+'\n')
            log = root / 'calls.jsonl'
            command = root / 'modprobe'
            command.write_text('#!/usr/bin/python3\nimport json,sys\nfrom pathlib import Path\nwith Path('+repr(str(log))+').open("a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n')
            command.chmod(0o755)
            script = ('BB='+str(busybox)+'\n'+block
                      .replace('/sys/devices', str(devices))
                      .replace('/run/xtos-modalias.count', str(root/'count')))
            result = subprocess.run(['sh','-c',script], env={**os.environ,'PATH':str(root)+':/usr/bin:/bin'},
                                    capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
            calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            self.assertEqual(sorted(calls), sorted([['-b','-q','pci:v00001002d000067DF'],
                                                  ['-b','-q','platform:fixed-mdio'],
                                                  ['-b','-q','usb:v1234p5678']]))
            self.assertIn('count=3',result.stdout)


if __name__ == '__main__':
    unittest.main()
