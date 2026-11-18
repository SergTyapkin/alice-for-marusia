"""Import ordinary provider subscriptions/share links into a local Xray proxy.

INCY/HAPP are clients/subscription formats, not tunnelling protocols. Encrypted
app-specific happ:// links need the original subscription URL from the provider.
"""

import base64, hashlib, ipaddress, json, pathlib, socket, subprocess, urllib.parse, urllib.request
import uuid
from .settings import DATA, read_json, write_json, update, settings, LOCK

CONFIG = pathlib.Path("/data/xray-test/config.json")
STORE = DATA / "vpn-servers.json"


def device_headers():
    # Identify this installation, without disclosing a MAC address or serial.
    # Keep the value stable so provider device limits work as intended.
    path = DATA / "vpn-device.json"
    with LOCK:
        record = read_json(path, {})
        if not record.get("hwid"):
            record = {"hwid": uuid.uuid4().hex}
            write_json(path, record)
    return {
        "x-hwid": record["hwid"],
        "x-device-os": "Linux",
        "x-device-model": "VK Capsula",
    }


def check_device_limit(headers):
    if any(
        str(headers.get(key, "")).lower() == "true"
        for key in ("x-hwid-max-devices-reached", "x-hwid-limit")
    ):
        raise ValueError(
            "Достигнут лимит устройств подписки. Освободите устройство в кабинете провайдера и повторите импорт."
        )
    if str(headers.get("x-hwid-not-supported", "")).lower() == "true":
        raise ValueError(
            "Провайдер не принял HWID колонки. Проверьте поддержку устройств в настройках подписки."
        )


def provider_notice(server):
    name = server.get("name", "").lower()
    return (
        "hwid" in name or "обновите подписку" in name or "update subscription" in name
    )


def decode64(s):
    return base64.urlsafe_b64decode(s.strip() + "=" * (-len(s.strip()) % 4)).decode(
        "utf8"
    )


def one(q, key, default=""):
    return q.get(key, [default])[0]


def stream_options(q):
    network = one(q, "type", "tcp")
    security = one(q, "security", "none")
    if network not in ("tcp", "ws", "grpc", "xhttp"):
        raise ValueError("Этот транспорт пока не поддерживается")
    if security not in ("none", "tls", "reality"):
        raise ValueError("Неизвестная защита соединения")
    stream = {"network": network, "security": security}
    if security == "tls":
        stream["tlsSettings"] = {
            "serverName": one(q, "sni", one(q, "host")),
            "allowInsecure": False,
        }
        if one(q, "fp"):
            stream["tlsSettings"]["fingerprint"] = one(q, "fp")
        if one(q, "alpn"):
            stream["tlsSettings"]["alpn"] = one(q, "alpn").split(",")
    if security == "reality":
        stream["realitySettings"] = {
            "serverName": one(q, "sni"),
            "fingerprint": one(q, "fp", "chrome"),
            "publicKey": one(q, "pbk"),
            "shortId": one(q, "sid"),
            "spiderX": one(q, "spx", "/"),
        }
        if not one(q, "pbk") or not one(q, "sni"):
            raise ValueError("Для REALITY нужны SNI и публичный ключ")
    if network == "ws":
        stream["wsSettings"] = {
            "path": one(q, "path", "/"),
            "headers": {"Host": one(q, "host")},
        }
    elif network == "grpc":
        stream["grpcSettings"] = {
            "serviceName": one(q, "serviceName"),
            "multiMode": one(q, "mode") == "multi",
        }
    elif network == "xhttp":
        stream["xhttpSettings"] = {
            "path": one(q, "path", "/"),
            "host": one(q, "host"),
            "mode": one(q, "mode", "auto"),
        }
    return stream


