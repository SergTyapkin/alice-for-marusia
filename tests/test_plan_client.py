import io, json, tempfile, time, unittest, urllib.request
from pathlib import Path
from unittest.mock import patch
from alice_for_marusia import openai_client as plan_client


class Stream:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def __iter__(self):
        for event in [
            {"type": "response.output_text.delta", "delta": "Короткий ответ."},
            {"type": "response.completed", "response": {"output": []}},
        ]:
            yield ("data: " + json.dumps(event) + "\n\n").encode()


class PlanTests(unittest.TestCase):
    def test_subscription_protocol_and_prompt(self):
        s = plan_client.PlanSession(Path("unused"))
        with patch.object(s, "token", return_value="x"), patch(
            "alice_for_marusia.openai_client._post", return_value=Stream()
        ) as post:
            result = s.ask("Привет", model="gpt-6-luna", search=True)
            request = json.loads(post.call_args.args[1])
        self.assertEqual(result["text"], "Короткий ответ.")
        self.assertTrue(request["stream"])
        self.assertFalse(request["store"])
        self.assertEqual(request["model"], "gpt-6-luna")
        self.assertNotIn("max_output_tokens", request)
        self.assertIn("Твои ответы озвучиваются", request["instructions"])
        self.assertEqual(request["tools"], [{"type": "web_search"}])

    def test_incomplete_response_is_error(self):
        s = plan_client.PlanSession(Path("unused"))

        class Bad(Stream):
            def __iter__(self):
                yield b'data: {"type":"response.output_text.delta","delta":"Partial"}\n\n'

        with patch.object(s, "token", return_value="x"), patch(
            "alice_for_marusia.openai_client._post", return_value=Bad()
        ):
            with self.assertRaises(RuntimeError):
                s.ask("Hi")

    def test_delta_callback_runs_before_completion(self):
        s = plan_client.PlanSession(Path("unused"))
        seen = []
        with patch.object(s, "token", return_value="x"), patch(
            "alice_for_marusia.openai_client._post", return_value=Stream()
        ):
            result = s.ask("Привет", search=False, on_delta=seen.append)
        self.assertEqual(seen, ["Короткий ответ."])
        self.assertEqual(result["text"], "Короткий ответ.")

    def test_proxy_applies_only_to_openai_and_never_falls_back(self):
        req = urllib.request.Request("https://api.openai.com/v1/models")
        with patch.dict(
            "os.environ", {"ALISA_OPENAI_PROXY": "http://127.0.0.1:18780"}
        ), patch(
            "alice_for_marusia.openai_client.urllib.request.build_opener"
        ) as build, patch(
            "alice_for_marusia.openai_client.urllib.request.urlopen"
        ) as direct:
            build.return_value.open.side_effect = OSError("proxy unavailable")
            with self.assertRaises(OSError):
                plan_client._open(req, 3)
            direct.assert_not_called()
            with self.assertRaises(ValueError):
                plan_client._open(urllib.request.Request("https://example.com/"), 3)

    def test_proxy_rejects_nonlocal_proxy(self):
        with patch.dict(
            "os.environ", {"ALISA_OPENAI_PROXY": "http://example.com:18780"}
        ):
            with self.assertRaises(ValueError):
                plan_client._open(
                    urllib.request.Request("https://api.openai.com/v1/models"), 3
                )

    def test_local_proxy_cannot_be_bypassed_by_no_proxy(self):
        req = urllib.request.Request("https://api.openai.com/v1/models")
        with patch.dict("os.environ", {"no_proxy": "*"}):
            plan_client._ForcedLocalProxy(
                {"https": "http://127.0.0.1:18780"}
            ).proxy_open(req, "http://127.0.0.1:18780", "https")
        self.assertEqual(req.host, "127.0.0.1:18780")
        self.assertEqual(req._tunnel_host, "api.openai.com")


if __name__ == "__main__":
    unittest.main()
