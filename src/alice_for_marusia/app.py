"""Continuous microphone, cancellable work and touch input for Capsula."""

import array, audioop, copy, ctypes as c, json, multiprocessing as mp
import os, queue, re, select, signal, struct, subprocess, threading, time
from .recognizer import (
    cfg,
    create,
    stream,
    accept,
    ready,
    decode,
    finish,
    result,
    destroy_result,
    drop_stream,
    drop_rec,
    F,
)
from .audio import ROOT, MPG
from .controls import ends_dialogue, no_speech_timeout, ShortTouch
from .conversations import Dialogs
from .speech import speech_worker
from .workers import (
    play_file,
    play_activation,
    play_text,
    request_worker,
    compact_worker,
)

# Fresh interpreters avoid reserving the recognizer heap for every child.
mp = mp.get_context("spawn")
from .indicators import IndicatorWorker
from .wake import monitor_wake
from .diagnostics import event, MarusyaMute

ENV = os.environ.copy()
ENV.pop("LD_LIBRARY_PATH", None)
ASSETS = "/data/alisa/assets"


def asset_path(name):
    mp3 = ASSETS + "/" + name + ".mp3"
    return mp3 if os.path.exists(mp3) else ASSETS + "/" + name + ".wav"


def log(*args):
    event(*args)


def stop_process(p):
    if not p:
        return
    if p.is_alive():
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            p.terminate()
        p.join(0.5)
        if p.is_alive():
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                p.kill()
    p.join(timeout=0.5)


def monitor_touch(events, stop, touch_down):
    try:
        fd = os.open("/dev/input/event0", os.O_RDONLY | os.O_NONBLOCK)
    except OSError as e:
        log("TOUCH_UNAVAILABLE", type(e).__name__)
        return
    event = struct.Struct("llHHi")
    touch = ShortTouch()
    buf = b""
    try:
        while not stop.is_set():
            if not select.select([fd], [], [], 0.3)[0]:
                continue
            buf += os.read(fd, event.size * 32)
            while len(buf) >= event.size:
                raw, buf = buf[: event.size], buf[event.size :]
                sec, usec, typ, code, value = event.unpack(raw)
                if typ == 1 and code == 0x14A and value == 1:
                    touch_down.set()
                if touch.event(typ, code, value, time.monotonic()):
                    events.put(("touch", None))
                if typ == 1 and code == 0x14A and value == 0:
                    touch_down.clear()
    except OSError as e:
        log("TOUCH_ERROR", type(e).__name__)
    finally:
        touch_down.clear()
        os.close(fd)