def parse_link(link):
    link = link.strip()
    p = urllib.parse.urlsplit(link)
    scheme = p.scheme.lower()
    q = urllib.parse.parse_qs(p.query)
    label = urllib.parse.unquote(p.fragment)
    if scheme == "vmess":
        v = json.loads(decode64(link[len("vmess://") :].split("#")[0]))
        host, port = v["add"], int(v["port"])
        label = v.get("ps") or host
        opts = {
            "type": [v.get("net", "tcp")],
            "security": ["tls" if v.get("tls") == "tls" else "none"],
            "host": [v.get("host", "")],
            "path": [v.get("path", "/")],
            "sni": [v.get("sni", "")],
        }
        outbound = {
            "protocol": "vmess",
            "settings": {
                "vnext": [
                    {
                        "address": host,
                        "port": port,
                        "users": [
                            {
                                "id": v["id"],
                                "alterId": int(v.get("aid", 0)),
                                "security": v.get("scy", "auto"),
                            }
                        ],
                    }
                ]
            },
            "streamSettings": stream_options(opts),
        }
    elif scheme in ("vless", "trojan"):
        host, port = p.hostname, p.port or 443
        if not host or not p.username:
            raise ValueError("Неполная ссылка сервера")
        password = urllib.parse.unquote(p.username)
        if scheme == "vless":
            user = {"id": password, "encryption": one(q, "encryption", "none")}
            if one(q, "flow"):
                user["flow"] = one(q, "flow")
            params = {"vnext": [{"address": host, "port": port, "users": [user]}]}
        else:
            params = {
                "servers": [{"address": host, "port": port, "password": password}]
            }
            q.setdefault("security", ["tls"])
        outbound = {
            "protocol": scheme,
            "settings": params,
            "streamSettings": stream_options(q),
        }
    elif scheme == "ss":
        raw = link[len("ss://") :].split("#")[0].split("?")[0]
        if "@" not in raw:
            raw = decode64(raw)
        auth, endpoint = raw.rsplit("@", 1)
        if ":" not in auth:
            auth = decode64(auth)
        method, password = urllib.parse.unquote(auth).split(":", 1)
        address = urllib.parse.urlsplit("ss://" + endpoint)
        host, port = address.hostname, address.port
        if not host or not port:
            raise ValueError("Неполная ссылка Shadowsocks")
        if one(q, "plugin"):
            raise ValueError("Плагины Shadowsocks не поддерживаются")
        outbound = {
            "protocol": "shadowsocks",
            "settings": {
                "servers": [
                    {
                        "address": host,
                        "port": port,
                        "method": method,
                        "password": password,
                    }
                ]
            },
        }
    elif scheme in ("http", "socks", "socks5"):
        host, port = p.hostname, p.port
        if not host or not port:
            raise ValueError("Укажите адрес и порт прокси")
        server = {"address": host, "port": port}
        if p.username:
            server["users"] = [
                {
                    "user": urllib.parse.unquote(p.username),
                    "pass": urllib.parse.unquote(p.password or ""),
                }
            ]
        outbound = {
            "protocol": "http" if scheme == "http" else "socks",
            "settings": {"servers": [server]},
        }
    else:
        raise ValueError(
            "Нужна обычная ссылка VLESS, VMess, Trojan, Shadowsocks, HTTP или SOCKS5"
        )
    outbound["tag"] = "provider"
    return {
        "id": hashlib.sha256(link.encode()).hexdigest()[:16],
        "name": (label or host)[:160],
        "protocol": outbound["protocol"],
        "outbound": outbound,
    }


def json_servers(value):
    """Read provider Xray subscription configs, keeping only proxy outbounds."""
    configs = value if isinstance(value, list) else [value]
    servers = []
    for config in configs:
        if not isinstance(config, dict):
            continue
        outbounds = config.get("outbounds", [])
        if config.get("protocol"):
            outbounds = [config]
        if not isinstance(outbounds, list):
            continue
        for outbound in outbounds:
            if not isinstance(outbound, dict) or outbound.get("protocol") not in (
                "vless",
                "vmess",
                "trojan",
                "shadowsocks",
                "http",
                "socks",
            ):
                continue
            proxy = {
                k: outbound[k]
                for k in ("protocol", "settings", "streamSettings", "mux")
                if k in outbound
            }
            if not isinstance(proxy.get("settings"), dict):
                continue
            identity = hashlib.sha256(
                json.dumps(proxy, sort_keys=True).encode()
            ).hexdigest()[:16]
            name = (
                config.get("remarks")
                or config.get("name")
                or outbound.get("tag")
                or "Сервер"
            )
            proxy["tag"] = "provider"
            servers.append(
                {
                    "id": identity,
                    "name": str(name)[:160],
                    "protocol": proxy["protocol"],
                    "outbound": proxy,
                }
            )
    return servers


