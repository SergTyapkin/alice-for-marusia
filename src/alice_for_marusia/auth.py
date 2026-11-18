"""Official public-client PKCE flow. Phone users paste the loopback callback URL.

The password is entered only at auth.openai.com. RS256 verification uses the
standard library rather than installing a second crypto runtime on the speaker.
"""

import base64, hashlib, json, secrets, threading, time, urllib.parse, uuid
from .settings import DATA, read_json, write_json
from .openai_client import AUTH_FILE, BASE, TOKEN_URL, _post, _open, credential_lock
import urllib.request

ISSUER = "https://auth.openai.com"
REDIRECT = "http://127.0.0.1:1455/auth/callback"


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(data):
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def validate_identity(encoded, keys, audience, nonce):
    head, payload, signature = encoded.split(".")
    header = json.loads(unb64(head))
    claims = json.loads(unb64(payload))
    if header.get("alg") != "RS256":
        raise ValueError("Неподдерживаемая подпись входа")
    key = next(
        (
            k
            for k in keys["keys"]
            if k.get("kid") == header.get("kid")
            and k.get("kty") == "RSA"
            and k.get("use", "sig") == "sig"
            and k.get("alg", "RS256") == "RS256"
        ),
        None,
    )
    if not key:
        raise ValueError("Не найден ключ подписи")
    n = int.from_bytes(unb64(key["n"]), "big")
    e = int.from_bytes(unb64(key["e"]), "big")
    size = (n.bit_length() + 7) // 8
    sig = unb64(signature)
    if len(sig) != size or size < 256 or int.from_bytes(sig, "big") >= n:
        raise ValueError("Неверная подпись входа")
    decoded = pow(int.from_bytes(sig, "big"), e, n).to_bytes(size, "big")
    digest = (
        bytes.fromhex("3031300d060960864801650304020105000420")
        + hashlib.sha256((head + "." + payload).encode()).digest()
    )
    expected = b"\x00\x01" + b"\xff" * (size - len(digest) - 3) + b"\x00" + digest
    if not secrets.compare_digest(decoded, expected):
        raise ValueError("Неверная подпись входа")
    now = time.time()
    aud = claims.get("aud", [])
    if isinstance(aud, str):
        aud = [aud]
    if (
        claims.get("iss") != ISSUER
        or audience not in aud
        or not claims.get("sub")
        or claims.get("exp", 0) <= now
        or not claims.get("iat")
        or claims["iat"] > now + 60
        or claims.get("nbf", 0) > now + 60
        or not secrets.compare_digest(str(claims.get("nonce", "")), nonce)
    ):
        raise ValueError("Проверка личности не пройдена")
    return claims


class ChatLogin:
    def __init__(self):
        self.lock = threading.RLock()
        self.pending = None
        self.status = "idle"

    def start(self):
        with self.lock:
            registration = read_json(DATA / "registration.json", {})
            old = read_json(AUTH_FILE, {})
            host = (
                registration.get("host")
                or old.get("ext_agent_host_id")
                or "urn:uuid:" + str(uuid.uuid4())
            )
            write_json(DATA / "registration.json", {"host": host})
            p = {
                "state": secrets.token_urlsafe(32),
                "nonce": secrets.token_urlsafe(32),
                "verifier": secrets.token_urlsafe(48),
                "host": host,
                "at": time.monotonic(),
                "client": old.get("client_id", "dynamic_agent_client"),
                "subject": old.get("subject"),
            }
            self.pending = p
            self.status = "waiting"
            params = {
                "client_id": p["client"],
                "ext_agent_host_id": host,
                "response_type": "code",
                "redirect_uri": REDIRECT,
                "resource": BASE,
                "scope": "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct",
                "state": p["state"],
                "nonce": p["nonce"],
                "code_challenge_method": "S256",
                "code_challenge": b64(hashlib.sha256(p["verifier"].encode()).digest()),
            }
            if p["client"] == "dynamic_agent_client":
                params["agent_name_hint"] = "Alisa for Capsula"
            elif old.get("id_token"):
                params["id_token_hint"] = old["id_token"]
            return ISSUER + "/api/accounts/authorize?" + urllib.parse.urlencode(params)

    def submit(self, callback):
        with self.lock:
            p = self.pending
            if not p or self.status != "waiting" or time.monotonic() - p["at"] > 600:
                raise ValueError("Начните вход заново")
            parsed = urllib.parse.urlsplit(callback.strip())
            expected = urllib.parse.urlsplit(REDIRECT)
            if (parsed.scheme, parsed.netloc, parsed.path) != (
                expected.scheme,
                expected.netloc,
                expected.path,
            ):
                raise ValueError("Вставьте полный адрес возврата 127.0.0.1 из браузера")
            q = urllib.parse.parse_qs(parsed.query)
            if not secrets.compare_digest(q.get("state", [""])[0], p["state"]):
                raise ValueError("Неверное состояние входа")
            if q.get("error"):
                self.pending = None
                raise ValueError("Вход отменён")
            client = q.get("client_id", [p["client"]])[0]
            code = q.get("code", [""])[0]
            if (
                not code
                or client == "dynamic_agent_client"
                or (p["client"] != "dynamic_agent_client" and client != p["client"])
            ):
                raise ValueError("Неполный ответ входа")
            self.status = "checking"
        try:
            fields = {
                "grant_type": "authorization_code",
                "client_id": client,
                "code": code,
                "code_verifier": p["verifier"],
                "redirect_uri": REDIRECT,
                "resource": BASE,
            }
            with _post(
                TOKEN_URL,
                urllib.parse.urlencode(fields).encode(),
                {"Content-Type": "application/x-www-form-urlencoded"},
                20,
            ) as r:
                token = json.load(r)
            with _open(
                urllib.request.Request(ISSUER + "/.well-known/jwks.json"), 15
            ) as r:
                keys = json.load(r)
            identity = validate_identity(token["id_token"], keys, client, p["nonce"])
            if p["subject"] and identity["sub"] != p["subject"]:
                raise ValueError(
                    "Выполнен вход в другой аккаунт; сначала выйдите из прежнего"
                )
            if (
                "chatgpt.tokens.use.direct" not in token.get("scope", "").split()
                or not token.get("access_token")
                or not token.get("refresh_token")
            ):
                raise ValueError("Не предоставлен доступ к запросам ChatGPT")
            record = {
                k: token[k]
                for k in (
                    "access_token",
                    "refresh_token",
                    "id_token",
                    "expires_in",
                    "token_type",
                )
                if k in token
            }
            record.update(
                issuer=ISSUER,
                subject=identity["sub"],
                email=identity.get("email", ""),
                client_id=client,
                ext_agent_host_id=p["host"],
                scopes=token["scope"].split(),
                saved_at=int(time.time()),
            )
            with self.lock:
                if self.pending is not p:
                    raise ValueError("Вход отменён")
                with credential_lock(AUTH_FILE):
                    write_json(AUTH_FILE, record)
                self.pending = None
                self.status = "done"
        except Exception:
            with self.lock:
                self.pending = None
                self.status = "error"
            raise

    def logout(self):
        with self.lock:
            self.pending = None
            self.status = "idle"
            with credential_lock(AUTH_FILE):
                AUTH_FILE.unlink(missing_ok=True)
