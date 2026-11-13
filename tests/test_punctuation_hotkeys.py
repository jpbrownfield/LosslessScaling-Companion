"""Exercise recorded punctuation identities through Windows and native LS settings."""
import json
from pathlib import Path
import shutil
import string
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from companion.core.models import AppConfig, HotkeyConfig
from companion.core.state import AppState
from companion.services.hotkey_listener import GlobalHotkeyListener, MOD_NOREPEAT
from companion.services.input_simulator import InputSimulator, KEYEVENTF_EXTENDEDKEY
from companion.services.ls_settings import LosslessSettingsXml
from companion.services.overlay_shortcuts import parse_shortcut, reshade_key_data, validate_overlay_hotkeys
from companion.services.reshade_overlay import menu_hotkey_label

FIXTURES = Path(__file__).parent / 'fixtures'
CASES = json.loads((FIXTURES / 'punctuation_hotkeys.json').read_text(encoding='utf-8'))


class PunctuationHotkeyTests(unittest.TestCase):
    def test_every_recorded_symbol_validates_and_registers_with_its_actual_windows_key(self):
        listener = GlobalHotkeyListener(SimpleNamespace(config=AppConfig()), AppState(), lambda: None)
        covered = set()
        with patch('companion.services.hotkey_listener.ctypes.windll.user32') as win32:
            win32.RegisterHotKey.return_value = True
            for case in CASES:
                for shifted in (False, True):
                    symbol = case['shifted'] if shifted else case['unshifted']
                    if symbol is None:
                        continue
                    covered.add(symbol)
                    with self.subTest(code=case['code'], symbol=symbol):
                        modifiers = ['alt', 'shift'] if shifted else ['alt']
                        desired = HotkeyConfig(key=case['canonical'], modifiers=modifiers)
                        self.assertEqual(HotkeyConfig.model_validate_json(desired.model_dump_json()), desired)
                        binding = '+'.join([*desired.modifiers, desired.key])
                        self.assertEqual(InputSimulator.vk_from_string(desired.key), case['vk'])
                        self.assertEqual(InputSimulator.vk_from_string(case['native']), case['vk'])
                        self.assertEqual(parse_shortcut(binding)[:2], (5 if shifted else 1, case['vk']))
                        self.assertEqual(reshade_key_data(binding), (case['vk'], 0, int(shifted), 1))
                        saved = validate_overlay_hotkeys({'overlay_toggle': binding})
                        listener.set_overlay_actions({'overlay_toggle': {'binding': saved['overlay_toggle'], 'callback': lambda: None}})
                        listener._sync_registration()
                        self.assertEqual(win32.RegisterHotKey.call_args.args[-2:],
                                         (MOD_NOREPEAT | (5 if shifted else 1), case['vk']))
                        label = menu_hotkey_label({'hotkeyVk': case['vk'], 'alt': True, 'shift': shifted}, '')
                        self.assertNotIn('VK ', label)
                        self.assertTrue(label.startswith('Alt+Shift+' if shifted else 'Alt+'))
        self.assertTrue(set(string.punctuation) <= covered)

    def test_all_punctuation_and_shifted_digit_bindings_round_trip_through_lossless_scaling_xml(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'Settings.xml'
            shutil.copy2(FIXTURES / 'lossless_scaling/Settings.xml', target)
            settings = LosslessSettingsXml(str(target))
            for case in CASES:
                with self.subTest(code=case['code']):
                    desired = HotkeyConfig(modifiers=['alt', 'shift'], key=case['canonical'])
                    settings.update_control_settings(hotkey=desired)
                    self.assertEqual(ET.parse(target).getroot().findtext('Hotkey'), case['native'])
                    self.assertEqual(settings.read_hotkey(), desired)
                    self.assertFalse(settings.control_changes_required(hotkey=desired)['hotkey'])

    def test_keypad_symbols_are_distinct_from_main_keyboard_and_divide_injection_is_extended(self):
        for keypad, main in [('add', '='), ('subtract', '-'), ('multiply', '8'), ('divide', '/'), ('decimal', '.'), ('separator', ',')]:
            self.assertNotEqual(parse_shortcut('alt+' + keypad)[:2], parse_shortcut('alt+' + main)[:2])
        with patch('companion.services.input_simulator.user32') as win32:
            win32.MapVirtualKeyW.return_value = 0x35
            win32.SendInput.return_value = 1
            InputSimulator.send_scancode_event(0x6F)
            event = win32.SendInput.call_args.args[1]._obj
            self.assertTrue(event.union.ki.dwFlags & KEYEVENTF_EXTENDEDKEY)
        for invalid in ['\u2603', '!', '<']:
            with self.assertRaises(ValueError):
                InputSimulator.vk_from_string(invalid)

    def test_shifted_symbols_still_collide_with_the_same_base_key_and_modifiers(self):
        with self.assertRaisesRegex(ValueError, 'different keys'):
            validate_overlay_hotkeys({'overlay_toggle': 'alt+shift+/', 'profile_editor': 'alt+shift+oemquestion'})
