import importlib, queue, sys, types, unittest
from unittest.mock import patch

# The real recognizer and audio interface are available only on the speaker.
fake_sherpa = types.ModuleType("alice_for_marusia.recognizer")
for (
    name
) in "cfg create stream accept ready decode finish result destroy_result drop_stream drop_rec F".split():
    setattr(fake_sherpa, name, None)
fake_controller = types.ModuleType("alice_for_marusia.audio")
fake_controller.ROOT = "/tmp/alisa-runtime"
fake_controller.MPG = "/tmp/mpg"
fake_controller.say = lambda text: None
fake_controller.PHRASES = {}
sys.modules.setdefault("audioop", types.ModuleType("audioop"))
with patch.dict(
    sys.modules,
    {
        "alice_for_marusia.recognizer": fake_sherpa,
        "alice_for_marusia.audio": fake_controller,
    },
):
    target = importlib.import_module("alice_for_marusia.app")


class Session:
    def ask(self, text, history=(), **kw):
        if kw.get("on_delta"):
            kw["on_delta"]("от")
            kw["on_delta"]("вет")
        return {"text": "ответ"}


class WorkerTests(unittest.TestCase):
    def test_request_stages_history_without_modifying_live_snapshot(self):
        data = {"current": "основной", "dialogs": {"основной": []}}
        out = queue.Queue()
        with patch.object(target.os, "setsid", create=True), patch.object(
            target, "PlanSession", Session
        ):
            target.request_worker(data, "тест", out)
        kind, answer, staged = out.get_nowait()
        self.assertEqual((kind, answer), ("ok", "ответ"))
        self.assertEqual(data["dialogs"]["основной"], [])
        self.assertEqual(len(staged["dialogs"]["основной"]), 2)

    def test_deltas_reach_speech_queue_before_completion(self):
        data = {"current": "основной", "dialogs": {"основной": []}}
        out = queue.Queue()
        speech = queue.Queue()
        with patch.object(target.os, "setsid", create=True), patch.object(
            target, "PlanSession", Session
        ):
            target.request_worker(data, "тест", out, speech)
        self.assertEqual(
            [speech.get_nowait(), speech.get_nowait(), speech.get_nowait()],
            [("delta", "от"), ("delta", "вет"), ("done", None)],
        )
        self.assertEqual(out.get_nowait()[0], "ok")
        self.assertEqual(data["dialogs"]["основной"], [])


if __name__ == "__main__":
    unittest.main()
