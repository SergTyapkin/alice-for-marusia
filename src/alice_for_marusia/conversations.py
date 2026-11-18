"""Persistent named conversations, with conservative request-time summarisation."""

import json, os, re, tempfile, time


class Dialogs:
    def __init__(self, path="/data/alisa/dialogs.json", limit=16000):
        self.path, self.limit = path, limit
        try:
            with open(path, encoding="utf8") as f:
                self.data = json.load(f)
        except FileNotFoundError:
            self.data = {"current": "основной", "dialogs": {"основной": []}}

    def maintain(self, now=None):
        now = time.time() if now is None else now
        dates = self.data.setdefault("updated_at", {})
        for name in self.data["dialogs"]:
            dates.setdefault(name, now)
        dates[self.data["current"]] = now
        for name in list(self.data["dialogs"]):
            if (
                len(self.data["dialogs"]) > 1
                and name != self.data["current"]
                and now - dates[name] > 7 * 86400
            ):
                del self.data["dialogs"][name]
                dates.pop(name, None)
        self.save()

    def save(self):
        fd, name = tempfile.mkstemp(dir=os.path.dirname(self.path), prefix=".dialogs-")
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf8") as f:
                json.dump(self.data, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def reload(self):
        try:
            with open(self.path, encoding="utf8") as f:
                self.data = json.load(f)
        except FileNotFoundError:
            pass

    def commit(self, staged, expected):
        # A web edit during generation must not resurrect a deleted conversation
        # or overwrite a newly selected one.
        from .settings import LOCK

        with LOCK:
            self.reload()
            if self.data != expected:
                return False
            self.data = staged
            self.save()
            return True

    def command(self, text):
        text = text.strip().lower().strip(" .!?")
        if text in ("следующий диалог", "предыдущий диалог"):
            names = list(self.data["dialogs"])
            index = names.index(self.data["current"]) + (
                1 if text.startswith("следующий") else -1
            )
            if not 0 <= index < len(names):
                return "Такого диалога нет."
            self.data["current"] = names[index]
            self.save()
            return "Переключаюсь на диалог " + str(index + 1) + "."
        if re.fullmatch(r"(?:создай |начни )?новый диалог", text):
            number = 1
            while "диалог " + str(number) in self.data["dialogs"]:
                number += 1
            name = "диалог " + str(number)
        else:
            m = re.fullmatch(
                r"(?:переключи|открой|выбери) диалог (?:на |под названием )?(.+)", text
            )
            if not m:
                return None
            name = m.group(1).strip()[:80]
        self.data["current"] = name
        self.data["dialogs"].setdefault(name, [])
        self.save()
        return (
            "Переключаюсь на диалог "
            + str(list(self.data["dialogs"]).index(name) + 1)
            + "."
        )

    def ask(self, session, text, on_delta=None):
        name = self.data["current"]
        history = self.data["dialogs"][name]
        options = {"history": history, "search": True}
        if on_delta is not None:
            options["on_delta"] = on_delta
        result = session.ask(text, **options)
        history.extend(
            [
                {"role": "user", "content": text},
                {"role": "assistant", "content": result["text"]},
            ]
        )
        self.save()
        return result["text"]

    def compact(self, session):
        name = self.data["current"]
        history = self.data["dialogs"][name]
        # Conservative character budget, not a claim about exact model token count.
        if sum(len(x["content"]) for x in history) > self.limit and len(history) > 4:
            old, recent = history[:-4], history[-4:]
            transcript = "\n".join(x["role"] + ": " + x["content"] for x in old)
            summary = session.ask(
                "Кратко сохрани важные факты, предпочтения и незавершённые задачи из этого диалога. Не отвечай на него заново:\n"
                + transcript,
                history=(),
                search=False,
            )["text"]
            history = [
                {
                    "role": "user",
                    "content": "Краткая память предыдущей части диалога: " + summary,
                },
                {"role": "assistant", "content": "Поняла."},
            ] + recent
            self.data["dialogs"][name] = history
            self.save()
