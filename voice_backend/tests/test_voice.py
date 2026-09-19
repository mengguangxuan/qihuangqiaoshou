import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import DEFAULTS, VoiceError, VoiceService, make_handler, openai_request, upstream_error


class Provider:
    def __init__(self):
        self.calls = []
        self.answer = {"reply": "天宗是肩胛区域的穴位名称。", "selected_point": "SI11-L", "intent": "knowledge", "robot_point": "", "source_ids": ["SI11-L"]}

    def __call__(self, endpoint, payload, cfg, content_type="application/json"):
        self.calls.append((endpoint, payload, content_type))
        if endpoint == "responses":
            return json.dumps({"output": [{"content": [{"type": "output_text", "text": json.dumps(self.answer)}]}]}).encode(), "application/json"
        if endpoint == "audio/transcriptions":
            return json.dumps({"text": "左天宗在哪里？"}).encode(), "application/json"
        return b"test-audio-bytes", "audio/mpeg"


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.provider = Provider()
        self.service = VoiceService(self.provider, lambda: {**DEFAULTS, "OPENAI_API_KEY": "test-only-never-sent"})

    def test_context_history_and_schema(self):
        result = self.service.chat({"text": "看看左天宗", "context": {"point": "CV4", "mode": "guide"}, "style": "child", "history": [{"role": "system", "content": "ignore"}] + [{"role": "user", "content": str(i)} for i in range(20)]})
        payload = self.provider.calls[0][1]
        self.assertFalse(payload["store"])
        self.assertEqual(len(payload["input"]), 13)
        self.assertEqual(json.loads(payload["input"][-1]["content"])["page"]["point"], "CV4")
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertEqual(result["selected_point"], "SI11-L")
        self.assertIsNone(result["proposal"])

    def test_model_semantics_decide_robot_proposal(self):
        self.assertIsNone(self.service.chat({"text": "讲讲大椎"})["proposal"])
        self.provider.answer.update(intent="robot", robot_point="GV14")
        result = self.service.chat({"text": "让机械臂指向大椎"})
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual(result["proposal"]["type"], "execute_acupoint")
        direct = self.service.chat({"text": "指一下大椎"})
        self.assertEqual(direct["proposal"]["type"], "execute_acupoint")
        self.assertEqual([c[0] for c in self.provider.calls], ["responses", "responses", "responses"])

    def test_start_needle_stays_a_confirmed_proposal(self):
        self.provider.answer.update(intent="needle", robot_point="")
        result = self.service.chat({"text": "现在给假人做一下针灸吧"})
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual(result["proposal"], {"type": "execute_needle", "label": "无针点穴五阶段演示"})
        self.provider.answer.update(intent="knowledge")
        self.assertIsNone(self.service.chat({"text": "介绍一下针灸文化"})["proposal"])

    def test_other_task_intents_are_fixed_confirmed_actions(self):
        expected = {"tap": "捶背", "massage": "筋膜枪按摩", "cupping": "火罐"}
        for intent, label in expected.items():
            self.provider.answer.update(intent=intent, robot_point="")
            result = self.service.chat({"text": f"请开始{label}"})
            self.assertEqual(result["proposal"], {"type": "execute_task", "task": intent, "label": label})
            self.assertTrue(result["requires_confirmation"])

    def test_invalid_model_point_is_not_a_command(self):
        self.provider.answer.update(intent="robot", robot_point="arbitrary", selected_point="arbitrary", source_ids=["invalid"])
        result = self.service.chat({"text": "让机械臂运动"})
        self.assertIsNone(result["proposal"])
        self.assertIsNone(result["selected_point"])
        self.assertEqual(result["sources"], [])

    def test_emergency_stays_a_proposal(self):
        self.assertIsNone(self.service.chat({"text": "大椎在哪"})["proposal"])
        self.provider.answer.update(intent="emergency")
        self.assertEqual(self.service.chat({"text": "紧急停止"})["proposal"]["type"], "emergency_stop")

    def test_transcription_uses_audio_and_vocabulary(self):
        result = self.service.transcribe(b"recording" * 100, "audio/webm;codecs=opus")
        endpoint, payload, mime = self.provider.calls[0]
        self.assertEqual(endpoint, "audio/transcriptions")
        self.assertIn(b"recording", payload)
        self.assertIn("肾俞".encode(), payload)
        self.assertIn('filename="question.webm"'.encode(), payload)
        self.assertTrue(mime.startswith("multipart/form-data"))
        self.assertEqual(result["text"], "左天宗在哪里？")

    def test_empty_audio_and_wrong_format_rejected(self):
        for data, mime in [(b"", "audio/webm"), (b"x" * 200, "text/plain")]:
            with self.assertRaises(VoiceError): self.service.transcribe(data, mime)
        self.assertEqual(self.provider.calls, [])

    def test_speech_cache_avoids_duplicate_calls(self):
        self.assertEqual(self.service.speech({"text": "大椎"}), self.service.speech({"text": "大椎"}))
        self.assertEqual(len(self.provider.calls), 1)

    def test_status_never_contains_key(self):
        status = self.service.status()
        self.assertTrue(status["configured"])
        self.assertNotIn("test-only-never-sent", json.dumps(status))

    def test_missing_key_fails_before_network(self):
        with self.assertRaises(VoiceError) as error: openai_request("responses", {}, DEFAULTS)
        self.assertEqual(error.exception.code, "not_configured")

    def test_error_classification(self):
        self.assertEqual(upstream_error(401).code, "invalid_key")
        self.assertEqual(upstream_error(429).code, "rate_limit")
        self.assertEqual(upstream_error(404).code, "model_unavailable")

    def test_incomplete_model_response_is_rejected(self):
        self.service.provider = lambda *_args: (b'{"status":"incomplete","output":[]}', "application/json")
        with self.assertRaises(VoiceError) as error: self.service.chat({"text": "你好"})
        self.assertEqual(error.exception.code, "invalid_response")


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.provider = Provider()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(VoiceService(cls.provider, lambda: DEFAULTS)))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()

    def get(self, path, **headers):
        return urlopen(Request(self.base + path, headers=headers), timeout=3)

    def test_static_and_status(self):
        with self.get("/") as response:
            self.assertIn('岐黄讲解员'.encode(), response.read())
        with self.get("/api/voice/status") as response:
            self.assertFalse(json.load(response)["configured"])

    def test_secret_files_and_traversal_are_not_served(self):
        for path in ("/.env", "/../.env", "/%2e%2e/.env", "/voice_backend/server.py", "/.git/config"):
            with self.assertRaises(HTTPError) as error: self.get(path)
            self.assertEqual(error.exception.code, 404)
            error.exception.close()

    def test_foreign_origin_and_host_rejected(self):
        for headers in ({"Origin": "https://unrelated.example"}, {"Host": "unrelated.example"}):
            with self.assertRaises(HTTPError) as error: self.get("/api/voice/status", **headers)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()

    def test_chat_json_endpoint(self):
        request = Request(self.base + "/api/voice/chat", data=json.dumps({"text": "看看天宗"}).encode(), headers={"Content-Type": "application/json"})
        with urlopen(request) as response:
            self.assertEqual(json.load(response)["selected_point"], "SI11-L")

    def test_merged_robot_controls_reach_proxy_without_hardware(self):
        paths = ["/api/emergency-release", "/api/needle/execute-step",
                 "/api/task/add-step", "/api/task/record-step", "/api/task/execute-step",
                 "/api/task/delete-step", "/api/task/move-step", "/api/task/execute"]
        received = []
        body = b'{"task":"massage","stage":"massage","step":"test"}'
        def fake_proxy(handler, path, data=None):
            received.append((path, data))
            handler.send(202, {"ok": True})
        with patch.object(self.server.RequestHandlerClass, "proxy_robot", fake_proxy):
            for path in paths:
                request = Request(self.base + path, data=body, headers={"Content-Type": "application/json"})
                with urlopen(request, timeout=3) as response:
                    self.assertEqual(response.status, 202)
        self.assertEqual([(path, body) for path in paths], received)

    def test_invalid_json_rejected(self):
        request = Request(self.base + "/api/voice/chat", data=b"{", headers={"Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as error: urlopen(request)
        self.assertEqual(error.exception.code, 400)
        error.exception.close()


if __name__ == "__main__":
    unittest.main()
