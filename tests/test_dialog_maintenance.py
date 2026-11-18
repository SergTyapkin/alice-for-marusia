import unittest
from alice_for_marusia.conversations import Dialogs


class MemoryDialogs(Dialogs):
    def save(self):
        pass


def store(data):
    d = MemoryDialogs.__new__(MemoryDialogs)
    d.data = data
    d.limit = 50
    return d


class MaintenanceTests(unittest.TestCase):
    def test_prunes_old_inactive_but_keeps_current_and_recent(self):
        d = store(
            {
                "current": "a",
                "dialogs": {"a": [], "b": [], "c": []},
                "updated_at": {"a": 0, "b": 0, "c": 700000},
            }
        )
        d.maintain(800000)
        self.assertEqual(list(d.data["dialogs"]), ["a", "c"])

    def test_keeps_single_and_migrates_unknown_dates(self):
        d = store({"current": "a", "dialogs": {"a": [], "b": []}})
        d.maintain(800000)
        self.assertEqual(len(d.data["dialogs"]), 2)
        d = store({"current": "a", "dialogs": {"a": []}, "updated_at": {"a": 0}})
        d.maintain(800000)
        self.assertEqual(len(d.data["dialogs"]), 1)

    def test_answer_does_not_wait_for_compaction(self):
        class Session:
            calls = []

            def ask(self, text, **kw):
                self.calls.append(text)
                return {"text": "ответ"}

        d = store(
            {
                "current": "a",
                "dialogs": {
                    "a": [
                        {
                            "role": "user" if i % 2 == 0 else "assistant",
                            "content": "x" * 30,
                        }
                        for i in range(8)
                    ]
                },
            }
        )
        s = Session()
        self.assertEqual(d.ask(s, "вопрос"), "ответ")
        self.assertEqual(s.calls, ["вопрос"])
        d.compact(s)
        self.assertEqual(len(s.calls), 2)


if __name__ == "__main__":
    unittest.main()
