"""Generate redistributable tones and cache spoken setup prompts. No playback."""

import argparse, array, asyncio, math, pathlib, sys, wave

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from alice_for_marusia.network import PROMPTS


def tone(path, notes):
    samples = array.array("h")
    rate = 16000
    for frequency, duration in notes:
        size = int(duration * rate)
        for i in range(size):
            envelope = min(1, i / (rate * 0.012), (size - i) / (rate * 0.025))
            samples.append(
                int(7000 * envelope * math.sin(2 * math.pi * frequency * i / rate))
            )
    if sys.byteorder != "little":
        samples.byteswap()
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(samples.tobytes())


async def prepare(destination):
    import edge_tts

    assets = destination / "assets"
    phrases = destination / "phrases-cache"
    assets.mkdir(parents=True, exist_ok=True)
    phrases.mkdir(parents=True, exist_ok=True)
    for name, notes in {
        "wake": [(880, 0.09), (1174, 0.12)],
        "ready": [(523, 0.10), (659, 0.10), (784, 0.18)],
        "error": [(440, 0.12), (330, 0.20)],
    }.items():
        tone(assets / (name + ".wav"), notes)
    texts = {"thinking": "Думаю.", "no_dialog": "Такого диалога нет."}
    texts.update({"setup-" + k: v for k, v in PROMPTS.items()})
    texts.update({"digit-" + str(i): str(i) for i in range(10)})
    texts["digit-dot"] = "точка"
    texts["digit-colon"] = "двоеточие"
    texts.update(
        {
            "switch-dialog-" + str(i): "Переключаюсь на диалог %d." % i
            for i in range(1, 21)
        }
    )
    for key, text in texts.items():
        path = phrases / (key + ".mp3")
        if path.exists() and path.stat().st_size:
            continue
        await asyncio.wait_for(
            edge_tts.Communicate(text, "ru-RU-SvetlanaNeural").save(str(path) + ".new"),
            30,
        )
        pathlib.Path(str(path) + ".new").replace(path)
        print("Cached", key, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=pathlib.Path, default=ROOT / "build/assets")
    args = parser.parse_args()
    asyncio.run(prepare(args.output))