def monitor_microphone(events, stop, indicator):
    proc = subprocess.Popen(
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
        env=ENV,
    )
    try:
        while not stop.is_set():
            block = proc.stdout.read(640)
            if not block:
                break
            indicator.update(audioop.rms(block, 2))
            try:
                events.put(("audio", (time.monotonic(), block)), timeout=0.05)
            except queue.Full:
                pass
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def monitor_output(indicator, stop):
    proc = subprocess.Popen(
        [
            "parec",
            "-d",
            "master.monitor",
            "--raw",
            "--format=s16le",
            "--rate=16000",
            "--channels=1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=ENV,
    )
    try:
        while not stop.is_set():
            block = proc.stdout.read(640)
            if not block:
                break
            indicator.update_output(audioop.rms(block, 2))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def feed(rec, s, block):
    pcm = array.array("h")
    pcm.frombytes(block)
    data = (F * len(pcm))(*(x / 32768.0 for x in pcm))
    accept(s, 16000, data, len(pcm))
    while ready(rec, s):
        decode(rec, s)


def get_text(rec, s):
    data = result(rec, s)
    try:
        return json.loads(c.string_at(data).decode()).get("text", "").strip()
    finally:
        destroy_result(data)


def main():
    from .network import Readiness, play_setup
    from .web import start as start_web
    from .settings import LOCK

    readiness = Readiness()
    readiness.start()
    try:
        web = start_web(readiness)
    except OSError:
        web = None
        log("WEB_UNAVAILABLE")
    os.chdir("/tmp/sherpa-test")
    cfg.model_config.num_threads = 1
    rec = create(c.byref(cfg))
    if not rec:
        raise RuntimeError("model init failed")
    events = queue.Queue(maxsize=40)
    touches = queue.Queue()
    wakes = queue.Queue()
    stopped = threading.Event()
    touch_down = threading.Event()
    kws_healthy = threading.Event()
    indicator = IndicatorWorker()
    threading.Thread(
        target=monitor_touch, args=(touches, stopped, touch_down), daemon=True
    ).start()
    threading.Thread(
        target=monitor_microphone, args=(events, stopped, indicator), daemon=True
    ).start()
    threading.Thread(
        target=monitor_output, args=(indicator, stopped), daemon=True
    ).start()
    wake_guard = mp.RawValue("d", 0)
    threading.Thread(
        target=monitor_wake,
        args=(wakes, kws_healthy, stopped, ROOT, ENV, wake_guard),
        daemon=True,
    ).start()
    mute = MarusyaMute()
    last_health = 0
    log("SERVICE_STARTED")
    dialogs = Dialogs()
    wake_stream = stream(rec)
    question_stream = None
    wake_started = time.monotonic()
    count = 0
    phase = "idle"
    cue = None
    request = None
    answer_audio = None
    pending_data = None
    pending_answer = None
    result_queue = None
    listen_started = 0
    last_voice = None
    heard = False
    discard_before = 0
    silence_limit = 20
    stream_audio = None
    speech_input = None
    play_gate = None
    commit_after = None
    speaking = None
    indicator_mode = None
    compactor = None
    compact_queue = None
    compact_base = None
    request_started = 0
    turn_base = None

    def indicator_set(mode):
        nonlocal indicator_mode
        if mode != indicator_mode:
            indicator_mode = mode
            indicator.set_listening(mode is not None, mode or "listen")
            log("INDICATOR", mode)

    def start_process(fn, *args):
        p = mp.Process(target=fn, args=args, daemon=False)
        p.start()
        return p

    def cancel_all():
        nonlocal cue, request, answer_audio, pending_data, pending_answer, result_queue, stream_audio, speech_input, play_gate, commit_after, speaking, compactor, compact_queue, compact_base
        for p in (cue, request, answer_audio, stream_audio, compactor):
            stop_process(p)
        if wake_guard.value > time.monotonic() + 1:
            wake_guard.value = time.monotonic() + 1
        cue = request = answer_audio = None
        pending_data = pending_answer = result_queue = None
        stream_audio = speech_input = play_gate = commit_after = speaking = None
        compactor = compact_queue = compact_base = None
        indicator_set(None)
        mute.set_active(False)

    def fail_turn():
        nonlocal phase, cue, question_stream, request_started
        cancel_all()
        if question_stream:
            drop_stream(question_stream)
            question_stream = None
        request_started = 0
        phase = "error"
        mute.set_active(True)
        wake_guard.value = time.monotonic() + 5
        cue = start_process(play_activation, asset_path("error"), wake_guard)
        reset_wake()

    def reset_wake():
        nonlocal wake_stream, wake_started, count
        drop_stream(wake_stream)
        wake_stream = stream(rec)
        wake_started = time.monotonic()
        count = 0

    def activate():
        nonlocal phase, cue
        cancel_all()
        status = readiness.snapshot()
        if status["state"] in ("offline", "login", "vpn"):
            phase = "setup"
            mute.set_active(True)
            wake_guard.value = time.monotonic() + 60
            address = status["ip"]
            if address and status["port"] != 80:
                address += ":" + str(status["port"])
            cue = start_process(
                play_setup, status["state"], address, ROOT, MPG, ENV, wake_guard
            )
            log("SETUP_REQUIRED", status["state"])
        else:
            begin_listen(True)

    def begin_listen(with_cue):
        nonlocal question_stream, phase, listen_started, last_voice, heard, cue, discard_before, silence_limit
        if question_stream:
            drop_stream(question_stream)
        question_stream = stream(rec)
        phase = "listen"
        listen_started = time.monotonic()
        last_voice = None
        heard = False
        silence_limit = 2 if with_cue else 20
        if with_cue:
            mute.set_active(True)
        indicator_set("listen")
        discard_before = listen_started - 0.10 if with_cue else listen_started
        if with_cue:
            cue_path = (
                ASSETS + "/wake.wav"
                if os.path.exists(ASSETS + "/wake.wav")
                else ASSETS + "/wake.mp3"
            )
            wake_guard.value = time.monotonic() + 5
            cue = start_process(play_activation, cue_path, wake_guard)
        log("LISTENING")

    log("MODEL_READY")
    cue = start_process(play_file, asset_path("ready"))
    try:
        while True:
            if time.monotonic() - last_health > 60:
                last_health = time.monotonic()
                with open("/proc/meminfo") as f:
                    memory = [
                        line.strip()
                        for line in f
                        if line.startswith(("MemAvailable:", "MemFree:"))
                    ]
                log("HEALTH", *memory)
            if compact_queue is not None:
                try:
                    compressed = compact_queue.get_nowait()
                except queue.Empty:
                    compressed = None
                if compressed is not None:
                    if pending_data == compact_base:
                        pending_data = compressed
                    elif dialogs.data == compact_base:
                        dialogs.data = compressed
                        dialogs.save()
                    stop_process(compactor)
                    compactor = compact_queue = compact_base = None
                elif compactor is not None and not compactor.is_alive():
                    stop_process(compactor)
                    compactor = compact_queue = compact_base = None
            if (
                phase == "working"
                and request_started
                and time.monotonic() - request_started
                > (180 if speaking is not None and speaking.is_set() else 45)
            ):
                log("TURN_TIMEOUT")
                fail_turn()
            if phase in ("error", "setup") and cue is not None and not cue.is_alive():
                cancel_all()
                phase = "idle"
                reset_wake()
            output_active = (
                phase == "working"
                and (
                    (speaking is not None and speaking.is_set())
                    or (cue is not None and cue.is_alive())
                )
            ) or (
                phase == "answer"
                and answer_audio is not None
                and answer_audio.is_alive()
            )
            if phase != "listen":
                indicator_set(
                    "output"
                    if output_active
                    else ("thinking" if phase == "working" else None)
                )
            # Check the panel before committing a completed response. A touch
            # queued during playback must win even if playback just ended.
            try:
                touches.get_nowait()
            except queue.Empty:
                pass
            else:
                if phase != "idle" or (cue and cue.is_alive()):
                    cancel_all()
                    phase = "idle"
                    if question_stream:
                        drop_stream(question_stream)
                        question_stream = None
                    reset_wake()
                    log("TOUCH_CANCEL")
                continue
            try:
                _, wake_stamp = wakes.get_nowait()
            except queue.Empty:
                pass
            else:
                if wake_stamp < wake_guard.value:
                    log("KWS_QUEUED_CUE_SUPPRESSED")
                    continue
                if phase != "listen":
                    log(
                        "WAKE_MICRO",
                        round(wake_stamp, 3),
                        "LAG_MS",
                        round((time.monotonic() - wake_stamp) * 1000),
                    )
                    activate()
                    reset_wake()
                continue
            if phase == "working" and result_queue:
                try:
                    response = (
                        result_queue.get(timeout=0.2)
                        if request is not None and not request.is_alive()
                        else result_queue.get_nowait()
                    )
                except queue.Empty:
                    response = None
                if response:
                    if response[0] == "ok":
                        pending_answer, pending_data = response[1:]
                        if (
                            sum(
                                len(m["content"])
                                for m in pending_data["dialogs"][
                                    pending_data["current"]
                                ]
                            )
                            > 16000
                        ):
                            compact_base = copy.deepcopy(pending_data)
                            compact_queue = mp.Queue()
                            compactor = start_process(
                                compact_worker, compact_base, compact_queue
                            )
                    else:
                        log("REQUEST_ERROR", response[1])
                        fail_turn()
                    stop_process(request)
                    request = None
                    result_queue = None
            if (
                phase == "working"
                and request is not None
                and not request.is_alive()
                and pending_answer is None
            ):
                log("REQUEST_DIED")
                fail_turn()
            if phase == "working" and stream_audio and (not cue or not cue.is_alive()):
                play_gate.set()
            if phase == "working" and stream_audio and not stream_audio.is_alive():
                ok = stream_audio.exitcode == 0
                if not ok:
                    log("SPEECH_STREAM_ERROR")
                    fail_turn()
                elif pending_data is not None:
                    if commit_after is None:
                        commit_after = time.monotonic() + 0.3
                    if (
                        time.monotonic() >= commit_after
                        and not touch_down.is_set()
                        and touches.empty()
                    ):
                        stop_process(stream_audio)
                        stream_audio = None
                        speech_input = play_gate = None
                        commit_after = None
                        speaking = None
                        dialogs.commit(pending_data, turn_base)
                        pending_data = pending_answer = None
                        mute.set_active(False)
                        request_started = 0
                        begin_listen(False)
                        reset_wake()
            if (
                phase == "working"
                and stream_audio is None
                and pending_answer is not None
                and (not cue or not cue.is_alive())
            ):
                if cue:
                    stop_process(cue)
                    cue = None
                answer_audio = start_process(play_text, pending_answer)
                phase = "answer"
            if phase == "answer" and answer_audio and not answer_audio.is_alive():
                ok = answer_audio.exitcode == 0
                stop_process(answer_audio)
                answer_audio = None
                if ok and pending_data is not None:
                    dialogs.commit(pending_data, turn_base)
                pending_data = pending_answer = None
                if ok:
                    mute.set_active(False)
                    begin_listen(False)
                else:
                    fail_turn()
                reset_wake()
            try:
                kind, block = touches.get_nowait()
            except queue.Empty:
                try:
                    kind, block = events.get(timeout=0.1)
                except queue.Empty:
                    kind = block = None
            if kind is None:
                if (
                    phase == "working"
                    and request
                    and not request.is_alive()
                    and pending_answer is None
                ):
                    # Queue feeder can lag the process; get with a short bounded wait.
                    try:
                        response = result_queue.get(timeout=0.3)
                    except queue.Empty:
                        log("REQUEST_DIED")
                        fail_turn()
                    else:
                        if response[0] == "ok":
                            pending_answer, pending_data = response[1:]
                        else:
                            log("REQUEST_ERROR", response[1])
                            fail_turn()
                        stop_process(request)
                        request = None
                        result_queue = None
                continue
            if kind == "touch":
                if phase != "idle" or (cue and cue.is_alive()):
                    cancel_all()
                    phase = "idle"
                    if question_stream:
                        drop_stream(question_stream)
                        question_stream = None
                    reset_wake()
                    log("TOUCH_CANCEL")
                continue
            stamp, block = block
            if stamp < discard_before:
                continue
            now = time.monotonic()
            if phase == "listen":
                level = audioop.rms(block, 2)
                if no_speech_timeout(heard, now - listen_started, silence_limit):
                    drop_stream(question_stream)
                    question_stream = None
                    cancel_all()
                    phase = "idle"
                    reset_wake()
                    log("NO_SPEECH_TIMEOUT")
                    continue
                if level > 650:
                    mute.set_active(True)
                    heard = True
                    last_voice = now
                feed(rec, question_stream, block)
                if (
                    heard and last_voice and now - last_voice >= 1.0
                ) or now - listen_started > 20:
                    pad = (F * 8000)()
                    accept(question_stream, 16000, pad, 8000)
                    finish(question_stream)
                    while ready(rec, question_stream):
                        decode(rec, question_stream)
                    text = get_text(rec, question_stream)
                    drop_stream(question_stream)
                    question_stream = None
                    log("TRANSCRIPT", json.dumps(text, ensure_ascii=False))
                    if not text and time.monotonic() - listen_started < silence_limit:
                        # The activation cue may trigger VAD but contain no
                        # words. Continue listening without replaying the cue.
                        question_stream = stream(rec)
                        last_voice = None
                        heard = False
                        log("EMPTY_CAPTURE_RETRY")
                        continue
                    indicator_set(None)
                    if not text or ends_dialogue(text):
                        cancel_all()
                        phase = "idle"
                        reset_wake()
                        continue
                    with LOCK:
                        dialogs.reload()
                        dialogs.maintain()
                        response = dialogs.command(text)
                        turn_base = copy.deepcopy(dialogs.data)
                    if response:
                        request_started = 0
                        pending_answer = response
                        pending_data = None
                        phase = "working"
                    else:
                        request_started = time.monotonic()
                        result_queue = mp.Queue()
                        speech_input = mp.Queue()
                        play_gate = mp.Event()
                        speaking = mp.Event()
                        stream_audio = start_process(
                            speech_worker,
                            speech_input,
                            play_gate,
                            ROOT,
                            MPG,
                            ENV,
                            speaking,
                        )
                        request = start_process(
                            request_worker,
                            dialogs.data,
                            text,
                            result_queue,
                            speech_input,
                        )
                        if cue:
                            stop_process(cue)
                        cue = start_process(play_text, "Думаю.", wake_guard)
                        phase = "working"
                    reset_wake()
                continue
            # Recognition remains active while requests and audio playback run.
            if time.monotonic() < wake_guard.value:
                continue
            if kws_healthy.is_set():
                if now - wake_started > 8:
                    reset_wake()
                continue
            feed(rec, wake_stream, block)
            count += 1
            if count % 5 == 0 and re.search(
                r"\bалиса\b", get_text(rec, wake_stream), re.I
            ):
                log(
                    "WAKE_DETECTED",
                    round(time.monotonic(), 3),
                    "AUDIO_LAG_MS",
                    round((time.monotonic() - stamp) * 1000),
                )
                activate()
                reset_wake()
                continue
            if now - wake_started > 8:
                reset_wake()
    finally:
        stopped.set()
        readiness.closed.set()
        if web:
            web.shutdown()
            web.server_close()
        cancel_all()
        indicator.close()
        mute.close()
        if question_stream:
            drop_stream(question_stream)
        drop_stream(wake_stream)
        drop_rec(rec)


def shutdown(*_):
    raise KeyboardInterrupt()
