from pathlib import Path
import tempfile
import unittest

from native.addon_manager.prepare import patch_managed_startup


class ManagedStartupPatchTests(unittest.TestCase):
    def test_retains_upstream_feature_setup_in_managed_and_stock_paths(self):
        startup = ('eam::GuiManager::StartGuiThread(addons.get(), [lossless] {\n'
                   '        D3D11Hook::InstallDispatchHooks();\n'
                   '        eam::features::scaling::InstallHooks(lossless);\n'
                   '    });')
        with tempfile.TemporaryDirectory() as directory:
            main = Path(directory) / "main.cpp"
            main.write_text(startup, encoding="utf-8")
            patch_managed_startup(main)
            actual = main.read_text(encoding="utf-8")
        self.assertIn('}, [lossless] { eam::features::scaling::InstallHooks(lossless); }))', actual)
        self.assertTrue(actual.endswith(startup))

    def test_legacy_startup_and_unknown_drift(self):
        legacy = 'eam::GuiManager::StartGuiThread(addons.get(), [] { D3D11Hook::InstallDispatchHooks(); });'
        with tempfile.TemporaryDirectory() as directory:
            main = Path(directory) / "main.cpp"
            main.write_text(legacy, encoding="utf-8")
            patch_managed_startup(main)
            self.assertTrue(main.read_text(encoding="utf-8").endswith(legacy))
            for text in ("new unreviewed startup", legacy + legacy):
                main.write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "Upstream drift"):
                    patch_managed_startup(main)
                self.assertEqual(main.read_text(encoding="utf-8"), text)
