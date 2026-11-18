"""Feed PulseAudio into a lightweight wake detector through ALSA loopback."""

import os, select, subprocess, time, threading, json
from .diagnostics import event
from .settings import settings


def masked_pcm(block, guard, stamp):
    return bytes(len(block)) if stamp < guard.value else block


def monitor_wake(events, healthy, stop, root, base_env, guard):
    loader = root + "/glibc/lib/aarch64-linux-gnu/ld-linux-aarch64.so.1"
    libs = (
        root
        + "/glibc/usr/lib/aarch64-linux-gnu:"
        + root
        + "/glibc/lib/aarch64-linux-gnu:/lib:/usr/lib"
    )
    env = base_env.copy()
    env.update(
        {
            "HOME": root,
            "ALSA_PLUGIN_DIR": root + "/plugins",
            "XDG_CACHE_HOME": root + "/cache",
        }
    )
    while not stop.is_set():
        audio = writer = detector = None
        forwarding_stop = threading.Event()
        try:
            threshold = settings()["wake_threshold"]
            config_path = root + "/alisa_mww.json"
            with open(config_path, encoding="utf8") as f:
                config = json.load(f)
            config["micro"]["probability_cutoff"] = threshold / 100
            with open(config_path, "w", encoding="utf8") as f:
                json.dump(config, f, ensure_ascii=False)
            audio = subprocess.Popen(
                [
                    "parec",
                    "-d",
                    "speex",
                    "--raw",
                    "--format=s16le",
                    "--rate=16000",
                    "--channels=1",
                    "--latency-msec=60",
                    "--process-time-msec=20",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=base_env,
            )
            writer = subprocess.Popen(
                [
                    "aplay",
                    "-q",
                    "-D",
                    "hw:7,1",
                    "-f",
                    "S16_LE",
                    "-r",
                    "16000",
                    "-c",
                    "1",
                    "-t",
                    "raw",
                    "--buffer-time=80000",
                    "--period-time=20000",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=base_env,
                bufsize=0,
            )
            forwarding_stop = threading.Event()

            def forward(source=audio, destination=writer, done=forwarding_stop):
                blocked = False
                try:
                    while not stop.is_set() and not done.is_set():
                        block = source.stdout.read(640)
                        if not block:
                            break
                        stamp = time.monotonic()
                        masking = stamp < guard.value
                        if masking != blocked:
                            event("KWS_MASK_BEGIN" if masking else "KWS_MASK_END")
                            blocked = masking
                        destination.stdin.write(masked_pcm(block, guard, stamp))
                except (OSError, BrokenPipeError):
                    pass

            threading.Thread(target=forward, daemon=True).start()
            detector = subprocess.Popen(
                [
                    loader,
                    "--library-path",
                    libs,
                    root + "/wake",
                    root + "/alisa_mww.json",
                    "--device",
                    "2",
                ],
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                bufsize=0,
            )
            buffer = b""
            while (
                not stop.is_set()
                and detector.poll() is None
                and writer.poll() is None
                and audio.poll() is None
            ):
                if settings()["wake_threshold"] != threshold:
                    event("KWS_RECONFIGURE")
                    break
                if not select.select([detector.stdout], [], [], 0.2)[0]:
                    continue
                data = os.read(detector.stdout.fileno(), 4096)
                if not data:
                    break
                buffer += data
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if b"Listening for" in line:
                        healthy.set()
                        event("KWS_READY")
                    elif b"Detected " in line:
                        stamp = time.monotonic()
                        if stamp < guard.value:
                            event(
                                "KWS_CUE_SUPPRESSED",
                                line.decode("utf8", "replace").strip(),
                            )
                        else:
                            event(
                                "KWS_DETECTED", line.decode("utf8", "replace").strip()
                            )
                            events.put(("wake", stamp))
            event("KWS_STOPPED")
        except OSError as e:
            print("KWS_ERROR", type(e).__name__, flush=True)
        finally:
            healthy.clear()
            if audio is not None:
                forwarding_stop.set()
            for p in (detector, writer, audio):
                if p and p.poll() is None:
                    p.terminate()
            for p in (detector, writer, audio):
                if p:
                    try:
                        p.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        p.wait()
        stop.wait(0.2)
