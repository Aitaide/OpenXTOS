#!/usr/bin/env python3
"""Live builder negative acceptance never changes existing rootfs or media."""
import json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/'scripts/build_live.py'

class LiveBuildTests(unittest.TestCase):
    def test_help_describes_non_installing_live_output(self):
        p=subprocess.run([sys.executable,str(SCRIPT),'--help'],capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertIn('--userspace-root',p.stdout)
        self.assertIn('--kernel-build',p.stdout)
        self.assertIn('--authorized-key',p.stdout)
        self.assertIn('lsm=',p.stdout)
        self.assertIn('plain ASCII',p.stdout)

    def test_existing_output_is_preserved(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            out=Path(tmp)/'existing';out.mkdir();(out/'sentinel').write_text('keep')
            p=subprocess.run([sys.executable,str(SCRIPT),'--userspace-root',tmp,'--kernel-build',tmp,'--busybox',tmp,'--output-dir',str(out)],capture_output=True,text=True)
            self.assertNotEqual(p.returncode,0)
            self.assertIn('already exists',p.stderr)
            self.assertEqual((out/'sentinel').read_text(),'keep')

    def test_missing_input_creates_no_output(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            out=Path(tmp)/'new'
            p=subprocess.run([sys.executable,str(SCRIPT),'--userspace-root',str(Path(tmp)/'missing'),'--kernel-build',tmp,'--busybox',tmp,'--output-dir',str(out)],capture_output=True,text=True)
            self.assertNotEqual(p.returncode,0)
            self.assertFalse(out.exists())

    def test_private_key_input_is_rejected(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('live',SCRIPT)
        assert spec and spec.loader
        m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            key=Path(tmp)/'key';key.write_text('-----BEGIN OPENSSH PRIVATE KEY-----\n')
            with self.assertRaises(ValueError):m.validate_public_key(key)

    def test_source_provenance_reports_clean_and_changed_trees(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('live',SCRIPT)
        assert spec and spec.loader
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.assertTrue(hasattr(module,'source_provenance'), 'builder hardcodes dirty instead of reading source state')
        with tempfile.TemporaryDirectory() as tmp:
            repo=Path(tmp)
            subprocess.run(['git','init','-q',str(repo)],check=True)
            subprocess.run(['git','-C',str(repo),'config','user.name','XTOS Test'],check=True)
            subprocess.run(['git','-C',str(repo),'config','user.email','test@example.invalid'],check=True)
            (repo/'source').write_text('first\n')
            subprocess.run(['git','-C',str(repo),'add','source'],check=True)
            subprocess.run(['git','-C',str(repo),'commit','-qm','fixture'],check=True)
            sha=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()
            self.assertEqual(module.source_provenance(repo), {'source_git_head':sha,'source_state':'clean'})
            (repo/'source').write_text('modified\n')
            self.assertEqual(module.source_provenance(repo)['source_state'],'dirty')
            subprocess.run(['git','-C',str(repo),'checkout','--','source'],check=True)
            (repo/'new-source').write_text('untracked\n')
            self.assertEqual(module.source_provenance(repo)['source_state'],'dirty')

    def test_frozen_source_rejects_mutation_before_candidate_publication(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('live',SCRIPT)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.assertTrue(hasattr(module,'verify_source_unchanged'),
                        'builder does not recheck frozen source before publishing')
        with tempfile.TemporaryDirectory() as tmp:
            repo=Path(tmp)
            subprocess.run(['git','init','-q',str(repo)],check=True)
            subprocess.run(['git','-C',str(repo),'config','user.name','XTOS Test'],check=True)
            subprocess.run(['git','-C',str(repo),'config','user.email','test@example.invalid'],check=True)
            (repo/'source').write_text('frozen\n')
            subprocess.run(['git','-C',str(repo),'add','source'],check=True)
            subprocess.run(['git','-C',str(repo),'commit','-qm','fixture'],check=True)
            initial=module.source_provenance(repo)
            self.assertEqual(module.verify_source_unchanged(initial,repo),initial)
            (repo/'new-source').write_text('untracked\n')
            with self.assertRaises(ValueError):module.verify_source_unchanged(initial,repo)
            (repo/'new-source').unlink()
            (repo/'source').write_text('changed while building\n')
            with self.assertRaises(ValueError):module.verify_source_unchanged(initial,repo)

    def test_strict_build_preflight_rejects_dirty_or_wrong_source(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('live',SCRIPT)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.assertTrue(hasattr(module,'validate_source_provenance'),
                        'release builder has no strict provenance gate')
        clean={'source_git_head':'a'*40,'source_state':'clean'}
        module.validate_source_provenance(clean,expected_source='a'*40,require_clean=True)
        for invalid,expected in [(dict(clean,source_state='dirty'),'a'*40),
                                 (clean,'b'*40),(clean,'main')]:
            with self.subTest(invalid=invalid,expected=expected),self.assertRaises(ValueError):
                module.validate_source_provenance(invalid,expected_source=expected,require_clean=True)

    def test_developer_has_graphics_groups_but_not_root_group(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('live',SCRIPT)
        assert spec and spec.loader
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.assertTrue(hasattr(module, 'account_groups'), 'missing graphics group provisioning')
        groups={line.split(':')[0]:line.split(':') for line in module.account_groups().splitlines()}
        for name,gid in [('tty','5'),('dialout','20'),('video','44'),('render','1001')]:
            self.assertEqual(groups[name][2],gid)
        for name in ['dialout','video','render']:
            self.assertIn('xtos',groups[name][3].split(','))
        self.assertNotIn('xtos',groups['root'][3].split(','))

    def test_key_only_accounts_have_no_usable_password_or_locked_key_auth(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('live',SCRIPT)
        assert spec and spec.loader
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        shadow=module.account_shadow(key_login=True)
        for line in shadow.splitlines():
            if line.startswith('runner:'):
                self.assertEqual(line.split(':')[1],'!')
                continue
            value=line.split(':')[1]
            self.assertEqual(value,'NP')
            self.assertNotIn(value[0],'!*')
        self.assertIn('root:!:',module.account_shadow(key_login=False))

    def test_runner_uses_dedicated_locked_account_and_fixed_compilation_identity(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('live_runner',SCRIPT)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.assertTrue(hasattr(module,'account_passwd'), 'runner account not provisioned')
        passwd={line.split(':')[0]:line.split(':') for line in module.account_passwd().splitlines()}
        groups={line.split(':')[0]:line.split(':') for line in module.account_groups().splitlines()}
        self.assertEqual(passwd['runner'][2:4],['62000','62000'])
        self.assertEqual(passwd['runner'][5:],['/nonexistent','/usr/sbin/nologin'])
        self.assertEqual(groups['runner'][2:4],['62000',''])
        self.assertIn('runner:!:',module.account_shadow(key_login=True))
        self.assertEqual(module.NATIVE_TOOLS.get('runner'),'usr/libexec/xtos/runner')
        self.assertEqual(module.native_identity_flags('runner'),
                         ['-DXTOS_RUNNER_UID=62000','-DXTOS_RUNNER_GID=62000'])
        self.assertEqual(module.native_identity_flags('info'),[])

    def test_native_tool_loop_builds_modelverify_without_other_tools(self):
        import importlib.util
        from unittest.mock import patch
        spec=importlib.util.spec_from_file_location('live_modelverify_loop',SCRIPT)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            stage=Path(tmp)/'rootfs';stage.mkdir()
            with patch.dict(module.NATIVE_TOOLS,
                            {'modelverify':'usr/libexec/xtos/modelverify'},clear=True):
                module.build_native_tools(stage)
            helper=stage/'usr/libexec/xtos/modelverify'
            self.assertTrue(os.access(helper,os.X_OK))
            dynamic=subprocess.run(['readelf','-l',str(helper)],capture_output=True,text=True,check=True)
            self.assertNotIn('INTERP',dynamic.stdout)
            message=Path(tmp)/'message';message.write_bytes(b'live staged model verifier fixture\\n')
            key=Path(tmp)/'private.pem';der=Path(tmp)/'public.der';signature=Path(tmp)/'signature'
            subprocess.run(['openssl','genpkey','-algorithm','Ed25519','-out',str(key)],
                           check=True,capture_output=True)
            subprocess.run(['openssl','pkey','-in',str(key),'-pubout','-outform','DER',
                            '-out',str(der)],check=True,capture_output=True)
            raw=Path(tmp)/'public.raw';raw.write_bytes(der.read_bytes()[-32:])
            subprocess.run(['openssl','pkeyutl','-sign','-inkey',str(key),'-rawin',
                            '-in',str(message),'-out',str(signature)],check=True,capture_output=True)
            command=[str(helper),'--pubkey',str(raw),'--message',str(message),
                     '--signature',str(signature)]
            accepted=subprocess.run(command,capture_output=True,timeout=5)
            self.assertEqual((accepted.returncode,accepted.stdout,accepted.stderr),(0,b'',b''))
            sig=bytearray(signature.read_bytes());sig[0]^=1;signature.write_bytes(sig)
            refused=subprocess.run(command,capture_output=True,timeout=5)
            self.assertEqual((refused.returncode,refused.stdout,refused.stderr),(1,b'',b''))

    def test_live_kernel_contract_requires_active_landlock_prerequisites(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('live_kernel',SCRIPT)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        for value in ['CONFIG_SECURITY=y','CONFIG_SECURITY_LANDLOCK=y','CONFIG_SECCOMP=y',
                      'CONFIG_SECCOMP_FILTER=y','CONFIG_SECURITYFS=y','CONFIG_NAMESPACES=y',
                      'CONFIG_UTS_NS=y','CONFIG_IPC_NS=y','CONFIG_PID_NS=y','CONFIG_NET_NS=y','CONFIG_USER_NS=y']:
            self.assertIn(value,module.REQUIRED_CONFIG)

    def test_build_rejects_disabled_landlock_before_later_kernel_inputs(self):
        import importlib.util
        from types import SimpleNamespace
        from unittest.mock import patch
        spec=importlib.util.spec_from_file_location('live_lsm_refusal',SCRIPT)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            # Invalid release is a sentinel: a configuration refusal must occur
            # before this later input is read, with no candidate/stage created.
            (root/'include/config').mkdir(parents=True)
            (root/'include/config/kernel.release').write_text('not-a-valid-release!')
            args=SimpleNamespace(output_dir=root/'output',userspace_root=root,kernel_build=root)
            for lsm in ('', 'CONFIG_LSM="lockdown,yama"', 'CONFIG_LSM="landlock_extra,lockdown"'):
                with self.subTest(lsm=lsm):
                    (root/'.config').write_text('\n'.join(module.REQUIRED_CONFIG)+'\n'+lsm+'\n')
                    with patch.object(module,'open_owned_root',side_effect=lambda _:(os.open(root,os.O_RDONLY),{'architecture':'x86_64'})):
                        with self.assertRaises(Exception) as refused:
                            module.build(args)
                    self.assertIsInstance(refused.exception,ValueError)
                    self.assertIn('landlock',str(refused.exception).lower())
                    self.assertFalse(args.output_dir.exists())
                    self.assertFalse(any(root.glob('.output-*')))

    def test_build_accepts_exact_active_landlock_then_checks_later_inputs(self):
        import importlib.util
        from types import SimpleNamespace
        from unittest.mock import patch
        spec=importlib.util.spec_from_file_location('live_lsm_control',SCRIPT)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'.config').write_text('\n'.join(module.REQUIRED_CONFIG)+'\nCONFIG_LSM="lockdown,landlock,yama"\n')
            (root/'include/config').mkdir(parents=True)
            (root/'include/config/kernel.release').write_text('not-a-valid-release!')
            args=SimpleNamespace(output_dir=root/'output',userspace_root=root,kernel_build=root)
            with patch.object(module,'open_owned_root',side_effect=lambda _:(os.open(root,os.O_RDONLY),{'architecture':'x86_64'})):
                with self.assertRaisesRegex(ValueError,'unsafe kernel release'):
                    module.build(args)
            self.assertFalse(args.output_dir.exists())

    def test_shared_runner_prerequisites_are_required_and_missing_each_fails_build(self):
        import importlib.util
        from types import SimpleNamespace
        from unittest.mock import patch
        spec=importlib.util.spec_from_file_location('live_shared_config',SCRIPT)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        shared=['CONFIG_UTS_NS=y','CONFIG_IPC_NS=y','CONFIG_SECCOMP=y','CONFIG_SECURITYFS=y']
        for setting in shared:
            self.assertIn(setting,module.REQUIRED_CONFIG)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'include/config').mkdir(parents=True)
            (root/'include/config/kernel.release').write_text('not-a-valid-release!')
            args=SimpleNamespace(output_dir=root/'output',userspace_root=root,kernel_build=root)
            full=module.REQUIRED_CONFIG+['CONFIG_LSM="lockdown,landlock,yama"']
            with patch.object(module,'open_owned_root',side_effect=lambda _:(os.open(root,os.O_RDONLY),{'architecture':'x86_64'})):
                for setting in shared:
                    with self.subTest(missing=setting):
                        (root/'.config').write_text(chr(10).join(x for x in full if x!=setting)+chr(10))
                        with self.assertRaisesRegex(ValueError,'Live kernel missing '+setting):
                            module.build(args)
                        self.assertFalse(args.output_dir.exists())
                        self.assertFalse(any(root.glob('.output-*')))
                (root/'.config').write_text(chr(10).join(full)+chr(10))
                with self.assertRaisesRegex(ValueError,'unsafe kernel release'):
                    module.build(args)
            self.assertFalse(args.output_dir.exists())
            self.assertFalse(any(root.glob('.output-*')))

    def test_grub_config_rejects_unsafe_extras_and_preserves_valid_cmdline(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('live_grub_contract',SCRIPT)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.assertTrue(hasattr(module,'grub_config'), 'builder has no validated GRUB generator')
        default=module.grub_config()
        for expected in ['set timeout=3','set default=0','console=tty0','console=ttyS0,115200',
                         'xtos.live=1','linux /boot/bzImage','initrd /boot/initramfs.gz']:
            self.assertIn(expected,default)
        self.assertIn(' linux /boot/bzImage console=tty0 console=ttyS0,115200 xtos.live=1'+chr(10),default)
        extra='root=/dev/vda1 console=ttyS0,115200 xtos.persist=XTOS-PERSIST xtos.luks=1 xtos.toram=1'
        generated=module.grub_config(extra)
        self.assertIn('xtos.live=1 '+extra+chr(10),generated)
        spaced='root=/dev/vda1  xtos.toram=1'
        self.assertIn('xtos.live=1 '+spaced+chr(10),module.grub_config(spaced))
        self.assertEqual(len(default.splitlines()),9)
        invalid=['lsm=lockdown,yama','root=/dev/vda1 lsm=landlock,lockdown',
                 'xtos.persist=XTOS-PERSIST lsm=landlock,lockdown', 'lsm=',
                 'lsm=lockdown lsm=landlock', 'lsm=landlock root=/dev/vda1',
                 '  lsm=landlock  ', 'root=/dev/vda1; reboot',
                 'foo="bar"', "foo='bar'", 'foo='+chr(92)+'bar', 'foo=$grub_platform',
                 'foo=`reboot`', 'foo={x}', 'foo=bar#comment', 'foo=(x)',
                 'foo=bar|reboot', 'foo=bar&reboot', 'foo=bar>file',
                 'foo=bar'+chr(10)+'initrd /evil', 'foo=bar'+chr(9)+' baz',
                 *('foo='+chr(code) for code in range(32)), 'foo='+chr(127),
                 'foo='+chr(160), 'foo=中文']
        for value in invalid:
            with self.subTest(value=value),self.assertRaises(ValueError):
                module.grub_config(value)

    def test_build_uses_validated_grub_before_later_inputs(self):
        import importlib.util
        from types import SimpleNamespace
        from unittest.mock import patch
        spec=importlib.util.spec_from_file_location('live_grub_build',SCRIPT)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            args=SimpleNamespace(output_dir=root/'output',userspace_root=root,kernel_build=root,
                                 kernel_extra='lsm=landlock,lockdown')
            config_text=chr(10).join(module.REQUIRED_CONFIG)+chr(10)+'CONFIG_LSM="lockdown,landlock,yama"'+chr(10)
            (root/'.config').write_text(config_text)
            (root/'include/config').mkdir(parents=True)
            (root/'include/config/kernel.release').write_text('not-a-valid-release!')
            with patch.object(module,'open_owned_root',side_effect=lambda _:(os.open(root,os.O_RDONLY),{'architecture':'x86_64'})):
                with self.assertRaisesRegex(ValueError,'kernel-extra'):
                    module.build(args)
            self.assertFalse(args.output_dir.exists())
            self.assertFalse(any(root.glob('.output-*')))

            args.kernel_extra='xtos.persist=XTOS-PERSIST xtos.luks=1'
            original=module.grub_config
            generated=[]
            def capture(extra):
                result=original(extra)
                generated.append(result)
                return result
            with (patch.object(module,'open_owned_root',side_effect=lambda _:(os.open(root,os.O_RDONLY),{'architecture':'x86_64'})),
                  patch.object(module,'grub_config',side_effect=capture) as generator):
                with self.assertRaisesRegex(ValueError,'unsafe kernel release'):
                    module.build(args)
            generator.assert_called_once_with(args.kernel_extra)
            self.assertIn('xtos.live=1 '+args.kernel_extra+chr(10),generated[0])
            self.assertFalse(args.output_dir.exists())
            self.assertFalse(any(root.glob('.output-*')))

if __name__=='__main__':unittest.main()
