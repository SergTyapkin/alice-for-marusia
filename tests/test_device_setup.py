import base64, json, pathlib, tempfile, unittest
from unittest.mock import patch
from alice_for_marusia import settings as config
from alice_for_marusia.vpn import parse_subscription, parse_link
from alice_for_marusia.auth import validate_identity


class SetupTests(unittest.TestCase):
    def test_threshold_default_limits_and_persistence(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(config, "DATA", pathlib.Path(tmp)),
        ):
            self.assertEqual(config.settings()["wake_threshold"], 70)
            for invalid in (59, 81, 65.5, True):
                with self.assertRaises(ValueError):
                    config.update(wake_threshold=invalid)
            config.update(wake_threshold=76)
            self.assertEqual(config.settings()["wake_threshold"], 76)
            config.update(wake_threshold=70)
            self.assertEqual(config.settings()["wake_threshold"], 70)

    def test_provider_subscription_preserves_server_names(self):
        links = (
            "vless://00000000-0000-0000-0000-000000000001@one.example:443?security=tls&sni=one.example#Netherlands\n"
            + "trojan://secret@two.example:443?security=tls#Germany"
        )
        encoded = base64.b64encode(links.encode()).decode()
        servers, skipped = parse_subscription(encoded)
        self.assertEqual([s["name"] for s in servers], ["Netherlands", "Germany"])
        self.assertEqual(skipped, 0)
        self.assertEqual(
            servers[1]["outbound"]["settings"]["servers"][0]["password"], "secret"
        )

    def test_unsupported_happ_is_not_treated_as_xray(self):
        with self.assertRaises(ValueError):
            parse_subscription("happ://encrypted/example")

    def test_realistic_reality_parameters(self):
        s = parse_link(
            "vless://id@server.example:443?security=reality&sni=example.org&pbk=public&sid=1234&type=grpc&serviceName=voice#DE"
        )
        stream = s["outbound"]["streamSettings"]
        self.assertEqual(stream["realitySettings"]["publicKey"], "public")
        self.assertEqual(stream["grpcSettings"]["serviceName"], "voice")

    def test_xray_json_subscription_ignores_direct_outbound(self):
        config = [
            {
                "remarks": "Netherlands",
                "outbounds": [
                    {
                        "protocol": "vless",
                        "settings": {
                            "vnext": [
                                {
                                    "address": "example.org",
                                    "port": 443,
                                    "users": [{"id": "test"}],
                                }
                            ]
                        },
                    },
                    {"protocol": "freedom", "settings": {}},
                ],
            }
        ]
        servers, skipped = parse_subscription(json.dumps(config))
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]["name"], "Netherlands")
        self.assertEqual(servers[0]["protocol"], "vless")

    def test_base64_subscription_accepts_line_breaks(self):
        source = "vless://id@example.org:443?security=tls#DE"
        encoded = base64.b64encode(source.encode()).decode()
        servers, _ = parse_subscription(encoded[:16] + "\n" + encoded[16:])
        self.assertEqual(servers[0]["name"], "DE")

    def test_import_is_not_blocked_by_chatgpt_check(self):
        import threading
        from alice_for_marusia.web import Application, OperationBusy

        app = Application(None)
        release = threading.Event()
        try:
            app.background(lambda: release.wait(2), kind="check")
            app.background(lambda: None, kind="vpn-import")
            with self.assertRaises(OperationBusy):
                app.background(lambda: None, kind="check")
        finally:
            release.set()

    def test_subscription_metadata_does_not_hide_base64_payload(self):
        link = "vless://id@example.org:443?security=tls#DE"
        body = (
            "#support-url: https://support.example.org\n"
            + base64.b64encode(link.encode()).decode()
        )
        servers, _ = parse_subscription(body)
        self.assertEqual(servers[0]["name"], "DE")

    def test_disconnect_stops_proxy_and_selects_direct_mode(self):
        from alice_for_marusia import vpn

        with (
            patch.object(vpn.subprocess, "run") as run,
            patch.object(vpn, "update") as update,
            patch.object(vpn, "listing", return_value={}),
        ):
            vpn.set_mode("direct")
        self.assertEqual(run.call_args.args[0], ["/etc/init.d/S98xray", "stop"])
        update.assert_called_once_with(vpn_mode="direct")

    def test_login_network_error_names_stage_without_leaking_code(self):
        import time
        from alice_for_marusia import auth

        login = auth.ChatLogin()
        login.pending = {
            "at": time.monotonic(),
            "state": "test-state",
            "client": "dynamic_agent_client",
            "verifier": "secret-verifier",
        }
        login.status = "waiting"
        callback = (
            auth.REDIRECT + "?state=test-state&code=secret-code&client_id=issued-client"
        )
        with (
            patch.object(auth, "_post", side_effect=RuntimeError("OpenAI HTTP 403")),
            patch.object(auth, "event") as logged,
        ):
            with self.assertRaises(ValueError) as error:
                login.submit(callback)
        self.assertIn("403", str(error.exception))
        self.assertNotIn("secret", str(error.exception))
        self.assertNotIn("secret", str(logged.call_args))

    def test_downloaded_subscription_decodes_utf8_bom_and_saves_servers(self):
        import io
        from alice_for_marusia import vpn

        payload = b"\xef\xbb\xbf" + base64.b64encode(
            b"vless://id@example.org:443?security=tls#DE"
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = pathlib.Path(tmp) / "servers.json"
            with (
                patch.object(vpn, "public_https"),
                patch.object(vpn, "STORE", store),
                patch.object(vpn, "DATA", pathlib.Path(tmp)),
                patch.object(vpn, "listing", return_value={"servers": []}),
                patch.object(vpn.urllib.request, "build_opener") as build,
            ):
                build.return_value.open.return_value = io.BytesIO(payload)
                vpn.import_servers(
                    "https://subscription.example.org/sub/test", subscription=True
                )
            saved = json.loads(store.read_text(encoding="utf8"))
            self.assertEqual(saved["servers"][0]["name"], "DE")
            self.assertEqual(saved["servers"][0]["protocol"], "vless")

    def test_hwid_is_stable_and_provider_device_limit_is_respected(self):
        import re
        from alice_for_marusia import vpn

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(vpn, "DATA", pathlib.Path(tmp)),
        ):
            first = vpn.device_headers()["x-hwid"]
            self.assertEqual(vpn.device_headers()["x-hwid"], first)
            self.assertTrue(re.fullmatch(r"[a-zA-Z0-9=-]{10,64}", first))
        with self.assertRaises(ValueError):
            vpn.check_device_limit({"x-hwid-max-devices-reached": "true"})
        self.assertTrue(
            vpn.provider_notice({"name": "INCY: включите HWID в настройках"})
        )

    def test_unsigned_identity_is_rejected(self):
        encode = (
            lambda x: base64.urlsafe_b64encode(json.dumps(x).encode())
            .rstrip(b"=")
            .decode()
        )
        with self.assertRaises(ValueError):
            validate_identity(
                encode({"alg": "none"}) + "." + encode({"sub": "user"}) + ".",
                {"keys": []},
                "client",
                "nonce",
            )


if __name__ == "__main__":
    unittest.main()
