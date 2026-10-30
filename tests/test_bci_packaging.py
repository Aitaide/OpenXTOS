"""Shipping paths must include the native bci command and shared protocol source."""
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[1]
class BciPackagingTests(unittest.TestCase):
    def test_system_stages_parser_builds_and_installs_native_bci(self):
        text=(ROOT/'scripts/build_system.sh').read_text()
        self.assertIn('drivers/xtos/bci/xtos_cyton_packet.h',text)
        self.assertIn('$STAGING/tools/xtos-cyton',text)
        self.assertIn('$ROOTFS/usr/bin/bci',text)
    def test_live_stages_parser_builds_and_installs_native_bci(self):
        text=(ROOT/'scripts/build_live.py').read_text()
        self.assertIn('drivers/xtos/bci/xtos_cyton_packet.h',text)
        self.assertIn("source/'tools/xtos-cyton'",text)
        self.assertIn("stage/'usr/bin/bci'",text)
    def test_actual_live_install_helper_builds_working_native_command(self):
        import importlib.util
        import json
        import os
        import subprocess
        import sys
        import tempfile
        spec=importlib.util.spec_from_file_location('live_bci_packaging',ROOT/'scripts/build_live.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            stage=Path(tmp)/'root';work=Path(tmp)/'work'
            (stage/'usr/bin').mkdir(parents=True);(stage/'run').mkdir()
            work.mkdir()
            module.install_sdk(stage,work)
            self.assertTrue((stage/'usr/bin/bci').is_file())
            self.assertEqual((stage/'usr/include/xtos/bci_config_uapi.h').read_bytes(),
                             (ROOT/'sdk/libxtos-bci/include/xtos/bci_config_uapi.h').read_bytes())
            diagnostic_spec=importlib.util.spec_from_file_location('staged_diagnostic_board',
                ROOT/'tools/xtos-cyton/test_cyton_diagnostic_serial.py')
            diagnostic=importlib.util.module_from_spec(diagnostic_spec)
            diagnostic_spec.loader.exec_module(diagnostic)
            for mode in ('shorted','impedance-p','impedance-n'):
                board=diagnostic.Board()
                output=Path(tmp)/(mode+'.json')
                try:
                    result=subprocess.run([str(stage/'usr/bin/bci'),'diagnose','--device',board.path,
                        '--mode',mode,'--channel','2','--frames','32','--settle-frames','2',
                        '--command-timeout-ms','1000','--output',str(output)],
                        capture_output=True,text=True,timeout=5)
                    self.assertEqual(result.returncode,0,result.stderr)
                    report=json.loads(output.read_text())
                    self.assertTrue(report['result_valid'])
                    self.assertEqual(report['sample_count'],32)
                    self.assertEqual(report['restoration'],'restored')
                    self.assertEqual(report['before']['registers'],report['after']['registers'])
                    self.assertEqual(board.regs,board.original)
                    self.assertFalse(report['physical_acceptance'])
                finally:
                    board.close()
            self.assertEqual((stage/'usr/include/xtos/bci_snapshot_uapi.h').read_bytes(),
                             (ROOT/'sdk/libxtos-bci/include/xtos/bci_snapshot_uapi.h').read_bytes())
            if os.geteuid()==0:
                result=subprocess.run([sys.executable,str(ROOT/'scripts/verify_native_bci_runtime.py'),
                    '--rootfs',str(stage)],capture_output=True,text=True,timeout=20)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                report=json.loads(result.stdout)
                self.assertEqual(report['status'],'pass')
                self.assertTrue(report['eight_channel_filter'])
                self.assertTrue(report['offline_calibration_fit_apply'])
                self.assertTrue(report['configuration_query_error_path'])
                self.assertFalse(report['physical_evidence'])
            else:
                result=subprocess.run([str(stage/'usr/bin/bci'),'--help'],capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)
    def test_system_busybox_gate_resolves_real_shell_choice(self):
        text=(ROOT/'scripts/build_system.sh').read_text()
        self.assertIn('CONFIG_SH_IS_ASH=y',text)
        self.assertNotIn('CONFIG_SH=y',text)
        config=(ROOT/'system/busybox_defconfig').read_text()
        self.assertIn('CONFIG_SH_IS_ASH=y',config)
    def test_shared_config_parser_is_staged_and_a_build_dependency(self):
        for path in ('scripts/build_system.sh', 'scripts/build_live.py'):
            with self.subTest(path=path):
                self.assertIn('xtos_cyton_config.h', (ROOT/path).read_text())
        self.assertIn('xtos_cyton_config.h', (ROOT/'tools/xtos-cyton/Makefile').read_text())
        self.assertIn('include/xtos/bci_config_uapi.h',
                      (ROOT/'sdk/libxtos-bci/Makefile').read_text())
    def test_maintenance_executes_all_new_suites(self):
        text=(ROOT/'scripts/test.sh').read_text()
        for name in ('test_bci_cli.py','test_bci_lifetime.py','test_bci_snapshot_sdk.py',
                     'test_bci_snapshot_kernel.py','test_bci_packaging.py','xtos-cyton',
                     'test_cyton_config.py','test_bci_device_config.py',
                     'test_bci_device_kernel.py','test_bci_device_sdk.py',
                     'test_bci_config_cli.py','test_bci_main_config.py','test_bci_main_calibration.py','test_bci_main_diagnostic.py',
                     'test_bci_diagnostic.py','test_bci_diagnostic_options.py',
                     'test_cyton_diagnostic_serial.py','test_bci_diagnostic_cli.py',
                     'test_bci_calibration.py','test_bci_calibration_cli.py'):
            with self.subTest(name=name): self.assertIn(name,text)
if __name__=='__main__': unittest.main()
