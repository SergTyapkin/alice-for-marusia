import os, tempfile, unittest
from unittest.mock import patch
from alice_for_marusia.indicators import ListeningLeds


class LedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        for i in range(1, 40):
            path = os.path.join(self.temp.name, "led%d" % i)
            os.mkdir(path)
            with open(os.path.join(path, "brightness"), "w") as f:
                f.write("0")
        self.led = ListeningLeds(self.temp.name, interval=0)

    def tearDown(self):
        self.led.close_channels()
        self.temp.cleanup()

    def test_floor_reactivity_and_off(self):
        self.assertTrue(self.led.start())
        low = self.led.read()
        self.assertGreater(low[0], 0)
        self.assertGreater(low[1], 0)
        self.led.update(3700)
        loud = self.led.read()
        self.assertGreater(loud[0], low[0])
        self.led.stop()
        self.assertFalse(any(self.led.read()))

    def test_does_not_override_existing_signal(self):
        with open(self.led.paths[2], "w") as f:
            f.write("50")
        self.assertFalse(self.led.start())
        self.assertEqual(self.led.read()[2], 50)

    def test_releases_on_external_change(self):
        self.led.start()
        with open(self.led.paths[2], "w") as f:
            f.write("99")
        self.led.update(500)
        self.assertFalse(self.led.owned)
        self.led.stop()
        self.assertEqual(self.led.read()[2], 99)

    def test_thinking_is_dim_and_rotates(self):
        self.assertTrue(self.led.start())
        self.led.thinking(100.0)
        first = self.led.read()
        self.led.thinking(100.5)
        second = self.led.read()
        self.assertNotEqual(first[:36], second[:36])
        self.assertGreater(min(first[i] for i in range(0, 36, 3)), 0)
        self.assertLess(min(first[i] for i in range(0, 36, 3)), 8)
        self.assertLess(first[36], 35)
        self.led.stop()
        self.assertFalse(any(self.led.read()))


if __name__ == "__main__":
    unittest.main()