def parse_subscription(text, *, decoded=False):
    text = text.strip().lstrip("\ufeff")
    text = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    ).strip()
    if text.startswith(("{", "[")):
        try:
            servers = json_servers(json.loads(text))
        except (ValueError, TypeError):
            raise ValueError("Провайдер вернул некорректный JSON подписки") from None
        if servers:
            return list({s["id"]: s for s in servers}.values())[:100], 0
        raise ValueError("В JSON подписки нет поддерживаемых серверов Xray")
    if text.startswith("<"):
        raise ValueError(
            "Провайдер вернул веб-страницу вместо списка серверов. Нужна прямая ссылка подписки."
        )
    if "://" not in text:
        if decoded or not text:
            raise ValueError("Подписка не содержит ссылок серверов")
        try:
            return parse_subscription(decode64("".join(text.split())), decoded=True)
        except (ValueError, UnicodeError, RecursionError):
            raise ValueError(
                "Не удалось прочитать формат подписки провайдера"
            ) from None
    servers, skipped = [], 0
    schemes = set()
    reasons = set()
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            server = parse_link(line)
        except (ValueError, KeyError, TypeError) as error:
            scheme = urllib.parse.urlsplit(line.strip()).scheme
            if scheme in (
                "vless",
                "vmess",
                "trojan",
                "ss",
                "hysteria2",
                "hy2",
                "tuic",
                "happ",
            ):
                schemes.add(scheme)
            if str(error).startswith("Этот транспорт"):
                reasons.add("неподдерживаемый транспорт")
            elif str(error).startswith("Для REALITY"):
                reasons.add("неполные параметры REALITY")
            skipped += 1
            continue
        if server["id"] not in {s["id"] for s in servers}:
            servers.append(server)
        if len(servers) >= 100:
            break
    if not servers:
        detail = (
            " Получены протоколы: " + ", ".join(sorted(schemes)) + "."
            if schemes
            else ""
        )
        if reasons:
            detail += " Причина: " + ", ".join(sorted(reasons)) + "."
        raise ValueError(
            "Поддерживаемых серверов не найдено."
            + detail
            + " Нужна обычная подписка VLESS/VMess/Trojan/Shadowsocks."
        )
    return servers, skipped


def public_https(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password:
        raise ValueError("Подписка должна быть HTTPS-ссылкой")
    for info in socket.getaddrinfo(p.hostname, p.port or 443, type=socket.SOCK_STREAM):
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise ValueError("Подписка должна находиться в интернете")
    return url


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def import_servers(source, subscription=False):
    source = source.strip()
    if not source:
        raise ValueError("Вставьте ссылку подписки или параметры сервера")
    if subscription:
        public_https(source)
        identity = device_headers()
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), SafeRedirect()
        )
        error = None
        for agent in ("v2rayNG/1.10.9", "Happ/3.0.0"):
            try:
                request = urllib.request.Request(
                    source,
                    headers={
                        "User-Agent": agent,
                        "Accept": "text/plain, application/json;q=0.9, */*;q=0.1",
                        **identity,
                    },
                )
                with opener.open(request, timeout=12) as response:
                    check_device_limit(getattr(response, "headers", {}))
                    raw = response.read(524289)
                if len(raw) > 524288:
                    raise ValueError("Слишком большая подписка")
                servers, skipped = parse_subscription(raw.decode("utf-8-sig"))
                if any(provider_notice(server) for server in servers):
                    raise ValueError(
                        "Провайдер вернул инструкцию по HWID вместо серверов. Обновите подписку; при необходимости проверьте лимит устройств у провайдера."
                    )
                break
            except urllib.error.HTTPError as exc:
                check_device_limit(exc.headers or {})
                error = ValueError(
                    "Сервис подписки ответил HTTP %d. Проверьте действительность ссылки."
                    % exc.code
                )
            except (urllib.error.URLError, TimeoutError, OSError):
                error = ValueError(
                    "Не удалось загрузить подписку. Проверьте интернет на колонке."
                )
            except (ValueError, UnicodeError) as exc:
                error = (
                    exc
                    if type(exc) is ValueError
                    else ValueError("Не удалось прочитать формат подписки")
                )
        else:
            raise error
    else:
        servers, skipped = parse_subscription(source)
    with LOCK:
        old = read_json(STORE, {})
        selected = old.get("selected")
        write_json(
            STORE,
            {
                "servers": servers,
                "selected": (
                    selected if selected in {s["id"] for s in servers} else None
                ),
            },
        )
    return dict(listing(), skipped=skipped)


