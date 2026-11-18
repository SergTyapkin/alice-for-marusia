"""Lightweight child processes; never import the recognizer or the main loop."""

import copy, os, subprocess, time
from .audio import ROOT, MPG, say
from .conversations import Dialogs
from .openai_client import PlanSession

ENV = os.environ.copy()
ENV.pop("LD_LIBRARY_PATH", None)


def play_file(path):
    os.setsid()
    if path.endswith(".wav"):
        subprocess.run(
            ["paplay", "--latency-msec=40", "--device=master", path],
            env=ENV,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=45,
        )
        return
    wav = ROOT + "/cue-%d.wav" % os.getpid()
    env = ENV.copy()
    env["LD_LIBRARY_PATH"] = MPG + "/usr/lib/aarch64-linux-gnu"
    try:
        subprocess.run(
            [MPG + "/usr/bin/mpg123.bin", "-q", "-e", "s16", "-w", wav, path],
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )
        subprocess.run(
            ["paplay", "--device=master", wav],
            env=ENV,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=45,
        )
    finally:
        try:
            os.unlink(wav)
        except FileNotFoundError:
            pass


def play_activation(path, guard):
    guard.value = time.monotonic() + 5
    try:
        play_file(path)
    finally:
        guard.value = time.monotonic() + 1.0


def play_text(text, guard=None):
    os.setsid()
    if guard is None:
        say(text)
    else:
        say(text, guard=guard)


def request_worker(snapshot, text, output, speech_input=None):
    os.setsid()
    try:

        class StagedDialogs(Dialogs):
            def save(self):
                pass

        dialogs = StagedDialogs.__new__(StagedDialogs)
        dialogs.data = copy.deepcopy(snapshot)
        dialogs.limit = 16000
        dialogs.path = ""
        on_delta = (
            (lambda delta: speech_input.put(("delta", delta)))
            if speech_input is not None
            else None
        )
        answer = dialogs.ask(PlanSession(), text, on_delta=on_delta)
        if speech_input is not None:
            speech_input.put(("done", None))
        output.put(("ok", answer, dialogs.data))
    except Exception as e:
        if speech_input is not None:
            speech_input.put(("cancel", None))
        output.put(("error", type(e).__name__))


def compact_worker(snapshot, output):
    os.setsid()
    try:

        class StagedDialogs(Dialogs):
            def save(self):
                pass

        dialogs = StagedDialogs.__new__(StagedDialogs)
        dialogs.data = copy.deepcopy(snapshot)
        dialogs.limit = 16000
        dialogs.path = ""
        dialogs.compact(PlanSession())
        output.put(dialogs.data)
    except Exception as e:
        print("COMPACTION_ERROR", type(e).__name__, flush=True)
