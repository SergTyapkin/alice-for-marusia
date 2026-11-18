"""Cached network/account checks and local-only onboarding announcements."""

import os, pathlib, socket, threading, time, urllib.request
from .settings import DATA

PROMPTS = {
    "offline": "Подключите колонку к интернету так, как подсказывает Маруся.",
    "login": "Подключитесь к сети, к которой подключена колонка, и подключите аккаунт Chat GPT в браузере по адресу эйч ти ти пи двоеточие слэш слэш",
    "vpn": "ChatGPT не отвечает. Кажется, проблема с VPN. Настройте по адресу эйч ти ти пи двоеточие слэш слэш",
}


def local_ip():
    # UDP connect chooses a local route without sending a packet.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("1.1.1.1", 53))
            return s.getsockname()[0]
    except OSError:
        return None


def internet_available():
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for url, expected in [
        ("https://detectportal.firefox.com/success.txt", b"success"),
        ("http://www.msftconnecttest.com/connecttest.txt", b"Microsoft Connect Test"),
    ]:
        try:
            with direct.open(url, timeout=2) as r:
                if expected in r.read(128):
                    return True
        except OSError:
            pass
    return False


class Readiness:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = "checking"
        self.checked = 0
        self.refresh = threading.Event()
        self.closed = threading.Event()
        self.inflight = False
        self.port = 100

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def invalidate(self):
        with self.lock:
            self.state = "checking"
        self.refresh.set()

    def snapshot(self):
        with self.lock:
            return self.snapshot_unlocked()

    def check(self, actual_request=False):
        with self.lock:
            if self.inflight:
                return self.snapshot_unlocked()
            self.inflight = True
        try:
            if not internet_available():
                value = "offline"
            elif not (DATA / "auth.json").exists():
                value = "login"
            else:
                from .openai_client import PlanSession

                try:
                    session = PlanSession()
                    if actual_request:
                        session.ask("Ответь одним словом: готово.", search=False)
                    else:
                        session.models()
                    value = "ready"
                except Exception as e:
                    value = (
                        "login"
                        if "401" in str(e) or "invalid_grant" in str(e)
                        else "vpn"
                    )
            with self.lock:
                self.state = value
                self.checked = time.time()
        finally:
            with self.lock:
                self.inflight = False
        return self.snapshot()

    def snapshot_unlocked(self):
        return {
            "state": self.state,
            "checked": self.checked,
            "ip": local_ip(),
            "port": self.port,
        }

    def run(self):
        while not self.closed.is_set():
            try:
                self.check()
            except Exception:
                with self.lock:
                    self.state = "vpn"
                    self.checked = time.time()
            self.refresh.wait(30)
            self.refresh.clear()


def play_setup(kind, ip, root, mpg, env, guard):
    """Speak a cached prefix while synthesising the IP. No online TTS for errors."""
    import subprocess
    from .audio import synthesize
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    os.setsid()
    guard.value = time.monotonic() + 60
    cache = DATA / "phrases-cache"
    cache.mkdir(exist_ok=True)

    def play(path):
        if str(path).endswith(".wav"):
            subprocess.run(
                ["paplay", "--device=master", str(path)],
                env=env,
                check=True,
                timeout=60,
            )
            return
        wav = str(path) + ".%d.wav" % os.getpid()
        e = env.copy()
        e["LD_LIBRARY_PATH"] = mpg + "/usr/lib/aarch64-linux-gnu"
        try:
            subprocess.run(
                [mpg + "/usr/bin/mpg123.bin", "-q", "-e", "s16", "-w", wav, str(path)],
                env=e,
                check=True,
                timeout=20,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["paplay", "--device=master", wav], env=env, check=True, timeout=60
            )
        finally:
            try:
                os.unlink(wav)
            except FileNotFoundError:
                pass

    def ip_audio():
        if not ip:
            return None
        path = cache / ("address-" + ip.replace(".", "-") + ".mp3")
        if not path.exists():
            try:

                async def make():
                    await asyncio.wait_for(
                        synthesize(
                            ip.replace(".", " точка ").replace(":", " двоеточие "),
                            str(path) + ".new",
                        ),
                        8,
                    )

                asyncio.run(make())
                os.replace(str(path) + ".new", path)
                for stale in cache.glob("address-*.mp3"):
                    if stale != path:
                        stale.unlink(missing_ok=True)
            except Exception:
                pathlib.Path(str(path) + ".new").unlink(missing_ok=True)
                return None
        return path

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            address = executor.submit(ip_audio) if kind != "offline" else None
            prefix = cache / ("setup-" + kind + ".mp3")
            if prefix.exists():
                play(prefix)
            else:
                error = DATA / "assets/error.mp3"
                play(error if error.exists() else DATA / "assets/error.wav")
            if address:
                path = address.result()
                if path:
                    play(path)
                elif ip:
                    # A VPN failure can also prevent online speech synthesis.
                    # Keep a completely offline digit-by-digit fallback.
                    for word in ip:
                        path = cache / (
                            "digit-"
                            + ({".": "dot", ":": "colon"}.get(word, word))
                            + ".mp3"
                        )
                        if path.exists():
                            play(path)
    finally:
        guard.value = time.monotonic() + 1
