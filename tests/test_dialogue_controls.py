import unittest
from alice_for_marusia.controls import ends_dialogue, no_speech_timeout, ShortTouch


class ControlsTests(unittest.TestCase):
    def test_no_speech_timeout(self):
        self.assertFalse(no_speech_timeout(False, 1.99, 2))
        self.assertTrue(no_speech_timeout(False, 2.0, 2))
        self.assertFalse(no_speech_timeout(True, 3.0, 2))

    def test_end_commands(self):
        for phrase in (
            "выключись",
            "замолчи",
            "я не хочу больше говорить",
            "достаточно",
            "всё",
            "спасибо, всё",
            "закончи диалог",
            "на этом всё",
        ):
            with self.subTest(phrase=phrase):
                self.assertTrue(ends_dialogue(phrase))

    def test_regular_questions_are_not_stops(self):
        for phrase in (
            "почему всё так сложно",
            "расскажи про стоп сигнал",
            "как отключить свет",
            "спасибо за ответ расскажи ещё",
            "как называется фильм пока ты спал",
        ):
            with self.subTest(phrase=phrase):
                self.assertFalse(ends_dialogue(phrase))

    def test_explicit_instructions_anywhere(self):
        for phrase in (
            "я передумал не отвечай на мой вопрос",
            "ладно пожалуйста выключись уже",
            "да хватит говорить мне это",
            "спасибо больше не нужно отвечать",
            "я не хочу больше с тобой разговаривать сегодня",
            "пожалуйста закрой диалог сейчас",
        ):
            with self.subTest(phrase=phrase):
                self.assertTrue(ends_dialogue(phrase))

    def test_word_boundaries(self):
        for phrase in (
            "расскажи о слове отвечай",
            "слово замолчите означает просьбу",
            "не выключайся расскажи ещё",
        ):
            with self.subTest(phrase=phrase):
                self.assertFalse(ends_dialogue(phrase))

    def test_short_touch(self):
        touch = ShortTouch()
        touch.event(1, 0x14A, 1, 10)
        touch.event(3, 0x35, 280, 10.01)
        touch.event(3, 0x36, 350, 10.02)
        self.assertTrue(touch.event(1, 0x14A, 0, 10.2))

    def test_swipe_and_long_press_not_touch(self):
        touch = ShortTouch()
        touch.event(1, 0x14A, 1, 10)
        touch.event(3, 0x35, 280, 10.01)
        touch.event(3, 0x36, 350, 10.02)
        touch.event(3, 0x35, 400, 10.1)
        self.assertFalse(touch.event(1, 0x14A, 0, 10.2))
        touch.event(1, 0x14A, 1, 11)
        self.assertFalse(touch.event(1, 0x14A, 0, 12))


if __name__ == "__main__":
    unittest.main()
