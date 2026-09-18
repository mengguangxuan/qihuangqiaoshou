"""Local voice service. Python 3.11+, no third-party runtime dependencies."""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
KNOWLEDGE = json.loads((Path(__file__).parent / "knowledge" / "acupoints.json").read_text(encoding="utf-8"))
POINTS = {p["id"]: p for p in KNOWLEDGE["points"]}
ROBOT_POINTS = {"GV14", "SI11-L", "SI11-R", "BL23-L", "BL23-R"}
STYLES = {"brief": "约80至140个中文字，先回答重点。", "detail": "约180至300个中文字，分两三个短段讲解。", "child": "约80至160个中文字，用小学生能懂的词语和贴切比喻。"}
DEFAULTS = {"OPENAI_API_KEY": "", "OPENAI_CHAT_MODEL": "gpt-4.1-mini", "OPENAI_TRANSCRIBE_MODEL": "gpt-4o-transcribe", "OPENAI_TTS_MODEL": "gpt-4o-mini-tts", "OPENAI_TTS_VOICE": "coral"}
DEFAULTS.update(DOUBAO_TTS_API_KEY="", DOUBAO_TTS_VOICE="zh_female_vv_uranus_bigtts", DOUBAO_TTS_RESOURCE_ID="seed-tts-2.0")
ROBOT_ROUTES = {"/status.json", "/stream.mjpg", "/snapshot", "/api/acupoints", "/api/preflight", "/api/teach", "/api/delete-keyframe", "/api/execute", "/api/return", "/api/emergency-stop", "/api/robot-enable", "/api/robot-disable", "/api/needle/add-step", "/api/needle/record-step", "/api/needle/delete-step", "/api/needle/move-step", "/api/needle/execute"}


