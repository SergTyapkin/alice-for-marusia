"""Cached prompts and Russian speech synthesis for the speaker."""

import asyncio, os, subprocess

ROOT = os.environ.get("ALISA_ROOT", "/tmp/alisa-runtime")
MPG = ROOT + "/mpg"
ENV = os.environ.copy()
ENV.pop("LD_LIBRARY_PATH", None)
ENV["SSL_CERT_FILE"] = "/etc/ssl/certs/ca-certificates.crt"


async def synthesize(text, path):
    import edge_tts

    await edge_tts.Communicate(text, "ru-RU-SvetlanaNeural").save(path)


PHRASES = {
    "Слушаю.": "listen",
    "Не расслышала. Повторите, пожалуйста.": "no_speech",
    "Не удалось распознать речь. Проверьте подключение к интернету.": "stt_error",
    "Не получилось связаться с ChatGPT.": "gpt_error",
    "Пожалуйста, попросите об этом Марусю напрямую.": "marusya",
}


def say(text):
    cached = PHRASES.get(text)
    mp3 = ROOT + "/phrases/" + cached + ".mp3" if cached else ROOT + "/speech.mp3"
    wav = ROOT + "/speech.wav"
    temporary = not cached
    try:
        if cached and (not os.path.exists(mp3) or not os.path.getsize(mp3)):
            mp3 = ROOT + "/phrases/stt_error.mp3"
        elif not cached:
            asyncio.run(synthesize(text, mp3))
        env = ENV.copy()
        env["LD_LIBRARY_PATH"] = MPG + "/usr/lib/aarch64-linux-gnu"
        subprocess.run(
            [MPG + "/usr/bin/mpg123.bin", "-q", "-e", "s16", "-w", wav, mp3],
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        subprocess.run(
            ["paplay", "--device=master", wav],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=45,
        )
    finally:
        for f in ((mp3, wav) if temporary else (wav,)):
            try:
                os.unlink(f)
            except FileNotFoundError:
                pass
