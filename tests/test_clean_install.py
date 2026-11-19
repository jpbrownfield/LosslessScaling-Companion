import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from companion.core.models import Profile
from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services.asset_store import AssetStore
from companion.services.automation import AutomationController
from companion.services.clean_install import clean_install, recover_clean_install, JOURNAL, TARGETS
from companion.services.managed_runtime import ManagedRuntime, BOOTSTRAP_FILES


class CleanInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        for name in ('LosslessScaling.exe', 'LosslessScaling.dll', 'Lossless.dll'):
            (self.source / name).write_bytes(name.encode())
        (self.source / 'LosslessScaling.runtimeconfig.json').write_text('{"runtimeOptions": {}}')
        self.bootstrap = self.root / 'bootstrap'
        self.bootstrap.mkdir()
        for name in BOOTSTRAP_FILES:
            (self.bootstrap / name).write_bytes(name.encode())
        hook = patch('companion.services.managed_runtime.bootstrap_directory', return_value=self.bootstrap)
        hook.start()
        self.addCleanup(hook.stop)
        self.native = self.root / 'native-settings.xml'
        self.native.write_text('<Settings><Keep>native</Keep></Settings>')
        seed = patch('companion.services.managed_runtime.native_settings_path', return_value=self.native)
        seed.start()
        self.addCleanup(seed.stop)
        self.manager = ProfileManager(self.root / 'config')
        self.runtime = ManagedRuntime(self.manager.config_dir)
        self.store = AssetStore(self.root / 'packages')
        self.executable = self.runtime.prepare(self.source / 'LosslessScaling.exe', self.store)
        self.runtime.bind(self.manager, self.executable, self.store)
        cfg = self.manager.config
        cfg.lossless_source_exe_path = str(self.source / 'LosslessScaling.exe')
        cfg.asset_store_path = str(self.store.root)
        cfg.profiles.append(Profile(id='custom', name='Custom', addon_config={'custom': 123}))
        cfg.profiles[0].native_scaling_settings = {'Strength': '42'}
        cfg.overlay_hotkeys['overlay_toggle'] = 'end'
        cfg.run_at_startup = True
        cfg.gloam_hdr_enabled = True
        cfg.default_addon_config = {'custom': 123}
        cfg.addon_manager.version = 'custom'
        cfg.ls_gpu_id_map = {'old': 7}
        self.manager.save_config()
        self.runtime.settings.write_text('<Settings><Keep>private</Keep></Settings>')
        (self.executable.parent / 'addons/test').mkdir(parents=True)
        (self.executable.parent / 'addons/test/config.json').write_text('{"old":true}')
        for directory in ('addon-configs', 'reshade-profiles'):
            path = self.manager.config_dir / directory
            path.mkdir(exist_ok=True)
            (path / 'custom.ini').write_text('old')
        self.cache = self.store.packages / 'cached.dll'
        self.cache.write_bytes(b'downloaded package')
        self.state = AppState()
        self.state.is_scaling_active = True
        self.state.current_active_profile = cfg.profiles[-1]
        self.watcher = Mock()
        self.watcher.check_is_lossless_scaling_running.return_value = True
        self.watcher.stop_lossless_scaling.return_value = True
        self.automation = AutomationController(self.manager, self.state, process_watcher=self.watcher,
                                               asset_store=self.store)
        self.cleanup = Mock()
        self.old_config = cfg.model_copy(deep=True)
        self.original = self.snapshot(self.manager.config_dir)

    @staticmethod
    def snapshot(root):
        return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}

    def run_reset(self):
        return clean_install(self.automation, self.cleanup)

    def assert_rolled_back(self):
        self.assertEqual(self.snapshot(self.manager.config_dir), self.original)
        self.assertEqual(self.manager.config, self.old_config)
        self.assertFalse(self.state.update_in_progress)
        self.assertFalse(list(self.manager.config_dir.glob('.clean-install-*')))

    def test_complete_reset_deletes_old_clones_settings_and_profiles_but_preserves_source_and_packages(self):
        cfg = self.manager.config
        source = self.snapshot(self.source)
        old_tree = self.runtime.root / 'installations/old-generation'
        old_tree.mkdir()
        (old_tree / 'old.dll').write_bytes(b'old')
        result = self.run_reset()
        self.assertTrue(result['ok'])
        self.assertIs(self.manager.config, cfg, 'Live services must retain the same config object')
        self.assertFalse(old_tree.exists())
        self.assertFalse((self.executable.parent / 'addons/test/config.json').exists())
        self.assertNotIn('Keep', self.runtime.settings.read_text())
        self.assertEqual(self.automation.lossless_settings.initial_backup_path.read_bytes(), self.runtime.settings.read_bytes())
        self.assertEqual(self.native.read_text(), '<Settings><Keep>native</Keep></Settings>')
        self.assertEqual(self.snapshot(self.source), source)
        self.assertEqual(self.cache.read_bytes(), b'downloaded package')
        expected = self.manager._get_default_config()
        for field in ('lossless_source_exe_path', 'lossless_scaling_exe_path',
                      'lossless_settings_xml_path', 'asset_store_path'):
            setattr(expected, field, getattr(cfg, field))
        self.manager.profile_settings.prepare(expected.profiles)
        self.assertEqual(cfg, expected)
        self.assertEqual(ProfileManager(self.manager.config_dir).config.model_dump(), cfg.model_dump())
        self.assertEqual({p.stem for p in (self.manager.config_dir / 'profile-settings').glob('*.json')},
                         {p.id for p in cfg.profiles})
        self.assertFalse((self.manager.config_dir / 'addon-configs').exists())
        self.assertFalse((self.manager.config_dir / 'reshade-profiles').exists())
        self.assertFalse((self.manager.config_dir / JOURNAL).exists())
        self.assertFalse(list(self.manager.config_dir.glob('.clean-install-*')))
        self.assertTrue(self.runtime.validate(Path(cfg.lossless_scaling_exe_path))['deploymentGeneration'])
        self.cleanup.assert_called_once()
        self.watcher.stop_lossless_scaling.assert_called_once()
        self.watcher.launch_lossless_scaling.assert_not_called()
        self.assertFalse(self.state.is_scaling_active)
        self.assertIsNone(self.state.current_active_profile)

    def test_failed_stop_preserves_every_file_and_setting(self):
        self.watcher.stop_lossless_scaling.return_value = False
        with self.assertRaisesRegex(RuntimeError, 'Could not stop'):
            self.run_reset()
        self.cleanup.assert_not_called()
        self.assert_rolled_back()

    def test_failed_clone_rolls_back_files_and_settings(self):
        def fail(*args, **kwargs):
            self.runtime.root.mkdir(parents=True)
            (self.runtime.root / 'partial').write_text('partial')
            raise OSError('copy failed')
        with patch.object(self.runtime, 'prepare', side_effect=fail):
            with self.assertRaisesRegex(OSError, 'copy failed'):
                self.run_reset()
        self.cleanup.assert_not_called()
        self.assert_rolled_back()

    def test_failed_settings_save_rolls_back_files_and_settings(self):
        with patch.object(self.manager, 'save_config', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.run_reset()
        self.assert_rolled_back()

    def test_failed_atomic_settings_replace_removes_partial_temp_and_restores_original(self):
        replace = os.replace
        def fail(source, destination):
            if Path(destination) == self.manager.config_file:
                raise OSError('settings locked')
            return replace(source, destination)
        with patch('companion.core.profile_manager.os.replace', side_effect=fail):
            with self.assertRaisesRegex(OSError, 'settings locked'):
                self.run_reset()
        self.assert_rolled_back()

    def test_failure_to_commit_recovery_journal_restores_already_saved_old_settings(self):
        atomic = AssetStore._atomic_json
        def fail(path, value):
            if Path(path).name == JOURNAL and value.get('phase') == 'committed':
                raise OSError('journal locked')
            return atomic(path, value)
        with patch.object(AssetStore, '_atomic_json', side_effect=fail):
            with self.assertRaisesRegex(OSError, 'journal locked'):
                self.run_reset()
        self.assert_rolled_back()

    def test_stale_foreground_callback_cannot_restore_a_deleted_profile_after_reset(self):
        old_profile = self.manager.config.profiles[-1]
        self.run_reset()
        self.automation.activate_profile(old_profile, source='process match')
        self.assertIsNone(self.state.current_active_profile)
        self.assertNotIn(old_profile.id, [profile.id for profile in self.manager.config.profiles])

    def test_changed_source_rebinds_confirmation_monitor_to_the_new_clone_before_resuming(self):
        from companion.services.ls_inspector import LosslessScalingInspector, ScaledTargetInfo
        inspector = LosslessScalingInspector(lossless_exe_path=str(self.executable),
            settings_xml_path=str(self.runtime.settings))
        inspector.last_known_target = ScaledTargetInfo(is_active=True, pid=123)
        (self.source / 'LosslessScaling.dll').write_bytes(b'updated source')
        refreshed = []
        def refresh():
            self.assertTrue(self.state.update_in_progress)
            cfg = self.manager.config
            inspector.rebind_runtime(cfg.lossless_scaling_exe_path, cfg.lossless_settings_xml_path)
            refreshed.append(True)
        self.automation.runtime_reset_callback = refresh
        self.run_reset()
        self.assertEqual(refreshed, [True])
        self.assertNotEqual(inspector.lossless_exe_path, str(self.executable))
        self.assertTrue(Path(inspector.lossless_exe_path).is_file())
        self.assertFalse(self.executable.exists())
        self.assertFalse(inspector.last_known_target.is_active)

    def test_failed_integration_cleanup_does_not_commit_reset(self):
        self.cleanup.side_effect = RuntimeError('RTSS locked')
        with self.assertRaisesRegex(RuntimeError, 'RTSS locked'):
            self.run_reset()
        self.assert_rolled_back()

    def test_failed_fresh_settings_backup_does_not_commit_reset(self):
        with patch.object(self.automation.lossless_settings, 'ensure_initial_backup', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'initial backup'):
                self.run_reset()
        self.cleanup.assert_not_called()
        self.assert_rolled_back()

    def test_locked_old_files_report_pending_cleanup_and_are_removed_on_next_launch(self):
        import companion.services.clean_install as module
        original = module.shutil.rmtree
        def remove(path, *args, **kwargs):
            if Path(path).name.startswith('.clean-install-'):
                raise PermissionError('scanner lock')
            return original(path, *args, **kwargs)
        with patch.object(module.shutil, 'rmtree', side_effect=remove):
            result = self.run_reset()
        self.assertFalse(result['ok'])
        self.assertTrue(result['settingsReset'])
        self.assertIn('pending', result['warnings'][0])
        cfg = self.manager.config.model_copy(deep=True)
        self.assertEqual(ProfileManager(self.manager.config_dir).config.model_dump(), cfg.model_dump())
        self.assertFalse(list(self.manager.config_dir.glob('.clean-install-*')))
        self.assertFalse((self.manager.config_dir / JOURNAL).exists())

    def test_startup_rolls_back_interrupted_reset_before_loading_settings(self):
        root = self.manager.config_dir
        identifier = 'a' * 32
        backup = root / ('.clean-install-' + identifier)
        backup.mkdir()
        existed = {name: (root / relative).exists() for name, relative in TARGETS.items()}
        (root / JOURNAL).write_text(json.dumps({'schema': 1, 'id': identifier,
            'phase': 'preparing', 'existed': existed}))
        for name, relative in TARGETS.items():
            if existed[name]:
                (root / relative).rename(backup / name)
        self.runtime.prepare(self.source / 'LosslessScaling.exe', self.store, fresh_settings=True)
        (root / 'settings.json').write_text('{"profiles": []}')
        reloaded = ProfileManager(root)
        self.assertEqual(reloaded.config.model_dump(), self.old_config.model_dump())
        self.assert_rolled_back()

    def test_stale_manifest_cannot_restore_old_addon_files_after_reset(self):
        from companion.services.deployment_manager import DeploymentManager
        addon = self.root / 'injected.dll'
        addon.write_bytes(b'addon engine')
        manager = DeploymentManager(self.store)
        manager.apply(profile_id='custom', lossless_scaling_exe=str(self.executable),
                      files=[{'relative_path': 'Lossless.dll', 'source_path': str(addon)}])
        old_manifest = manager.manifest_path
        self.run_reset()
        self.assertEqual(manager._read_manifest(str(self.executable))['files'], [])
        self.assertNotEqual(manager.manifest_path, old_manifest)
        self.assertEqual((self.executable.parent / 'Lossless.dll').read_bytes(), b'Lossless.dll')

    def test_busy_states_and_missing_source_cannot_start_reset(self):
        for field in ('update_in_progress', 'benchmark_mode_active', 'gpu_mapping_in_progress'):
            with self.subTest(field=field):
                setattr(self.state, field, True)
                with self.assertRaises(RuntimeError):
                    self.run_reset()
                setattr(self.state, field, False)
        self.manager.config.lossless_source_exe_path = str(self.root / 'missing.exe')
        with self.assertRaises(FileNotFoundError):
            self.run_reset()
        self.watcher.stop_lossless_scaling.assert_not_called()

    def test_rejects_untrusted_recovery_paths_without_touching_files(self):
        journal = self.manager.config_dir / JOURNAL
        journal.write_text(json.dumps({'schema': 1, 'id': '../../source', 'phase': 'committed',
            'existed': {name: True for name in TARGETS}}))
        source = self.snapshot(self.source)
        with self.assertRaisesRegex(RuntimeError, 'Invalid clean install'):
            recover_clean_install(self.manager.config_dir)
        self.assertEqual(self.snapshot(self.source), source)
        journal.unlink()

    @unittest.skipUnless(os.name == 'nt', 'Windows junction safety')
    def test_rejects_junction_inside_clone_and_leaves_external_data_untouched(self):
        import subprocess
        linked = self.executable.parent / 'external'
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(linked), str(self.source)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        try:
            with self.assertRaisesRegex(RuntimeError, 'linked|escapes'):
                self.run_reset()
            self.assertTrue((self.source / 'Lossless.dll').is_file())
            self.watcher.stop_lossless_scaling.assert_not_called()
        finally:
            linked.rmdir()  # Remove the junction itself, never its target.