class VoiceError(Exception):
    def __init__(self, message: str, code: str = "invalid_request", status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


def config() -> dict:
    values = dict(DEFAULTS)
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key in values:
                values[key] = value.strip().strip('"').strip("'")
    for key in values:
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def clean_text(value: object, limit: int, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise VoiceError(f"{name}不能为空，且不能超过 {limit} 字。")
    return value.strip()


def upstream_error(status: int) -> VoiceError:
    errors = {401: ("API Key 无效，请检查本机 .env 配置。", "invalid_key"), 403: ("当前账号或所在网络没有访问该接口的权限。", "access_denied"), 404: ("当前账号无法使用配置的模型，请检查模型名称和权限。", "model_unavailable"), 429: ("OpenAI 配额不足或请求过于频繁，请检查余额并稍后重试。", "rate_limit")}
    message, code = errors.get(status, ("OpenAI 暂时未能完成请求，请稍后重试。", "upstream_error"))
    return VoiceError(message, code, 502)


def openai_request(endpoint: str, payload: dict | bytes, cfg: dict, content_type: str = "application/json") -> tuple[bytes, str]:
    key = cfg["OPENAI_API_KEY"].strip()
    if not key or key.startswith("YOUR_"):
        raise VoiceError("还没有填写 OpenAI API Key。请在本机 .env 中填写后点击“检查配置”。", "not_configured", 503)
    data = json.dumps(payload, ensure_ascii=False).encode() if isinstance(payload, dict) else payload
    request = Request("https://api.openai.com/v1/" + endpoint, data=data, headers={"Authorization": "Bearer " + key, "Content-Type": content_type}, method="POST")
    try:
        with urlopen(request, timeout=65) as response:
            return response.read(), response.headers.get_content_type()
    except HTTPError as error:
        raise upstream_error(error.code) from None
    except (URLError, TimeoutError, OSError):
        raise VoiceError("连接 OpenAI 失败或超时，请检查本机网络后重试。", "network_error", 504) from None


def doubao_speech(payload: dict, cfg: dict) -> bytes:
    key = cfg["DOUBAO_TTS_API_KEY"].strip()
    if not key or key.startswith("YOUR_"):
        raise VoiceError("请在本机 .env 填写 DOUBAO_TTS_API_KEY。", "not_configured", 503)
    request = Request("https://openspeech.bytedance.com/api/v3/tts/unidirectional/sse",
                      data=json.dumps(payload, ensure_ascii=False).encode(), method="POST",
                      headers={"Content-Type": "application/json", "X-Api-Key": key,
                               "X-Api-Resource-Id": cfg["DOUBAO_TTS_RESOURCE_ID"],
                               "X-Api-Request-Id": secrets.token_hex(16)})
    chunks = []
    try:
        with urlopen(request, timeout=65) as response:
            for line in response:
                if not line.startswith(b"data:"):
                    continue
                event = json.loads(line[5:].strip())
                if not isinstance(event, dict):
                    raise ValueError("invalid event")
                if event.get("code", 0) not in (0, 20000000):
                    raise VoiceError("豆包语音合成失败，请检查语音服务是否开通、额度和音色权限。", "doubao_error", 502)
                if event.get("data"):
                    chunks.append(base64.b64decode(event["data"], validate=True))
    except HTTPError as error:
        if error.code in (401, 403):
            raise VoiceError("豆包语音鉴权失败，请检查语音 API Key 和语音合成模型 2.0 的开通权限。", "doubao_auth", 502) from None
        raise VoiceError("豆包语音请求失败，请检查服务额度并稍后重试。", "doubao_error", 502) from None
    except (URLError, TimeoutError, OSError):
        raise VoiceError("连接豆包语音失败或超时，请检查网络后重试。", "network_error", 504) from None
    except (ValueError, TypeError, binascii.Error):
        raise VoiceError("豆包返回的音频格式无效，请重试。", "invalid_audio", 502) from None
    if not chunks:
        raise VoiceError("豆包未返回音频，请重试。", "empty_audio", 502)
    return b"".join(chunks)


class VoiceService:
    def __init__(self, provider=openai_request, config_loader=config, doubao_provider=doubao_speech):
        self.provider, self.config_loader = provider, config_loader
        self.doubao_provider = doubao_provider
        self.audio_cache: dict[str, bytes] = {}
        self.cache_lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(3)

    def status(self):
        cfg = self.config_loader()
        key = cfg["OPENAI_API_KEY"].strip()
        doubao = bool(cfg["DOUBAO_TTS_API_KEY"].strip())
        return {"ok": True, "service": "qihuang-voice", "configured": bool(key and not key.startswith("YOUR_")), "models": {"chat": cfg["OPENAI_CHAT_MODEL"], "transcribe": cfg["OPENAI_TRANSCRIBE_MODEL"], "speech": "豆包语音 2.0" if doubao else cfg["OPENAI_TTS_MODEL"]}, "voice": cfg["DOUBAO_TTS_VOICE"] if doubao else cfg["OPENAI_TTS_VOICE"], "speech_provider": "doubao" if doubao else "openai", "max_record_seconds": 45}

    def chat(self, body: dict):
        text = clean_text(body.get("text"), 1500, "问题")
        context = body.get("context", {})
        if not isinstance(context, dict):
            raise VoiceError("页面上下文格式不正确。")
        point_id = context.get("point")
        if point_id not in POINTS:
            point_id = "GV14"
        mode = context.get("mode") if context.get("mode") in {"guide", "needle", "tap", "massage", "cupping"} else "guide"
        style = body.get("style", "brief")
        if style not in STYLES:
            raise VoiceError("讲解风格无效。")
        history = body.get("history", [])
        if not isinstance(history, list):
            raise VoiceError("对话记录格式不正确。")
        messages = []
        for item in history[-12:]:
            if isinstance(item, dict) and item.get("role") in ("user", "assistant") and isinstance(item.get("content"), str):
                messages.append({"role": item["role"], "content": item["content"][:2500]})
        instructions = (
            "你是岐黄巧手展馆的中文讲解员。回答适合直接朗读，不用Markdown、列表符号或表情。"
            "结合页面上下文与对话理解指代。" + STYLES[style] +
            "资料是项目提供的科普草稿，未经逐条权威核验。可以解释这些资料，但不要声称来自已核实古籍、"
            "编造出处、疗效或诊疗建议。不确定时坦诚说明。不得提供针刺深度、用药处方等个体治疗指导。"
            "selected_point仅在用户希望查看/讲解另一个明确穴位时填写其ID，否则为空字符串。"
            "普通的'指出/看看穴位'只切换网页。仅当本轮明确要求机器人或机械臂动作时，intent为robot；"
            "仅支持五个背部演示点，不支持其它运动。无法确定左右时先询问，robot_point留空。"
            "急停请求intent为emergency，提醒使用实体急停；你没有执行过任何动作。"
            "所有动作只能是待确认建议，绝不能声称已经运动。其余intent为knowledge。"
            "source_ids只填写实际使用的资料ID，若回答不依赖所给资料可为空数组。"
        )
        messages.append({"role": "user", "content": json.dumps({"question": text, "page": {"point": point_id, "mode": mode, "audience": context.get("audience", "mannequin")}, "reference_material": KNOWLEDGE}, ensure_ascii=False)})
        schema = {"type": "object", "properties": {"reply": {"type": "string"}, "selected_point": {"type": "string", "enum": ["", *POINTS]}, "intent": {"type": "string", "enum": ["knowledge", "robot", "emergency"]}, "robot_point": {"type": "string", "enum": ["", *sorted(ROBOT_POINTS)]}, "source_ids": {"type": "array", "items": {"type": "string", "enum": list(POINTS)}}}, "required": ["reply", "selected_point", "intent", "robot_point", "source_ids"], "additionalProperties": False}
        started = time.monotonic()
        cfg = self.config_loader()
        data, _ = self.provider("responses", {"model": cfg["OPENAI_CHAT_MODEL"], "store": False, "instructions": instructions, "input": messages, "max_output_tokens": 1000, "text": {"format": {"type": "json_schema", "name": "qihuang_answer", "strict": True, "schema": schema}}}, cfg)
        try:
            response = json.loads(data)
            if response.get("status") == "incomplete":
                raise ValueError("incomplete")
            output = "".join(part.get("text", "") for item in response.get("output", []) for part in item.get("content", []) if part.get("type") == "output_text")
            result = json.loads(output)
            reply = clean_text(result.get("reply"), 2500, "回答")
        except (ValueError, TypeError, AttributeError, VoiceError):
            raise VoiceError("模型没有返回完整回答，请缩短问题后重试。", "invalid_response", 502) from None
        proposal = None
        if result.get("intent") == "emergency" and any(word in text for word in ("急停", "紧急停止", "停止机械臂", "机械臂停下")):
            proposal = {"type": "emergency_stop", "label": "紧急停止"}
        elif result.get("intent") == "robot" and result.get("robot_point") in ROBOT_POINTS and any(word in text for word in ("机械臂", "机器人")):
            code = result["robot_point"]
            proposal = {"type": "execute_acupoint", "point": code, "label": POINTS[code]["name"]}
        selected = result.get("selected_point")
        source_ids = result.get("source_ids", [])
        return {"ok": True, "reply": reply, "selected_point": selected if selected in POINTS else None, "proposal": proposal, "requires_confirmation": bool(proposal), "sources": [{"id": code, "name": POINTS[code]["name"], "label": "项目科普资料（待权威校订）"} for code in source_ids if isinstance(code, str) and code in POINTS], "elapsed_ms": round((time.monotonic() - started) * 1000)}

    def transcribe(self, audio: bytes, mime: str):
        extensions = {"audio/webm": "webm", "video/webm": "webm", "audio/mp4": "mp4", "audio/mpeg": "mp3", "audio/wav": "wav", "audio/x-wav": "wav"}
        mime = mime.split(";")[0].strip()
        if mime not in extensions or len(audio) < 100:
            raise VoiceError("录音为空或格式不支持，请重新录音。")
        boundary = "qihuang_" + secrets.token_hex(12)
        cfg = self.config_loader()
        fields = {"model": cfg["OPENAI_TRANSCRIBE_MODEL"], "language": "zh", "response_format": "json", "prompt": "岐黄巧手穴位文化讲解。词汇：大椎、天宗、肾俞、膻中、中脘、天枢、关元。请忠实转写，不补写未说出的内容。"}
        chunks = []
        for name, value in fields.items():
            chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        chunks.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="question.{extensions[mime]}"\r\nContent-Type: {mime}\r\n\r\n'.encode(), audio, f'\r\n--{boundary}--\r\n'.encode()])
        data, _ = self.provider("audio/transcriptions", b"".join(chunks), cfg, "multipart/form-data; boundary=" + boundary)
        try:
            text = json.loads(data).get("text", "").strip()
        except (ValueError, AttributeError):
            raise VoiceError("语音识别返回了无效结果，请重试。", "invalid_response", 502) from None
        if not text:
            raise VoiceError("没有识别到有效语音，请靠近麦克风重试。", "no_speech", 422)
        return {"ok": True, "text": text[:1500]}

    def speech(self, body: dict):
        text = clean_text(body.get("text"), 2500, "朗读文本")
        cfg = self.config_loader()
        payload = {"model": cfg["OPENAI_TTS_MODEL"], "voice": cfg["OPENAI_TTS_VOICE"], "input": text, "response_format": "mp3", "instructions": "请用标准普通话，像耐心的展馆讲解员一样自然讲述。温和清晰，语速适中，句间略作停顿，不夸张表演。注意穴位名称读音：大椎（dà zhuī）、肾俞（shèn shū）、膻中（dàn zhōng）。"}
        doubao = bool(cfg["DOUBAO_TTS_API_KEY"].strip())
        if doubao:
            payload = {"user": {"uid": "qihuang-local"}, "req_params": {"text": text, "speaker": cfg["DOUBAO_TTS_VOICE"], "sample_rate": 24000, "audio_params": {"format": "mp3", "bit_rate": 64000, "speech_rate": 0, "loudness_rate": 0}}}
        cache_key = hashlib.sha256(json.dumps(["doubao" if doubao else "openai", payload], ensure_ascii=False).encode()).hexdigest()
        with self.cache_lock:
            cached = self.audio_cache.get(cache_key)
        if cached is not None:
            return cached
        if doubao:
            data = self.doubao_provider(payload, cfg)
        else:
            data, _ = self.provider("audio/speech", payload, cfg)
        if not data:
            raise VoiceError("语音合成未返回音频，请重试。", "empty_audio", 502)
        with self.cache_lock:
            if len(self.audio_cache) >= 32:
                self.audio_cache.pop(next(iter(self.audio_cache)))
            self.audio_cache[cache_key] = data
        return data


def make_handler(service: VoiceService, robot_base="http://127.0.0.1:8008"):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Do not log transcripts, credentials or upstream response bodies.

        def send(self, status, data, content_type="application/json; charset=utf-8"):
            if isinstance(data, dict):
                data = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def trusted(self):
            host = self.headers.get("Host", "")
            if host not in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}:
                raise VoiceError("仅允许本机访问。", "forbidden", 403)
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + host:
                raise VoiceError("不接受跨站请求。", "forbidden", 403)

        def body(self, limit):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise VoiceError("请求长度无效。") from None
            if length <= 0 or length > limit:
                raise VoiceError("请求为空或超过大小限制。", "too_large", 413)
            return self.rfile.read(length)

        def proxy_robot(self, path, data=None):
            try:
                request = Request(robot_base + path, data=data, headers={"Content-Type": "application/json"})
                with urlopen(request, timeout=5) as response:
                    if path == "/stream.mjpg":
                        self.send_response(200)
                        self.send_header("Content-Type", response.headers.get("Content-Type", "multipart/x-mixed-replace; boundary=frame"))
                        self.end_headers()
                        while chunk := response.read(8192):
                            self.wfile.write(chunk)
                        return
                    self.send(200, response.read(), response.headers.get("Content-Type", "application/json"))
            except HTTPError as error:
                self.send(error.code, {"ok": False, "error": "机器人后端未接受请求，请查看机器人控制台。"})
            except (URLError, TimeoutError, OSError):
                self.send(503, {"ok": False, "error": "机器人后端未连接；语音讲解可独立使用。"})

        def handle_request(self, post=False):
            acquired = False
            try:
                self.trusted()
                path = urlsplit(self.path).path
                if path in ROBOT_ROUTES:
                    return self.proxy_robot(path, self.body(256_000) if post else None)
                if not post:
                    if path == "/api/voice/status":
                        return self.send(200, service.status())
                    relative = unquote(path).lstrip("/") or "index.html"
                    target = (DIST / relative).resolve()
                    if not target.is_relative_to(DIST.resolve()) or any(part.startswith(".") for part in Path(relative).parts) or not target.is_file():
                        return self.send(404, {"ok": False, "error": "文件不存在。"})
                    mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
                    return self.send(200, target.read_bytes(), mime + ("; charset=utf-8" if mime.startswith("text/") or mime == "application/javascript" else ""))
                if path not in {"/api/voice/chat", "/api/voice/transcribe", "/api/voice/speech"}:
                    return self.send(404, {"ok": False, "error": "接口不存在。"})
                acquired = service.slots.acquire(blocking=False)
                if not acquired:
                    raise VoiceError("正在处理其他请求，请稍后再试。", "busy", 429)
                if path.endswith("/transcribe"):
                    return self.send(200, service.transcribe(self.body(8_000_000), self.headers.get("Content-Type", "")))
                if self.headers.get_content_type() != "application/json":
                    raise VoiceError("请求必须使用 JSON 格式。")
                body = json.loads(self.body(50_000))
                if not isinstance(body, dict):
                    raise VoiceError("请求内容必须是对象。")
                if path.endswith("/chat"):
                    return self.send(200, service.chat(body))
                return self.send(200, service.speech(body), "audio/mpeg")
            except VoiceError as error:
                self.send(error.status, {"ok": False, "error": str(error), "code": error.code})
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.send(400, {"ok": False, "error": "请求内容格式无效。"})
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            except Exception:
                self.send(500, {"ok": False, "error": "本机语音服务处理失败，请重试。", "code": "internal_error"})
            finally:
                if acquired:
                    service.slots.release()

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request(True)

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(VoiceService()))
    print(f"Qihuang voice: http://127.0.0.1:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
