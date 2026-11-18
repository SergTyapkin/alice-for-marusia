import base64, json, pathlib, tempfile, unittest
from unittest.mock import patch
from alice_for_marusia import settings as config
from alice_for_marusia.vpn import parse_subscription, parse_link
from alice_for_marusia.auth import validate_identity


class SetupTests(unittest.TestCase):
    def test_threshold_default_limits_and_persistence(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            config, "DATA", pathlib.Path(tmp)
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
