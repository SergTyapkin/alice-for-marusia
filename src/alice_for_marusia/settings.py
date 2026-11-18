"""Small persistent configuration; the web server and detector share this file."""

import json, os, pathlib, tempfile, threading

DATA = pathlib.Path(os.environ.get("ALISA_DATA", "/data/alisa"))
LOCK = threading.RLock()


def _after_fork():
    global LOCK
    LOCK = threading.RLock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)
DEFAULTS = {"wake_threshold": 70, "vpn_mode": "xray"}


def read_json(path, default):
    try:
        with open(path, encoding="utf8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, value):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".config-", dir=str(path.parent))
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf8") as f:
            json.dump(value, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def settings():
    result = dict(DEFAULTS)
    result.update(read_json(DATA / "settings.json", {}))
    try:
        result["wake_threshold"] = max(60, min(80, int(result["wake_threshold"])))
    except (TypeError, ValueError):
        result["wake_threshold"] = 70
    return result


def update(**changes):
    if "wake_threshold" in changes:
        n = changes["wake_threshold"]
        if type(n) is not int or not 60 <= n <= 80:
            raise ValueError("Недопустимая чувствительность")
    if "vpn_mode" in changes and changes["vpn_mode"] not in ("direct", "xray"):
        raise ValueError("Неизвестный режим подключения")
    with LOCK:
        value = settings()
        value.update(changes)
        write_json(DATA / "settings.json", value)
    return value
