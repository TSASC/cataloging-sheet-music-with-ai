import unittest

import app as appmod


class FakeResponse:
    def __init__(self, status_code, data=None, text=""):
        self.status_code = status_code
        self._data = data or {}
        self.text = text

    def json(self):
        return self._data


class HarvardApiTest(unittest.TestCase):
    def setUp(self):
        self._orig_post = appmod.requests.post
        self._orig_encode_image = appmod.encode_image
        self._orig_get_default_prompt = appmod.get_default_prompt
        self._orig_call_harvard_gemini = appmod._call_harvard_gemini

    def tearDown(self):
        appmod.requests.post = self._orig_post
        appmod.encode_image = self._orig_encode_image
        appmod.get_default_prompt = self._orig_get_default_prompt
        appmod._call_harvard_gemini = self._orig_call_harvard_gemini

    def test_harvard_uses_documented_v2_endpoint_first(self):
        calls = []

        def fake_post(url, headers, json, timeout):
            calls.append((url, headers, json, timeout))
            return FakeResponse(
                200,
                {"choices": [{"message": {"content": "ok"}}]},
            )

        appmod.requests.post = fake_post

        result = appmod._call_harvard("image", "prompt", "key", "gpt-5.4")

        self.assertEqual(result, "ok")
        self.assertEqual(len(calls), 1)
        url, headers, data, timeout = calls[0]
        self.assertEqual(
            url,
            "https://apis.huit.harvard.edu/ais-openai-direct/v2/chat/completions",
        )
        self.assertEqual(headers["X-Api-Key"], "key")
        self.assertNotIn("api-key", headers)
        self.assertEqual(timeout, 120)
        self.assertEqual(data["model"], "gpt-5.4")

    def test_harvard_falls_back_to_legacy_endpoint_on_auth_failure(self):
        calls = []

        def fake_post(url, headers, json, timeout):
            calls.append((url, headers))
            if len(calls) == 1:
                return FakeResponse(403, text="forbidden")
            return FakeResponse(
                200,
                {"choices": [{"message": {"content": "legacy ok"}}]},
            )

        appmod.requests.post = fake_post

        result = appmod._call_harvard("image", "prompt", "key", "gpt-5.4")

        self.assertEqual(result, "legacy ok")
        self.assertEqual(
            calls,
            [
                (
                    "https://apis.huit.harvard.edu/ais-openai-direct/v2/chat/completions",
                    {"Content-Type": "application/json", "X-Api-Key": "key"},
                ),
                (
                    "https://go.apis.huit.harvard.edu/ais-openai-direct/v1/chat/completions",
                    {"Content-Type": "application/json", "api-key": "key"},
                ),
            ],
        )

    def test_harvard_gemini_uses_huit_endpoint_and_user_role(self):
        calls = []

        def fake_post(url, headers, json, timeout):
            calls.append((url, headers, json, timeout))
            return FakeResponse(
                200,
                {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]},
            )

        appmod.requests.post = fake_post

        result = appmod._call_harvard_gemini(
            "image",
            "prompt",
            "key",
            "gemini-2.5-flash",
        )

        self.assertEqual(result, "ok")
        self.assertEqual(len(calls), 1)
        url, headers, data, timeout = calls[0]
        self.assertEqual(
            url,
            "https://apis.huit.harvard.edu/ais-google-gemini/v1beta/models/gemini-2.5-flash:generateContent",
        )
        self.assertEqual(headers["X-Api-Key"], "key")
        self.assertNotIn("api-key", headers)
        self.assertEqual(timeout, 120)
        self.assertEqual(data["contents"][0]["role"], "user")
        self.assertEqual(data["contents"][0]["parts"][0]["inline_data"]["data"], "image")
        self.assertEqual(data["contents"][0]["parts"][1]["text"], "prompt")

    def test_harvard_gemini_dispatches_from_image_to_text(self):
        calls = []

        def fake_call(base64_image, prompt, api_key, model):
            calls.append((base64_image, prompt, api_key, model))
            return "ok"

        appmod.encode_image = lambda path: "encoded"
        appmod.get_default_prompt = lambda: "default prompt"
        appmod._call_harvard_gemini = fake_call

        result = appmod.image_to_text(
            "scan.jpg",
            "key",
            provider="harvard_gemini",
            model="gemini-2.5-flash",
        )

        self.assertEqual(result, "ok")
        self.assertEqual(calls, [("encoded", "default prompt", "key", "gemini-2.5-flash")])

    def test_harvard_gemini_reuses_harvard_key_slot(self):
        client = appmod.app.test_client()
        response = client.post(
            "/settings",
            data={"action": "save_key", "provider": "harvard_gemini", "api_key": "shared-key"},
        )
        self.assertEqual(response.status_code, 302)
        with client:
            client.get("/settings")
            self.assertEqual(appmod.get_api_key_for_provider("harvard"), "shared-key")
            self.assertEqual(appmod.get_api_key_for_provider("harvard_gemini"), "shared-key")
        self.assertEqual(appmod._provider_key_id("harvard_gemini"), "harvard")
        self.assertEqual(
            appmod._expand_provider_key_aliases({"harvard": {"saved": True}})["harvard_gemini"],
            {"saved": True},
        )

    def test_harvard_gemini_active_provider_uses_session_harvard_key(self):
        with appmod.app.test_request_context("/"):
            appmod.session["active_provider"] = "harvard_gemini"
            appmod.session["api_keys"] = {"harvard": appmod.encrypt_api_key("shared-key")}

            key, provider = appmod.get_active_api_key()

        self.assertEqual(key, "shared-key")
        self.assertEqual(provider, "harvard_gemini")


if __name__ == "__main__":
    unittest.main()
