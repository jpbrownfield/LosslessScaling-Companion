import unittest

from companion.services.hdr_display import HdrDisplayDetector


class HdrDisplayDetectorTests(unittest.TestCase):
    def test_selects_highest_active_hdr_peak(self):
        detector = HdrDisplayDetector(lambda: [
            {"deviceName": "SDR", "colorSpace": 0, "maxLuminanceNits": 1200},
            {"deviceName": "HDR 600", "colorSpace": 12, "maxLuminanceNits": 603.4},
            {"deviceName": "HDR 1000", "hdrActive": True, "maxLuminanceNits": 991.6},
        ])

        result = detector.detect()

        self.assertTrue(result["available"])
        self.assertEqual(result["detectedNits"], 992)
        self.assertEqual(result["displayName"], "HDR 1000")
        self.assertEqual(result["activeHdrDisplays"], 2)

    def test_returns_unavailable_when_hdr_is_not_active(self):
        detector = HdrDisplayDetector(lambda: [
            {"deviceName": "Remote Display", "colorSpace": 0, "maxLuminanceNits": 1000},
        ])

        self.assertEqual(detector.detect()["detectedNits"], None)


if __name__ == "__main__":
    unittest.main()
