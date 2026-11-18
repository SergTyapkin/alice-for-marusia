"""Local dialogue-end and touch recognition; no network or device access."""

import re

END_PHRASES = (
    r"выключи(?:сь| тебя)",
    r"замолчи",
    r"помолчи",
    r"хватит(?: говорить)?",
    r"достаточно",
    r"закончи(?:м|ть)? (?:разговор|диалог|общение)",
    r"закончим",
    r"(?:я )?не хочу больше (?:с тобой )?(?:говорить|разговаривать|общаться)",
    r"(?:мне )?больше не нужно",
    r"останови(?:сь| разговор)",
    r"прекрати(?: говорить| разговор)?",
    r"отбой",
    r"до свидания",
    r"пока",
    r"спасибо (?:вс[её]|хватит|выключись|замолчи)",
    r"на этом вс[её](?: спасибо)?",
    r"вс[её] (?:спасибо|выключись|замолчи)",
    r"вс[её]",
    r"стоп",
    r"спасибо",
)
END_RE = re.compile(
    r"^(?:ну |ладно |хорошо |алиса )?(?:" + "|".join(END_PHRASES) + r")[\s.!?,]*$", re.I
)

# Explicit stop instructions are recognised anywhere in an utterance. Common
# words such as "всё", "пока" or "спасибо" remain whole-utterance commands.
INLINE_END_PHRASES = (
    r"выключись",
    r"замолчи",
    r"помолчи",
    r"не отвечай",
    r"не (?:надо|нужно) отвечать",
    r"(?:хватит|прекрати|перестань) (?:говорить|отвечать)",
    r"(?:закончи|закончим|закончить|закрой|прекрати|останови) (?:разговор|диалог|общение)",
    r"не хочу больше (?:с тобой )?(?:говорить|разговаривать|общаться)",
)
INLINE_END_RE = re.compile(
    r"(?<!\w)(?:" + "|".join(INLINE_END_PHRASES) + r")(?!\w)", re.I
)


def ends_dialogue(text):
    normalized = re.sub(r"[,;:]+", " ", text.strip())
    normalized = re.sub(r"\s+", " ", normalized)
    return bool(END_RE.fullmatch(normalized) or INLINE_END_RE.search(normalized))


def no_speech_timeout(heard, elapsed, limit):
    return not heard and elapsed >= limit


class ShortTouch:
    def __init__(self):
        self.down = None
        self.start_pos = None
        self.pos = [None, None]
        self.moved = False

    def event(self, typ, code, value, stamp):
        if typ == 3 and code in (0x35, 0x36):
            axis = code - 0x35
            self.pos[axis] = value
            if self.down is not None:
                if self.start_pos is None and None not in self.pos:
                    self.start_pos = tuple(self.pos)
                elif self.start_pos and abs(value - self.start_pos[axis]) > 30:
                    self.moved = True
        if typ == 1 and code == 0x14A:
            if value == 1:
                self.down = stamp
                self.start_pos = None
                self.pos = [None, None]
                self.moved = False
            elif value == 0 and self.down is not None:
                short = not self.moved and 0 <= stamp - self.down <= 0.65
                self.down = None
                return short
        return False