def listing():
    record = read_json(STORE, {})
    if not record and CONFIG.exists():
        # Adopt the already-working installation without exposing provider keys.
        active = read_json(CONFIG, {}).get("outbounds", [])
        supported = next(
            (
                x
                for x in active
                if x.get("protocol")
                in ("vless", "vmess", "trojan", "shadowsocks", "http", "socks")
            ),
            None,
        )
        if supported:
            record = {
                "selected": "saved-current",
                "servers": [
                    {
                        "id": "saved-current",
                        "name": "Текущее подключение",
                        "protocol": supported["protocol"],
                        "outbound": supported,
                    }
                ],
            }
            write_json(STORE, record)
    mode = settings()["vpn_mode"]
    try:
        pid = int(pathlib.Path("/run/alisa-xray.pid").read_text())
        running = b"alisa-xray" in pathlib.Path("/proc/%d/cmdline" % pid).read_bytes()
    except (OSError, ValueError):
        running = False
    return {
        "mode": settings()["vpn_mode"],
        "selected": record.get("selected"),
        "servers": [
            {k: s[k] for k in ("id", "name", "protocol")}
            for s in record.get("servers", [])
            if not provider_notice(s)
        ],
        "configured": CONFIG.exists(),
        "active": mode == "xray" and running,
    }


def set_mode(mode):
    if mode not in ("direct", "xray"):
        raise ValueError("Неизвестный режим подключения")
    action = "stop" if mode == "direct" else "start"
    if mode == "direct" or CONFIG.exists():
        try:
            subprocess.run(
                ["/etc/init.d/S98xray", action],
                check=True,
                capture_output=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            raise ValueError("Не удалось изменить подключение VPN") from None
    update(vpn_mode=mode)
    return listing()


def select_server(server_id):
    with LOCK:
        record = read_json(STORE, {})
        server = next(
            (s for s in record.get("servers", []) if s["id"] == server_id), None
        )
        if not server:
            raise ValueError("Выберите сервер из списка")
        if (
            server_id == "saved-current"
            and record.get("selected") == server_id
            and CONFIG.exists()
        ):
            return set_mode("xray")
        if not pathlib.Path("/tmp/alisa-xray").exists():
            subprocess.run(
                ["/etc/init.d/S98xray", "start"],
                check=True,
                capture_output=True,
                timeout=20,
            )
        candidate = {
            "log": {"loglevel": "warning"},
            "inbounds": [
                {
                    "listen": "127.0.0.1",
                    "port": 18780,
                    "protocol": "http",
                    "settings": {},
                }
            ],
            "outbounds": [server["outbound"]],
        }
        staged = CONFIG.with_suffix(".candidate.json")
        write_json(staged, candidate)
        try:
            result = subprocess.run(
                ["/tmp/alisa-xray", "run", "-test", "-config", str(staged)],
                capture_output=True,
                timeout=10,
            )
            if result.returncode:
                raise ValueError(
                    "Xray не принял параметры сервера; проверьте ссылку и поддерживаемый транспорт"
                )
            previous = CONFIG.read_bytes() if CONFIG.exists() else None
            staged.replace(CONFIG)
            try:
                subprocess.run(
                    ["/etc/init.d/S98xray", "restart"],
                    check=True,
                    capture_output=True,
                    timeout=20,
                )
            except (subprocess.SubprocessError, OSError):
                if previous is not None:
                    CONFIG.write_bytes(previous)
                subprocess.run(
                    ["/etc/init.d/S98xray", "start"], capture_output=True, timeout=20
                )
                raise ValueError(
                    "Не удалось запустить VPN; прежние настройки восстановлены"
                ) from None
            record["selected"] = server_id
            write_json(STORE, record)
            update(vpn_mode="xray")
        finally:
            staged.unlink(missing_ok=True)
    return listing()
