import types, unittest
from alice_for_marusia.wake import masked_pcm


class CueTests(unittest.TestCase):
    def test_cue_and_tail_are_silent_only_in_detector_copy(self):
        block = b"\x11\x22" * 320
        guard = types.SimpleNamespace(value=12.0)
        self.assertEqual(masked_pcm(block, guard, 11.5), bytes(640))
        self.assertEqual(block, b"\x11\x22" * 320)
        self.assertEqual(masked_pcm(block, guard, 12.0), block)

    def test_expired_guard_does_not_suppress_normal_speech(self):
        self.assertEqual(
            masked_pcm(b"voice", types.SimpleNamespace(value=0), 10), b"voice"
        )


if __name__ == "__main__":
    unittest.main()
