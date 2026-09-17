import unittest

from companion.ui.icons import create_lightning_icon


class LightningIconTests(unittest.TestCase):
    def test_outer_ring_is_white_in_every_state(self):
        for state in ("closed", "open", "scaling"):
            with self.subTest(state=state):
                icon = create_lightning_icon(64, 64, state=state)
                self.assertEqual(icon.getpixel((32, 4))[:3], (255, 255, 255))

    def test_closed_bolt_is_static_grey(self):
        dim = create_lightning_icon(64, 64, state="closed", pulse=0.0)
        bright = create_lightning_icon(64, 64, state="closed", pulse=1.0)
        self.assertEqual(dim.getpixel((32, 32)), bright.getpixel((32, 32)))
        self.assertEqual(bright.getpixel((32, 32))[:3], (148, 163, 184))

    def test_open_and_scaling_bolts_pulse_in_their_respective_colors(self):
        blue_dim = create_lightning_icon(64, 64, state="open", pulse=0.0)
        blue_bright = create_lightning_icon(64, 64, state="open", pulse=1.0)
        yellow_dim = create_lightning_icon(64, 64, state="scaling", pulse=0.0)
        yellow_bright = create_lightning_icon(64, 64, state="scaling", pulse=1.0)

        self.assertNotEqual(blue_dim.getpixel((32, 32)), blue_bright.getpixel((32, 32)))
        self.assertNotEqual(yellow_dim.getpixel((32, 32)), yellow_bright.getpixel((32, 32)))
        self.assertGreater(blue_bright.getpixel((32, 32))[2], blue_bright.getpixel((32, 32))[0])
        self.assertGreater(yellow_bright.getpixel((32, 32))[0], yellow_bright.getpixel((32, 32))[2])


if __name__ == "__main__":
    unittest.main()
