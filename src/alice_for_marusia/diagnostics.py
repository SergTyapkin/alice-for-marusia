"""Small persistent event log and reversible mute of stock assistant streams."""

import json, logging, os, re, subprocess, threading, time
from logging.handlers import RotatingFileHandler

_logger = None


def event(*args):
    global _logger
    print(*args, flush=True)
    try:
        if _logger is None:
            _logger = logging.getLogger("alisa-events")
            _logger.setLevel(logging.INFO)
            handler = RotatingFileHandler(
                "/data/alisa/events.log",
                maxBytes=262144,
                backupCount=2,
                encoding="utf8",
            )
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            _logger.addHandler(handler)
        safe = (
            args
            if not args or args[0] not in ("TRANSCRIPT", "ANSWER")
            else (args[0], "[text omitted]")
        )
        _logger.info("uptime=%.2f %s", time.monotonic(), " ".join(map(str, safe)))
    except OSError:
        pass


class MarusyaMute:
    def __init__(self, path="/data/alisa/mute-state.json"):
        self.path = path
        self.env = os.environ.copy()
        self.env.pop("LD_LIBRARY_PATH", None)
        self.condition = threading.Condition()
        self.active = False
        self.exiting = False
        self.saved = {}
        with open("/proc/sys/kernel/random/boot_id") as f:
            self.boot = f.read().strip()
        try:
            with open(path) as f:
                old = json.load(f)
            if old.get("boot") == self.boot:
                self.saved = old.get("streams", {})
        except (OSError, ValueError):
            pass
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def call(self, *args):
        return subprocess.run(
            ["pactl", *args], env=self.env, check=True, capture_output=True, timeout=2
        ).stdout.decode("utf8", "replace")

    def streams(self):
        found = {}
        for section in re.split(
            r"(?=Sink Input #\d+)", self.call("list", "sink-inputs")
        ):
            if 'application.process.binary = "voice-agent"' not in section:
                continue
            index = re.search(r"Sink Input #(\d+)", section)
            pid = re.search(r'application.process.id = "(\d+)"', section)
            if index and pid:
                found[index[1]] = {
                    "pid": pid[1],
                    "mute": bool(re.search(r"\n\s*Mute: yes", section)),
                }
        return found

    def persist(self):
        temporary = self.path + ".new"
        with open(temporary, "w") as f:
            json.dump({"boot": self.boot, "streams": self.saved}, f)
        os.replace(temporary, self.path)

    def set_active(self, value):
        with self.condition:
            if self.active != value:
                self.active = value
                self.condition.notify()

    def refresh(self, active):
        current = self.streams()
        changed = False
        if active:
            for index, state in current.items():
                if index not in self.saved or self.saved[index]["pid"] != state["pid"]:
                    self.saved[index] = state
                    changed = True
                    self.persist()  # Save restoration state before changing audio.
                if not state["mute"]:
                    self.call("set-sink-input-mute", index, "1")
        else:
            for index, state in self.saved.items():
                if index in current and current[index]["pid"] == state["pid"]:
                    self.call(
                        "set-sink-input-mute", index, "1" if state["mute"] else "0"
                    )
            if self.saved:
                self.saved = {}
                changed = True
        if changed:
            self.persist()

    def run(self):
        while True:
            with self.condition:
                active = self.active
                exiting = self.exiting
            try:
                self.refresh(active and not exiting)
            except (OSError, subprocess.SubprocessError, ValueError) as e:
                event("MUTE_ERROR", type(e).__name__)
            if exiting:
                return
            with self.condition:
                self.condition.wait(0.15)

    def close(self):
        with self.condition:
            self.active = False
            self.exiting = True
            self.condition.notify()
        self.thread.join(timeout=5)
