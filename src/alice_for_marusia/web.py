"""LAN-only configuration UI. Python standard library, no framework or database."""

import http.server, json, os, pathlib, secrets, threading, time, urllib.parse
from .settings import DATA, LOCK, read_json, settings, update
from .conversations import Dialogs
from .auth import ChatLogin
from .openai_client import AUTH_FILE
from . import vpn as vpn_config

WEB = pathlib.Path(__file__).with_name("static")


class Application:
    def __init__(self, readiness):
        self.readiness = readiness
        self.login = ChatLogin()
        self.csrf = secrets.token_urlsafe(32)
        self.job_lock = threading.Lock()
        self.job = {"running": False}

    def state(self):
        auth = read_json(AUTH_FILE, {})
        with LOCK:
            dialogs = Dialogs(str(DATA / "dialogs.json"))
            names = list(dialogs.data["dialogs"])
            listing = [
                {
                    "id": name,
                    "title": "Диалог %d" % (i + 1),
                    "active": name == dialogs.data["current"],
                    "messages": dialogs.data["dialogs"][name],
                }
                for i, name in enumerate(names)
            ]
        return {
            "csrf": self.csrf,
            "network": self.readiness.snapshot(),
            "settings": settings(),
            "account": {
                "connected": bool(auth.get("access_token")),
                "email": auth.get("email", ""),
            },
            "dialogs": listing,
            "vpn": vpn_config.listing(),
            "job": dict(self.job),
        }

    def background(self, fn):
        with self.job_lock:
            if self.job.get("running"):
                raise ValueError("Дождитесь завершения текущей операции")
            self.job = {"running": True}

        def run():
            try:
                fn()
                result = {"running": False, "ok": True}
            except Exception as e:
                # Only our validation messages are exposed. Upstream errors can
                # contain credentials, subscription URLs or callback codes.
                result = {
                    "running": False,
                    "ok": False,
                    "error": (
                        str(e)
                        if type(e) is ValueError
                        else "Не удалось выполнить операцию. Проверьте подключение и попробуйте снова."
                    ),
                }
            with self.job_lock:
                self.job = result

        threading.Thread(target=run, daemon=True).start()
        return {"running": True}

    def dialog_action(self, action, payload):
        with LOCK:
            d = Dialogs(str(DATA / "dialogs.json"))
            name = payload.get("id")
            dialogs = d.data["dialogs"]
            if action == "new":
                if len(dialogs) >= 32:
                    raise ValueError("Сначала удалите ненужные диалоги")
                n = 1
                while "диалог " + str(n) in dialogs:
                    n += 1
                name = "диалог " + str(n)
                dialogs[name] = []
                d.data["updated_at"] = dict(
                    d.data.get("updated_at", {}), **{name: time.time()}
                )
            elif name not in dialogs:
                raise ValueError("Диалог не найден")
            if action in ("activate", "new"):
                d.data["current"] = name
            elif action == "delete":
                if len(dialogs) == 1:
                    dialogs[name] = []
                else:
                    del dialogs[name]
                    d.data.get("updated_at", {}).pop(name, None)
                    if d.data["current"] == name:
                        d.data["current"] = next(iter(dialogs))
            elif action == "clear":
                dialogs[name] = []
            elif action not in ("new", "activate"):
                raise ValueError("Неизвестная операция")
            d.save()
        return {"ok": True}

    def post(self, path, data):
        if path == "/api/settings":
            return update(wake_threshold=data["threshold"])
        if path == "/api/settings/reset":
            return update(wake_threshold=70)
        if path == "/api/login/start":
            return {"url": self.login.start()}
        if path == "/api/login/finish":
            return self.background(
                lambda: (
                    self.login.submit(data.get("callback", "")),
                    self.readiness.invalidate(),
                )
            )
        if path == "/api/logout":
            self.login.logout()
            self.readiness.invalidate()
            return {"ok": True}
        if path == "/api/check":
            return self.background(lambda: self.readiness.check(actual_request=True))
        if path == "/api/vpn/import":
            return self.background(
                lambda: vpn_config.import_servers(
                    data.get("source", ""), data.get("subscription", False)
                )
            )
        if path == "/api/vpn/select":
            return self.background(
                lambda: (
                    vpn_config.select_server(data.get("id")),
                    self.readiness.invalidate(),
                )
            )
        if path == "/api/vpn/mode":
            result = update(vpn_mode=data.get("mode"))
            self.readiness.invalidate()
            return result
        if path.startswith("/api/dialog/"):
            return self.dialog_action(path.rsplit("/", 1)[1], data)
        raise ValueError("Неизвестная операция")


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "Alisa"
    sys_version = ""

    def log_message(self, *_):
        pass

    def local_request(self):
        try:
            host = urllib.parse.urlsplit(
                "http://" + self.headers.get("Host", "")
            ).hostname
            return host in ("localhost", "127.0.0.1", self.connection.getsockname()[0])
        except ValueError:
            return False

    def respond(self, status, data, content="application/json; charset=utf-8"):
        raw = (
            json.dumps(data, ensure_ascii=False).encode()
            if not isinstance(data, bytes)
            else data
        )
        self.send_response(status)
        self.send_header("Content-Type", content)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if not self.local_request():
            return self.respond(403, {"error": "Неверный адрес колонки"})
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/state":
            return self.respond(200, self.server.app.state())
        assets = {
            "/": ("index.html", "text/html"),
            "/index.html": ("index.html", "text/html"),
            "/app.css": ("app.css", "text/css"),
            "/app.js": ("app.js", "text/javascript"),
        }
        if path not in assets:
            return self.respond(404, {"error": "Не найдено"})
        name, kind = assets[path]
        return self.respond(200, (WEB / name).read_bytes(), kind + "; charset=utf-8")

    def do_POST(self):
        if (
            not self.local_request()
            or self.headers.get("Sec-Fetch-Site") == "cross-site"
        ):
            return self.respond(403, {"error": "Откройте страницу колонки напрямую"})
        if not secrets.compare_digest(
            self.headers.get("X-Alisa-CSRF", ""), self.server.app.csrf
        ):
            return self.respond(403, {"error": "Обновите страницу"})
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + self.headers.get("Host", ""):
            return self.respond(403, {"error": "Неверный источник запроса"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 524288:
                raise ValueError("Неверный размер запроса")
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("Неверный запрос")
            result = self.server.app.post(urllib.parse.urlsplit(self.path).path, data)
            self.respond(200, result)
        except (ValueError, KeyError, TypeError):
            self.respond(400, {"error": "Проверьте введённые данные"})
        except Exception:
            self.respond(500, {"error": "Не удалось выполнить операцию"})

    def setup(self):
        super().setup()
        self.connection.settimeout(15)


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, address, app):
        self.app = app
        super().__init__(address, Handler)


def start(readiness, port=100):
    app = Application(readiness)
    server = Server(("0.0.0.0", port), app)
    readiness.port = server.server_port
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
