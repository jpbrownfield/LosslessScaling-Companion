import unittest

from companion.services.input_simulator import InputSimulator


class InputSimulatorKeyMapTests(unittest.TestCase):
    def test_extended_function_keys_are_mapped(self):
        self.assertEqual(InputSimulator.vk_from_string("F13"), 0x7C)
        self.assertEqual(InputSimulator.vk_from_string("f23"), 0x86)
        self.assertEqual(InputSimulator.vk_from_string("F24"), 0x87)


if __name__ == "__main__":
    unittest.main()
