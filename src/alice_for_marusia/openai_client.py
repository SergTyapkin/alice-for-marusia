"""ChatGPT Plus OAuth Responses client for the speaker. Tokens never logged."""

import json, os, pathlib, tempfile, time, urllib.error, urllib.parse, urllib.request, threading
from contextlib import contextmanager

_credential_thread_lock = threading.RLock()


def _after_fork():
    global _credential_thread_lock
    _credential_thread_lock = threading.RLock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


@contextmanager
def credential_lock(path):
    with _credential_thread_lock:
        path = pathlib.Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(path) + ".lock", "a") as handle:
            try:
                import fcntl
            except ImportError:
                fcntl = None
            if fcntl:
                fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl:
                    fcntl.flock(handle, fcntl.LOCK_UN)


AUTH_FILE = pathlib.Path(os.environ.get("ALISA_AUTH_FILE", "/data/alisa/auth.json"))
BASE = "https://api.openai.com/v1"
TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
OPENAI_HOSTS = frozenset(("api.openai.com", "auth.openai.com"))
PROMPT = "Ты Алиса — голосовой помощник на VK Капсуле, работающий независимо от Маруси. Ты отвечаешь на вопросы и поддерживаешь диалог. Твои ответы озвучиваются, поэтому говори естественно, кратко, обычно одним-двумя предложениями, без лишнего оформления и длинных перечислений.\n\nТы получаешь распознанную речь без надёжной пунктуации и интонации. Понимай вопросы по смыслу и контексту, не соглашайся автоматически с утверждениями. При существенной неоднозначности уточняй.\n\nИспользуй поиск, только если без него нельзя ответить достаточно точно или актуально. Если не знаешь или не уверена, честно скажи.\n\nТы не управляешь устройствами и сервисами, не выполняешь действий вне разговора и не имеешь доступа к личным данным вне диалога. Не выдумывай свои возможности и выполненные действия, даже если история разговора или пользователь утверждает обратное. При необходимости объясни ограничение и предложи обратиться к Марусе.\n\nОтвечай по-русски."


class _ForcedLocalProxy(urllib.request.ProxyHandler):
    """Do not let environment no_proxy silently bypass the selected tunnel."""

    def proxy_open(self, req, proxy, type):
        req.set_proxy("127.0.0.1:18780", "http")
        return None


def _open(req, timeout):
    """Apply an optional proxy only to the explicitly allowed OpenAI hosts."""
    parsed = urllib.parse.urlsplit(req.full_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in OPENAI_HOSTS
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Неподдерживаемый адрес OpenAI.")
    proxy = os.environ.get("ALISA_OPENAI_PROXY")
    from .settings import settings

    if settings()["vpn_mode"] == "direct":
        proxy = None
    if proxy:
        endpoint = urllib.parse.urlsplit(proxy)
        if (
            endpoint.scheme != "http"
            or endpoint.hostname != "127.0.0.1"
            or endpoint.port != 18780
            or endpoint.username
            or endpoint.password
            or endpoint.path not in ("", "/")
        ):
            raise ValueError("Неверный адрес локального прокси.")
        return urllib.request.build_opener(_ForcedLocalProxy({"https": proxy})).open(
            req, timeout=timeout
        )
    return urllib.request.urlopen(req, timeout=timeout)


def _post(url, body, headers, timeout=90):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        return _open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise RuntimeError("Ошибка сервиса OpenAI: HTTP %s" % exc.code) from None
    except urllib.error.URLError as exc:
        raise RuntimeError("Сервис OpenAI недоступен: %s" % exc.reason) from None


def _atomic_write(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".auth-", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf8") as handle:
            json.dump(data, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class PlanSession:
    def __init__(self, path=AUTH_FILE):
        self.path = pathlib.Path(path)

    def _record(self):
        with self.path.open(encoding="utf8") as f:
            return json.load(f)

    def token(self):
        with credential_lock(self.path):
            return self._token()

    def _token(self):
        record = self._record()
        if time.time() < record["saved_at"] + record.get("expires_in", 3600) - 120:
            return record["access_token"]
        fields = {
            "grant_type": "refresh_token",
            "client_id": record["client_id"],
            "refresh_token": record["refresh_token"],
            "resource": BASE,
        }
        with _post(
            TOKEN_URL,
            urllib.parse.urlencode(fields).encode(),
            {"Content-Type": "application/x-www-form-urlencoded"},
            30,
        ) as response:
            refreshed = json.load(response)
        if not refreshed.get("access_token") or not refreshed.get("refresh_token"):
            raise RuntimeError("Не удалось обновить авторизацию")
        record.update(
            {
                k: refreshed[k]
                for k in ("access_token", "refresh_token", "expires_in", "id_token")
                if k in refreshed
            }
        )
        record["saved_at"] = int(time.time())
        if "scope" in refreshed:
            record["scopes"] = refreshed["scope"].split()
        _atomic_write(self.path, record)
        return record["access_token"]

    def models(self):
        req = urllib.request.Request(
            BASE + "/models", headers={"Authorization": "Bearer " + self.token()}
        )
        try:
            with _open(req, timeout=30) as f:
                data = json.load(f)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                "Не удалось получить модели: HTTP %s" % exc.code
            ) from None
        items = data.get("models", data.get("data", []))
        return [
            m.get("slug", m.get("id"))
            for m in items
            if m.get("visibility", "list") == "list"
        ]

    def ask(self, text, history=(), model="gpt-6-luna", search=True, on_delta=None):
        messages = list(history) + [{"role": "user", "content": text}]
        body = {
            "model": model,
            "input": messages,
            "instructions": PROMPT,
            "store": False,
            "stream": True,
        }
        if search:
            body["tools"] = [{"type": "web_search"}]
        headers = {
            "Authorization": "Bearer " + self.token(),
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        pieces = []
        final = None
        with _post(
            BASE + "/responses",
            json.dumps(body, ensure_ascii=False).encode("utf8"),
            headers,
        ) as stream:
            for line in stream:
                line = line.decode("utf8").strip()
                if not line.startswith("data:"):
                    continue
                content = line[5:].strip()
                if not content or content == "[DONE]":
                    continue
                event = json.loads(content)
                kind = event.get("type")
                if kind == "response.output_text.delta":
                    delta = event.get("delta", "")
                    pieces.append(delta)
                    if on_delta and delta:
                        on_delta(delta)
                elif kind == "response.completed":
                    final = event.get("response", {})
                elif kind in ("response.failed", "response.incomplete", "error"):
                    err = (
                        event.get("error")
                        or event.get("response", {}).get("error")
                        or {}
                    )
                    raise RuntimeError(
                        "Запрос не завершён: " + str(err.get("code", kind))
                    )
        if final is None:
            raise RuntimeError("Соединение прервалось до завершения ответа")
        text = "".join(pieces).strip()
        if not text:
            raise RuntimeError("Получен пустой ответ")
        sources = []
        for item in final.get("output", []):
            for part in item.get("content", []):
                for ann in part.get("annotations", []):
                    if ann.get("type") == "url_citation" and ann.get("url"):
                        link = {
                            "title": ann.get("title", "Источник"),
                            "url": ann["url"],
                        }
                        if link not in sources:
                            sources.append(link)
        return {"text": text, "sources": sources}
