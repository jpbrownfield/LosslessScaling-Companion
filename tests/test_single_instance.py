import unittest

from companion.services.single_instance import SingleInstanceGuard


class SingleInstanceGuardTests(unittest.TestCase):
    def test_second_acquire_fails_while_first_is_held(self):
        first = SingleInstanceGuard()
        self.assertTrue(first.acquire())
        try:
            second = SingleInstanceGuard()
            self.assertFalse(second.acquire())
            self.assertIsNotNone(second.holder_pid)
        finally:
            first.release()

    def test_acquire_succeeds_after_release(self):
        guard = SingleInstanceGuard()
        self.assertTrue(guard.acquire())
        guard.release()
        again = SingleInstanceGuard()
        self.assertTrue(again.acquire())
        again.release()


if __name__ == "__main__":
    unittest.main()
