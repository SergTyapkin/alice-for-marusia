"""Synthesize the first sentence and, at completion, the entire remainder."""

import asyncio, os, queue, re, subprocess, tempfile, threading, time, wave


class TwoPartSegmenter:
    def __init__(self):
        self.buffer = ""
        self.first_sent = False
        self.done = False

    def push(self, text):
        if self.done:
            raise ValueError("response is already complete")
        self.buffer += text
        if self.first_sent:
            return []
        boundary = re.search(r"[.!?]+[»”\"\']*(?=\s|$)", self.buffer)
        if not boundary:
            return []
        first = self.buffer[: boundary.end()].strip()
        self.buffer = self.buffer[boundary.end() :]
        self.first_sent = True
        return [first] if first else []

    def finish(self):
        if self.done:
            raise ValueError("response is already complete")
        self.done = True
        tail = self.buffer.strip()
        self.buffer = ""
        return [tail] if tail else []


def speech_worker(incoming, gate, root, mpg, env, activity=None):
    """Start each synthesis immediately; play the resulting files in order."""
    os.setsid()
    from .audio import synthesize

    tasks = queue.Queue()
    end = object()
    temp = tempfile.TemporaryDirectory(prefix="alice-stream-", dir=root)

    def prepare():
        segmenter = TwoPartSegmenter()
        index = 0

        def submit(text):
            nonlocal index
            index += 1
            task = {
                "path": os.path.join(temp.name, "%02d.mp3" % index),
                "ready": threading.Event(),
                "error": None,
            }

            def synth():
                try:
                    print(
                        "TTS_START",
                        index_for_log,
                        round(time.monotonic(), 3),
                        flush=True,
                    )
                    asyncio.run(synthesize(text, task["path"]))
                    print(
                        "TTS_READY",
                        index_for_log,
                        round(time.monotonic(), 3),
                        flush=True,
                    )
                except Exception as e:
                    task["error"] = type(e).__name__
                finally:
                    task["ready"].set()

            index_for_log = index
            threading.Thread(target=synth, daemon=True).start()
            tasks.put(task)

        try:
            while True:
                typ, value = incoming.get()
                if typ == "cancel":
                    break
                if typ == "delta":
                    chunks = segmenter.push(value)
                elif typ == "done":
                    chunks = segmenter.finish()
                else:
                    continue
                for chunk in chunks:
                    submit(chunk)
                if typ == "done":
                    break
        except Exception as e:
            tasks.put({"error": type(e).__name__, "ready": None})
        finally:
            tasks.put(end)

    threading.Thread(target=prepare, daemon=True).start()
    playback = None
    try:
        while True:
            task = tasks.get()
            if task is end:
                break
            if task["ready"] is not None:
                task["ready"].wait()
            if task["error"]:
                raise RuntimeError("speech synthesis failed: " + task["error"])
            path = task["path"]
            wav = path + ".wav"
            decode_env = env.copy()
            decode_env["LD_LIBRARY_PATH"] = mpg + "/usr/lib/aarch64-linux-gnu"
            subprocess.run(
                [
                    mpg + "/usr/bin/mpg123.bin",
                    "-q",
                    "-m",
                    "-r",
                    "24000",
                    "-e",
                    "s16",
                    "-w",
                    wav,
                    path,
                ],
                env=decode_env,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
            gate.wait()
            if playback is None:
                playback = subprocess.Popen(
                    [
                        "pacat",
                        "--playback",
                        "--device=master",
                        "--raw",
                        "--format=s16le",
                        "--rate=24000",
                        "--channels=1",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=env,
                )
                if activity is not None:
                    activity.set()
            with wave.open(wav, "rb") as decoded:
                if (
                    decoded.getnchannels(),
                    decoded.getsampwidth(),
                    decoded.getframerate(),
                ) != (1, 2, 24000):
                    raise RuntimeError("unexpected audio format")
                print("TTS_PLAY", round(time.monotonic(), 3), flush=True)
                while True:
                    pcm = decoded.readframes(4096)
                    if not pcm:
                        break
                    playback.stdin.write(pcm)
            os.unlink(path)
            os.unlink(wav)
        if playback:
            playback.stdin.close()
            if playback.wait(timeout=60):
                raise RuntimeError("audio playback failed")
    finally:
        if playback and playback.poll() is None:
            playback.terminate()
            playback.wait(timeout=3)
        if activity is not None:
            activity.clear()
        temp.cleanup()
