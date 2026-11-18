import asyncio, queue, tempfile, threading, types, unittest, wave
from unittest.mock import patch
from alice_for_marusia.speech import TwoPartSegmenter, speech_worker


class SegmentTests(unittest.TestCase):
    def test_one_sentence(self):
        s = TwoPartSegmenter()
        self.assertEqual(s.push("Четыре."), ["Четыре."])
        self.assertEqual(s.finish(), [])

    def test_two_requests_for_many_sentences(self):
        s = TwoPartSegmenter()
        self.assertEqual(s.push("Первое"), [])
        self.assertEqual(s.push(" предложение. Второе"), ["Первое предложение."])
        self.assertEqual(s.push(" предложение. Третье!"), [])
        self.assertEqual(s.finish(), ["Второе предложение. Третье!"])

    def test_without_punctuation(self):
        s = TwoPartSegmenter()
        self.assertEqual(s.push("Ответ без точки"), [])
        self.assertEqual(s.finish(), ["Ответ без точки"])

    def test_remainder_synthesis_starts_before_first_is_ready(self):
        incoming = queue.Queue()
        gate = threading.Event()
        gate.set()
        first_started = threading.Event()
        second_started = threading.Event()
        release_first = threading.Event()
        pcm = []

        async def synth(text, path):
            if text == "Первое.":
                first_started.set()
                while not release_first.is_set():
                    await asyncio.sleep(0.01)
            else:
                second_started.set()
            with open(path, "wb") as f:
                f.write(text.encode("utf8"))

        class Sink:
            def write(self, data):
                pcm.append(data)

            def close(self):
                pass

        class Player:
            def __init__(self, *a, **kw):
                self.stdin = Sink()

            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0

        def decode(args, **kw):
            with open(args[-1], "rb") as f:
                text = f.read().decode("utf8")
            with wave.open(args[-2], "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(24000)
                w.writeframes(bytes([1 if text.startswith("Первое") else 2, 0]) * 100)

        controller = types.ModuleType("alice_for_marusia.audio")
        controller.synthesize = synth
        with tempfile.TemporaryDirectory() as root, patch.dict(
            "sys.modules", {"alice_for_marusia.audio": controller}
        ), patch("alice_for_marusia.speech.os.setsid", create=True), patch(
            "alice_for_marusia.speech.subprocess.run", side_effect=decode
        ), patch(
            "alice_for_marusia.speech.subprocess.Popen", Player
        ):
            worker = threading.Thread(
                target=speech_worker, args=(incoming, gate, root, root, {})
            )
            worker.start()
            incoming.put(("delta", "Первое. Остальная"))
            self.assertTrue(first_started.wait(2))
            incoming.put(("delta", " часть ответа."))
            incoming.put(("done", None))
            try:
                self.assertTrue(
                    second_started.wait(2),
                    "second request must not wait for first synthesis",
                )
            finally:
                release_first.set()
            worker.join(3)
            self.assertFalse(worker.is_alive())
        self.assertEqual([block[0] for block in pcm], [1, 2])


if __name__ == "__main__":
    unittest.main()
