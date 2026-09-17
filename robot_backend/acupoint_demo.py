#!/usr/bin/env python3
"""Gemini 2 + ArUco mannequin acupoint overlay and guarded robot console."""

from __future__ import annotations

import argparse
import json
import logging
import math
import mimetypes
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from PIL import Image as PilImage
from PIL import ImageDraw, ImageFont

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import Image


LOG = logging.getLogger("acupoint_demo")
PROJECT_DIR = Path(__file__).resolve().parent
WEB_ROOT = PROJECT_DIR.parent / "dist"
CONSOLE_PATH = WEB_ROOT / "index.html"
FLOW_DIR = PROJECT_DIR / "acupoint_flows"
FLOW_RUNNER = PROJECT_DIR / "run_acupoint_flow.py"
NEEDLE_FLOW_PATH = PROJECT_DIR / "needle_flow.json"
NEEDLE_RUNNER = PROJECT_DIR / "run_needle_flow.py"
EVENT_LOG_PATH = PROJECT_DIR / "data" / "acupoint_events.jsonl"
NEEDLE_GESTURES = {
    "pinch": {"label": "捏", "positions": [0, 71, 0, 255, 255, 255]},
    "release": {"label": "放", "positions": [255, 71, 255, 255, 255, 255]},
}

# Canonical coordinates describe the physical tag layout on the mannequin.
# The top/bottom four tags form the main quadrilateral. ID 2 is the neck tag
# and gives an extra constraint when visible.
MARKER_LAYOUT = {
    11: (0.0, 0.0),
    1: (1.0, 0.0),
    3: (0.0, 1.0),
    19: (1.0, 1.0),
    2: (0.5, -0.245),
}

# Widely separated demonstration positions. These are intentionally described
# as demo coordinates, not medical localization results.
ACUPOINTS = (
    (1, "大椎", "GV14", (0.50, -0.20), (30, 220, 255), (24, -13)),
    (2, "左天宗", "SI11-L", (0.23, 0.31), (80, 255, 120), (-214, -13)),
    (3, "右天宗", "SI11-R", (0.77, 0.31), (80, 255, 120), (24, -13)),
    (4, "左肾俞", "BL23-L", (0.32, 0.72), (255, 160, 70), (-214, -13)),
    (5, "右肾俞", "BL23-R", (0.68, 0.72), (255, 160, 70), (24, -13)),
)

ACUPOINT_KNOWLEDGE = {
    "GV14": {
        "name": "大椎",
        "answer": "大椎位于颈背交界的后正中线、第七颈椎棘突下凹陷处。名称中的“大”表示显著、重要，“椎”指椎骨；这里用于文化科普和假人定位，不代替真人诊疗。",
    },
    "SI11-L": {"name": "左天宗", "answer": "左天宗位于左侧肩胛冈下窝中央区域，属于手太阳小肠经的代表穴位之一。"},
    "SI11-R": {"name": "右天宗", "answer": "右天宗位于右侧肩胛冈下窝中央区域，与左侧构成对称参照。"},
    "BL23-L": {"name": "左肾俞", "answer": "左肾俞位于左侧腰背部、第二腰椎棘突下旁开区域，属于足太阳膀胱经的背俞穴体系。"},
    "BL23-R": {"name": "右肾俞", "answer": "右肾俞位于右侧腰背部，与左肾俞构成对称参照。"},
}


def interpret_agent_query(text: str) -> dict:
    """Route a spoken question without allowing unconfirmed robot motion."""
    words = "".join(str(text).strip().split())
    if not words:
        raise ValueError("问题不能为空")
    if any(token in words for token in ("急停", "紧急停止", "停止机械臂", "机械臂停下")):
        return {
            "ok": True,
            "intent": "emergency_stop",
            "reply": "我识别到紧急停止请求。语音不能替代实体急停；页面会提供一次确认，同时请现场人员立即准备按下实体急停按钮。",
            "proposal": {"type": "emergency_stop", "label": "紧急停止"},
            "requires_confirmation": True,
            "model_connected": False,
        }
    matched_code = next(
        (code for code, item in ACUPOINT_KNOWLEDGE.items() if item["name"] in words or code.lower() in words.lower()),
        None,
    )
    asks_action = any(token in words for token in ("指", "指出", "找", "带我看", "运动到", "移动到"))
    if matched_code and asks_action:
        item = ACUPOINT_KNOWLEDGE[matched_code]
        return {
            "ok": True,
            "intent": "point",
            "reply": f"我理解为：让机械臂指向{item['name']}。为防止误识别，我不会立即运动，请在页面上再次确认。",
            "proposal": {"type": "execute_acupoint", "point": matched_code, "label": item["name"]},
            "requires_confirmation": True,
            "model_connected": False,
        }
    if any(token in words for token in ("按摩", "循按", "顺着", "沿着")):
        return {
            "ok": True,
            "intent": "massage",
            "reply": "我识别到按摩轨迹请求，但目前五组示教程序只有穴位指向动作。请先为目标路线示教起点、中间关键帧和撤离位，再开放语音执行。",
            "proposal": None,
            "requires_confirmation": False,
            "model_connected": False,
        }
    if matched_code:
        item = ACUPOINT_KNOWLEDGE[matched_code]
        return {
            "ok": True,
            "intent": "knowledge",
            "reply": item["answer"],
            "proposal": None,
            "requires_confirmation": False,
            "model_connected": False,
        }
    return {
        "ok": True,
        "intent": "model_required",
        "reply": "我已经收到问题，但当前本机尚未配置医学知识大模型，只能可靠回答五个演示穴位并解析已示教动作。接入模型后可扩展为更广泛的医学文化问答。",
        "proposal": None,
        "requires_confirmation": False,
        "model_connected": False,
    }

PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>假人背部穴位实时演示</title>
  <style>
    :root { color-scheme: dark; }
    * { box-sizing: border-box; }
    body { margin: 0; background: #0d141b; color: #edf4f8; font-family: system-ui,"Microsoft YaHei",sans-serif; }
    header { padding: 16px 22px; background: #132331; border-bottom: 1px solid #284052; }
    h1 { margin: 0 0 6px; font-size: 23px; }
    .sub { color: #a9becd; font-size: 14px; }
    main { display: grid; grid-template-columns: minmax(480px,1fr) 290px; gap: 16px; padding: 16px; }
    .panel { background: #121e28; border: 1px solid #263b4a; border-radius: 12px; overflow: hidden; }
    .video { padding: 10px; }
    .video img { display: block; width: 100%; height: auto; background: #05080a; border-radius: 8px; }
    aside { padding: 16px; }
    .pill { display: inline-block; margin: 4px 0 14px; padding: 6px 10px; border-radius: 99px; background: #5b3118; color: #ffd0a5; }
    .pill.ok { background: #173c2b; color: #98f1bd; }
    ol { padding-left: 25px; line-height: 1.9; }
    code { color: #9bd9ff; }
    button { width: 100%; margin-top: 12px; padding: 10px; color: white; background: #176ca5; border: 0; border-radius: 8px; cursor: pointer; }
    #saveResult { min-height: 24px; margin-top: 8px; color: #a9becd; font-size: 13px; word-break: break-all; }
    .warning { margin-top: 16px; padding: 11px; color: #ffd6a1; background: #3b2b18; border-radius: 8px; font-size: 13px; line-height: 1.55; }
    @media(max-width:850px) { main { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <header><h1>假人背部穴位实时演示</h1><div class="sub">Gemini 2 · ArUco DICT_5X5_100 · 仅视觉显示</div></header>
  <main>
    <section class="panel video"><img src="/stream.mjpg" alt="实时穴位画面"></section>
    <aside class="panel">
      <div id="state" class="pill">等待相机画面</div>
      <div id="detail">正在读取状态……</div>
      <h3>演示点位</h3>
      <ol><li>大椎 GV14</li><li>左天宗 SI11-L</li><li>右天宗 SI11-R</li><li>左肾俞 BL23-L</li><li>右肾俞 BL23-R</li></ol>
      <button id="save">保存当前画面</button><div id="saveResult"></div>
      <div class="warning">本页面点位用于机器人比赛演示，不构成真人穴位或医疗定位。程序不会向机械臂发送任何动作。</div>
    </aside>
  </main>
  <script>
    const state = document.getElementById('state');
    const detail = document.getElementById('detail');
    async function refresh() {
      try {
        const s = await (await fetch('/status.json', {cache:'no-store'})).json();
        state.textContent = s.tracking ? '定位正常' : (s.frame_ready ? '等待足够标记' : '等待相机画面');
        state.className = s.tracking ? 'pill ok' : 'pill';
        detail.innerHTML = `帧率：<code>${s.fps}</code><br>本帧标记：<code>${s.visible_ids.join(', ') || '无'}</code><br>参与定位：<code>${s.tracked_ids.join(', ') || '无'}</code><br>图像延迟：<code>${s.frame_age_s ?? '-'} s</code>`;
      } catch (_) { state.textContent = '状态连接中断'; state.className = 'pill'; }
    }
    setInterval(refresh, 500); refresh();
    document.getElementById('save').onclick = async () => {
      const box = document.getElementById('saveResult'); box.textContent = '保存中……';
      const s = await (await fetch('/snapshot', {method:'POST'})).json();
      box.textContent = s.ok ? `已保存：${s.file}` : `失败：${s.error}`;
    };
  </script>
</body></html>"""


@dataclass
class TrackedMarker:
    center: np.ndarray
    last_seen: float


@dataclass
class SharedView:
    snapshot_dir: Path
    lock: threading.Lock = field(default_factory=threading.Lock)
    condition: threading.Condition = field(init=False)
    jpeg: Optional[bytes] = None
    bgr: Optional[np.ndarray] = None
    frame_no: int = 0
    status: dict = field(default_factory=lambda: {
        "frame_ready": False,
        "tracking": False,
        "fps": 0.0,
        "visible_ids": [],
        "tracked_ids": [],
        "acupoints": [],
        "frame_age_s": None,
    })

    def __post_init__(self) -> None:
        self.condition = threading.Condition(self.lock)

    def update(self, bgr: np.ndarray, jpeg: bytes, status: dict) -> None:
        with self.condition:
            self.bgr = bgr
            self.jpeg = jpeg
            self.frame_no += 1
            self.status = status
            self.condition.notify_all()

    def get_status(self) -> dict:
        with self.lock:
            result = dict(self.status)
        received_at = result.pop("received_at", None)
        result["frame_age_s"] = None if received_at is None else round(max(0.0, time.time() - received_at), 2)
        return result

    def save_snapshot(self) -> Path:
        with self.lock:
            image = None if self.bgr is None else self.bgr.copy()
        if image is None:
            raise RuntimeError("还没有收到相机画面")
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        path = self.snapshot_dir / f"acupoints_{time.strftime('%Y%m%d_%H%M%S')}.jpg"
        if not cv2.imwrite(str(path), image):
            raise RuntimeError("保存图片失败")
        return path


class ActionController:
    """Serializes flow execution and keeps hardware opt-in explicit."""

    def __init__(self, shared: SharedView, enabled: bool, namespace: str, speed_scale: float,
                 bridge_url: str) -> None:
        self.shared = shared
        self.enabled = enabled
        self.namespace = namespace
        self.speed_scale = speed_scale
        self.bridge_url = bridge_url.rstrip("/")
        self.lock = threading.Lock()
        self.event_lock = threading.Lock()
        self.events = self._load_events()
        arm_enabled = {"left": False, "right": False}
        restored_arms: set[str] = set()
        for event in self.events:
            arm = str(event.get("output", ""))
            if arm in arm_enabled and event.get("kind") in {"robot_enable", "robot_disable"}:
                arm_enabled[arm] = event["kind"] == "robot_enable"
                restored_arms.add(arm)
        self.state = {
            "enabled": enabled,
            "arm_enabled": arm_enabled,
            "robot_enabled": any(arm_enabled.values()),
            "running": False,
            "point": None,
            "phase": "idle",
            "message": "真机执行未启用" if not enabled else (
                "已恢复上一次左右臂控制状态" if restored_arms else "等待选择穴位"
            ),
            "bridge": {"checked": False, "ok": False, "message": "尚未检查机器人动作桥"},
            "updated_at": time.time(),
        }

    @staticmethod
    def _load_events() -> list[dict]:
        if not EVENT_LOG_PATH.is_file():
            return []
        events: list[dict] = []
        try:
            for line in EVENT_LOG_PATH.read_text(encoding="utf-8").splitlines()[-100:]:
                if line.strip():
                    events.append(json.loads(line))
        except Exception as exc:  # noqa: BLE001
            LOG.warning("读取穴位事件日志失败：%s", exc)
        return events

    def _record_event(self, kind: str, ok: bool, message: str, *, point: Optional[str] = None,
                      keyframe: Optional[str] = None, output: str = "") -> dict:
        event = {
            "id": f"{int(time.time() * 1000)}-{len(self.events) + 1}",
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "kind": kind,
            "ok": bool(ok),
            "point": point,
            "keyframe": keyframe,
            "message": message,
            "output": output,
        }
        with self.event_lock:
            self.events.append(event)
            self.events = self.events[-100:]
            EVENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with EVENT_LOG_PATH.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def history(self) -> list[dict]:
        with self.event_lock:
            return [dict(item) for item in self.events[-50:]]

    def _bridge_request(self, method: str, path: str, body: Optional[dict] = None,
                        timeout: float = 4.0) -> tuple[int, dict]:
        payload = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{self.bridge_url}{path}",
            data=payload,
            method=method,
            headers={"Content-Type": "application/json"} if payload is not None else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                value = json.loads(exc.read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                value = {"error": str(exc)}
            return exc.code, value

    def preflight(self) -> dict:
        result = {
            "checked": True,
            "ok": False,
            "bridge_url": self.bridge_url,
            "message": "机器人动作桥不可用",
            "health": None,
            "state": None,
            "checked_at": time.time(),
        }
        try:
            health_status, health = self._bridge_request("GET", "/health")
            state_status, robot_state = self._bridge_request("GET", "/state")
            result.update({
                "ok": health_status == HTTPStatus.OK and state_status == HTTPStatus.OK and bool(health.get("ok")),
                "health": health,
                "state": robot_state,
                "message": "机器人、ROS2 服务与双臂状态均已就绪"
                if health_status == HTTPStatus.OK and bool(health.get("ok"))
                else "；".join(health.get("errors") or [health.get("error") or "动作桥健康检查未通过"]),
            })
        except Exception as exc:  # noqa: BLE001
            result["message"] = f"无法连接动作桥：{exc}"
        with self.lock:
            self.state["bridge"] = result
            self.state["updated_at"] = time.time()
        return result

    def emergency_stop(self) -> dict:
        try:
            status, payload = self._bridge_request("POST", "/emergency_stop", {"emergency": True})
            if status != HTTPStatus.OK or not payload.get("ok"):
                raise RuntimeError(payload.get("error") or f"动作桥返回 {status}")
            phase, message = "emergency_stop", "已向左右机械臂发送急停；请现场确认实体急停状态"
        except Exception as exc:  # noqa: BLE001
            phase, message = "error", f"软件急停请求失败：{exc}；请立即使用实体急停"
        with self.lock:
            self.state.update({
                "running": False,
                "arm_enabled": {"left": False, "right": False},
                "robot_enabled": False,
                "phase": phase,
                "message": message,
                "updated_at": time.time(),
            })
        if phase == "error":
            raise RuntimeError(message)
        return self.get_state()

    def set_robot_enabled(self, arm: str, enabled: bool) -> dict:
        arm = str(arm or "").strip().lower()
        if arm not in {"left", "right"}:
            raise ValueError("必须指定 arm=left 或 arm=right")
        if enabled and not self.enabled:
            raise RuntimeError("本机服务处于安全预览模式；请用 --enable-robot 重启后再使能")
        with self.lock:
            if self.state["running"]:
                raise RuntimeError("动作正在执行，不能切换使能状态")
        status, payload = self._bridge_request(
            "POST", "/emergency_stop", {"arm": arm, "emergency": not enabled}
        )
        if status != HTTPStatus.OK or not payload.get("ok"):
            raise RuntimeError(payload.get("error") or f"动作桥返回 {status}")
        arm_label = "左臂" if arm == "left" else "右臂"
        message = f"{arm_label}已使能，可以执行对应穴位" if enabled else f"{arm_label}已掉使能"
        with self.lock:
            arm_enabled = dict(self.state["arm_enabled"])
            arm_enabled[arm] = enabled
            self.state.update({
                "arm_enabled": arm_enabled,
                "robot_enabled": any(arm_enabled.values()),
                "phase": "enabled" if enabled else "disabled",
                "message": message,
                "updated_at": time.time(),
            })
        self._record_event("robot_enable" if enabled else "robot_disable", True, message, output=arm)
        return self.get_state()

    def flow_info(self, code: str) -> dict:
        path = FLOW_DIR / f"{code}.json"
        if path.parent != FLOW_DIR or not path.is_file():
            raise ValueError(f"未知穴位：{code}")
        flow = json.loads(path.read_text(encoding="utf-8"))
        frames = flow.get("keyframes", [])
        arm = str(flow.get("arm", "")).strip().lower()
        if arm not in {"left", "right"}:
            raise ValueError(f"{code} 的 arm 必须是 left 或 right")
        arm_label = "左臂" if arm == "left" else "右臂"
        missing = [frame.get("name") for frame in frames
                   if frame.get("name") in {"approach", "target", "retreat"}
                   and frame.get("joints") is None]
        speed = float(flow.get("speed", 0.8)) * self.speed_scale
        acceleration = float(flow.get("acceleration", 0.8)) * self.speed_scale
        keyframes = []
        for index, frame in enumerate(frames, 1):
            joints = frame.get("joints")
            recorded = isinstance(joints, list) and len(joints) == 7
            command = None
            if recorded:
                command = (
                    f"ros2 service call /{self.namespace}/{arm}_arm/move_joint "
                    "lbot_arm_interfaces/srv/MoveJ "
                    f"\"{{joints: {json.dumps(joints)}, speed: {speed:.4f}, "
                    f"acce: {acceleration:.4f}, block: true}}\""
                )
            keyframes.append({
                "index": index,
                "name": frame.get("name"),
                "label": frame.get("label", frame.get("name", "?")),
                "recorded": recorded,
                "joints": joints,
                "hold": frame.get("hold", 0.0),
                "recorded_at": frame.get("recorded_at"),
                "recorded_from": frame.get("recorded_from"),
                "ros2_command": command,
            })

        safe_body = json.dumps({"point": code}, ensure_ascii=False, separators=(",", ":"))
        execute_command = (
            "Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8091/api/execute' "
            f"-ContentType 'application/json' -Body '{safe_body}'"
        )
        return_command = (
            "Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8091/api/return' "
            f"-ContentType 'application/json' -Body '{safe_body}'"
        )
        return {
            "code": code,
            "name": flow["point"]["name"],
            "arm": arm,
            "arm_label": arm_label,
            "ready": not missing,
            "missing": missing,
            "updated_at": path.stat().st_mtime,
            "keyframes": keyframes,
            "ros2_commands": [item["ros2_command"] for item in keyframes if item["ros2_command"]],
            "execute_command": execute_command,
            "return_command": return_command,
        }

    def catalog(self) -> list[dict]:
        return [self.flow_info(item[2]) for item in ACUPOINTS]

    @staticmethod
    def _load_needle_flow() -> dict:
        flow = json.loads(NEEDLE_FLOW_PATH.read_text(encoding="utf-8"))
        if flow.get("schema") != "fivefinger.needle-flow/v1" or flow.get("arm") != "left":
            raise ValueError("无针点穴流程格式无效，且必须固定使用左臂")
        if len(flow.get("stages") or []) != 7:
            raise ValueError("无针点穴流程必须包含固定的七个阶段")
        return flow

    @staticmethod
    def _write_needle_flow(flow: dict) -> None:
        temporary = NEEDLE_FLOW_PATH.with_suffix(NEEDLE_FLOW_PATH.suffix + ".tmp")
        temporary.write_text(json.dumps(flow, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(NEEDLE_FLOW_PATH)

    @staticmethod
    def _needle_stage(flow: dict, stage_id: str) -> dict:
        stage = next((item for item in flow["stages"] if item.get("id") == stage_id), None)
        if stage is None:
            raise ValueError(f"未知无针点穴阶段：{stage_id}")
        return stage

    @staticmethod
    def _normalize_needle_labels(flow: dict) -> None:
        for stage in flow["stages"]:
            pose_number = 0
            for step in stage.get("steps") or []:
                if step.get("type") == "pose":
                    pose_number += 1
                    step["label"] = f"{stage['label']} · 姿态 {pose_number}"

    def needle_flow_info(self) -> dict:
        flow = self._load_needle_flow()
        missing: list[str] = []
        total_steps = 0
        recorded_poses = 0
        stages = []
        for number, stage in enumerate(flow["stages"], 1):
            steps = []
            raw_steps = stage.get("steps") or []
            if stage.get("id") != "wait_5s" and not raw_steps:
                missing.append(f"{stage['label']}没有步骤")
            for index, raw in enumerate(raw_steps, 1):
                step_type = raw.get("type")
                if step_type not in {"pose", "pinch", "release"}:
                    missing.append(f"{stage['label']}步骤{index}类型无效")
                joints = raw.get("joints")
                recorded = step_type != "pose" or (isinstance(joints, list) and len(joints) == 7)
                if step_type == "pose" and not recorded:
                    missing.append(f"{stage['label']}姿态{index}未记录")
                if step_type == "pose" and recorded:
                    recorded_poses += 1
                gesture = NEEDLE_GESTURES.get(step_type)
                steps.append({
                    "id": str(raw.get("id", "")),
                    "type": step_type,
                    "label": raw.get("label") or (gesture or {}).get("label") or f"步骤 {index}",
                    "recorded": recorded,
                    "joints": joints,
                    "recorded_at": raw.get("recorded_at"),
                    "positions": (gesture or {}).get("positions"),
                })
            total_steps += len(steps)
            stages.append({
                "number": number,
                "id": stage["id"],
                "label": stage["label"],
                "wait_seconds": float(stage.get("wait_seconds", 0.0)),
                "steps": steps,
            })
        return {
            "name": flow.get("name", "无针点穴七阶段演示"),
            "arm": "left",
            "arm_label": "左臂",
            "speed": float(flow.get("speed", 1.5)) * self.speed_scale,
            "acceleration": float(flow.get("acceleration", 1.0)) * self.speed_scale,
            "gestures": NEEDLE_GESTURES,
            "stages": stages,
            "total_steps": total_steps,
            "recorded_poses": recorded_poses,
            "ready": not missing,
            "missing": missing,
            "updated_at": NEEDLE_FLOW_PATH.stat().st_mtime,
        }

    def _capture_left_joints(self) -> list[float]:
        status, state = self._bridge_request("GET", "/state", timeout=3.0)
        if status != HTTPStatus.OK or not state.get("ok"):
            raise RuntimeError(state.get("error") or f"动作桥返回 {status}")
        age = (state.get("joint_state_age_s") or {}).get("left")
        joints = (state.get("joints") or {}).get("left")
        if age is None or float(age) > 0.5:
            raise RuntimeError(f"左臂 joint_states 已过期：{age}")
        if not isinstance(joints, list) or len(joints) < 7:
            raise RuntimeError("动作桥没有返回完整的左臂 7 关节状态")
        values = [round(float(item), 6) for item in joints[:7]]
        if not all(math.isfinite(item) for item in values):
            raise RuntimeError("左臂关节状态包含无效数值")
        return values

    def needle_add_step(self, stage_id: str, step_type: str) -> dict:
        if step_type not in {"pose", "pinch", "release"}:
            raise ValueError("步骤类型只能是 pose、pinch 或 release")
        with self.lock:
            if self.state["running"]:
                raise RuntimeError("当前仍有动作或示教任务在运行")
            self.state.update({"running": True, "phase": "needle_editing",
                               "message": "正在记录左臂姿态" if step_type == "pose" else "正在添加固定手势",
                               "updated_at": time.time()})
        try:
            joints = self._capture_left_joints() if step_type == "pose" else None
            flow = self._load_needle_flow()
            stage = self._needle_stage(flow, stage_id)
            step = {"id": f"{stage_id}-{time.time_ns()}", "type": step_type}
            if step_type == "pose":
                step.update({"joints": joints, "hold": 0.0,
                             "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                             "recorded_from": "http://127.0.0.1:8766/state"})
            stage.setdefault("steps", []).append(step)
            self._normalize_needle_labels(flow)
            self._write_needle_flow(flow)
            label = "左臂姿态" if step_type == "pose" else f"左手{NEEDLE_GESTURES[step_type]['label']}"
            message = f"已在“{stage['label']}”末尾添加{label}"
            ok = True
        except Exception as exc:
            message, ok = f"添加无针点穴步骤失败：{exc}", False
        with self.lock:
            self.state.update({"running": False, "phase": "needle_edited" if ok else "error",
                               "message": message, "updated_at": time.time()})
        self._record_event("needle_step_add", ok, message, output=stage_id)
        if not ok:
            raise RuntimeError(message)
        return self.get_state()

    def needle_record_step(self, stage_id: str, step_id: str) -> dict:
        with self.lock:
            if self.state["running"]:
                raise RuntimeError("当前仍有动作或示教任务在运行")
            self.state.update({"running": True, "phase": "needle_teaching",
                               "message": "正在覆盖记录左臂当前姿态", "updated_at": time.time()})
        try:
            joints = self._capture_left_joints()
            flow = self._load_needle_flow()
            stage = self._needle_stage(flow, stage_id)
            step = next((item for item in stage.get("steps") or [] if item.get("id") == step_id), None)
            if step is None or step.get("type") != "pose":
                raise ValueError("找不到要覆盖的姿态关键帧")
            step.update({"joints": joints, "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                         "recorded_from": "http://127.0.0.1:8766/state"})
            self._write_needle_flow(flow)
            message, ok = f"已覆盖记录“{step.get('label', '左臂姿态')}”", True
        except Exception as exc:
            message, ok = f"覆盖记录失败：{exc}", False
        with self.lock:
            self.state.update({"running": False, "phase": "needle_taught" if ok else "error",
                               "message": message, "updated_at": time.time()})
        self._record_event("needle_step_record", ok, message, output=stage_id)
        if not ok:
            raise RuntimeError(message)
        return self.get_state()

    def needle_delete_step(self, stage_id: str, step_id: str) -> dict:
        with self.lock:
            if self.state["running"]:
                raise RuntimeError("当前仍有动作或示教任务在运行")
            flow = self._load_needle_flow()
            stage = self._needle_stage(flow, stage_id)
            steps = stage.get("steps") or []
            index = next((i for i, item in enumerate(steps) if item.get("id") == step_id), None)
            if index is None:
                raise ValueError("找不到要删除的关键帧")
            removed = steps.pop(index)
            self._normalize_needle_labels(flow)
            self._write_needle_flow(flow)
            message = f"已从“{stage['label']}”删除{removed.get('label', '步骤')}"
            self.state.update({"phase": "needle_edited", "message": message, "updated_at": time.time()})
        self._record_event("needle_step_delete", True, message, output=stage_id)
        return self.get_state()

    def needle_move_step(self, stage_id: str, step_id: str, direction: str) -> dict:
        if direction not in {"up", "down"}:
            raise ValueError("direction 必须是 up 或 down")
        with self.lock:
            if self.state["running"]:
                raise RuntimeError("当前仍有动作或示教任务在运行")
            flow = self._load_needle_flow()
            stage = self._needle_stage(flow, stage_id)
            steps = stage.get("steps") or []
            index = next((i for i, item in enumerate(steps) if item.get("id") == step_id), None)
            if index is None:
                raise ValueError("找不到要移动的关键帧")
            target = index - 1 if direction == "up" else index + 1
            if not 0 <= target < len(steps):
                raise RuntimeError("该步骤已经位于阶段边界")
            steps[index], steps[target] = steps[target], steps[index]
            self._normalize_needle_labels(flow)
            self._write_needle_flow(flow)
            message = f"已调整“{stage['label']}”内部步骤顺序"
            self.state.update({"phase": "needle_edited", "message": message, "updated_at": time.time()})
        return self.get_state()

    def request_needle(self) -> dict:
        info = self.needle_flow_info()
        bridge = self.preflight()
        with self.shared.lock:
            tracking = bool(self.shared.status.get("tracking"))
        with self.lock:
            if not self.enabled:
                raise RuntimeError("真机执行未启用；请用 --enable-robot 启动本地服务")
            if not self.state["arm_enabled"].get("left", False):
                raise RuntimeError("左臂尚未使能；请先点击“左臂使能”")
            if not bridge["ok"]:
                raise RuntimeError(f"机器人连接预检未通过：{bridge['message']}")
            if self.state["running"]:
                raise RuntimeError("上一组动作仍在执行")
            if not tracking:
                raise RuntimeError("AprilTag 定位尚未稳定，禁止执行")
            if not info["ready"]:
                raise RuntimeError("无针点穴七阶段尚未完成示教：" + "；".join(info["missing"][:3]))
            message = "无针点穴七阶段演示已进入队列"
            self.state.update({"running": True, "point": "NEEDLE", "phase": "needle_queued",
                               "message": message, "updated_at": time.time()})
        threading.Thread(target=self._run_needle, daemon=True).start()
        self._record_event("needle_execute", True, message, point="NEEDLE")
        return self.get_state()

    def _run_needle(self) -> None:
        with self.lock:
            self.state.update({"phase": "needle_running", "message": "正在执行无针点穴七阶段演示",
                               "updated_at": time.time()})
        command = [
            sys.executable, str(NEEDLE_RUNNER), "--execute",
            "--bridge-url", self.bridge_url,
            "--speed-scale", str(self.speed_scale),
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=600, check=False)
            output = (result.stdout if result.returncode == 0 else result.stderr).strip()
            if result.returncode != 0:
                raise RuntimeError(output or f"无针点穴流程退出码 {result.returncode}")
            phase, message, ok = "needle_done", "无针点穴七阶段演示完成", True
        except Exception as exc:  # noqa: BLE001
            output = str(exc)
            phase, message, ok = "error", f"无针点穴演示失败：{exc}", False
        with self.lock:
            self.state.update({"running": False, "phase": phase, "message": message, "updated_at": time.time()})
        self._record_event("needle_done", ok, message, point="NEEDLE", output=output)

    def get_state(self) -> dict:
        with self.lock:
            state = dict(self.state)
            state["arm_enabled"] = dict(self.state["arm_enabled"])
        state["events"] = self.history()
        return state

    def request(self, code: str, reverse: bool = False, round_trip: bool = True) -> dict:
        info = self.flow_info(code)
        bridge = self.preflight()
        with self.shared.lock:
            tracking = bool(self.shared.status.get("tracking"))
        with self.lock:
            if not self.enabled:
                raise RuntimeError("真机执行未启用；请用 --enable-robot 启动本地服务")
            if not self.state["arm_enabled"].get(info["arm"], False):
                raise RuntimeError(f"{info['arm_label']}尚未使能；请先点击“{info['arm_label']}使能”")
            if not bridge["ok"]:
                raise RuntimeError(f"机器人连接预检未通过：{bridge['message']}")
            if self.state["running"]:
                raise RuntimeError("上一组动作仍在执行")
            if not tracking:
                raise RuntimeError("AprilTag 定位尚未稳定，禁止执行")
            if not info["ready"]:
                raise RuntimeError("该穴位尚未完成三帧示教")
            if reverse:
                round_trip = False
            phase = "queued_return" if reverse else "queued_cycle" if round_trip else "queued"
            message = (
                f"{info['name']} 三帧倒序返回已进入队列" if reverse
                else f"{info['name']} 正向三帧、等待 5 秒、倒序返程并归零已进入队列" if round_trip
                else f"{info['name']} 三帧执行已进入队列"
            )
            self.state.update({
                "running": True,
                "point": code,
                "phase": phase,
                "message": message,
                "updated_at": time.time(),
            })
        threading.Thread(
            target=self._run,
            args=(code, info["name"], reverse, round_trip),
            daemon=True,
        ).start()
        self._record_event("return" if reverse else "cycle" if round_trip else "execute", True, message, point=code)
        return self.get_state()

    def teach(self, code: str, keyframe: str) -> dict:
        info = self.flow_info(code)
        if keyframe not in {"approach", "target", "retreat"}:
            raise ValueError("示教帧只能是 approach、target 或 retreat")
        labels = {"approach": "姿态 1", "target": "姿态 2", "retreat": "姿态 3"}
        with self.lock:
            if self.state["running"]:
                raise RuntimeError("当前仍有动作或示教任务在运行")
            self.state.update({
                "running": True,
                "point": code,
                "phase": "teaching",
                "message": f"正在记录 {info['name']} · {labels[keyframe]}（{info['arm_label']}）",
                "updated_at": time.time(),
            })
        threading.Thread(target=self._teach, args=(code, info["name"], keyframe, labels[keyframe]), daemon=True).start()
        self._record_event("teach_started", True, f"开始记录 {info['name']} · {labels[keyframe]}（{info['arm_label']}）",
                           point=code, keyframe=keyframe)
        return self.get_state()

    def delete_keyframe(self, code: str, keyframe: str) -> dict:
        info = self.flow_info(code)
        if keyframe not in {"approach", "target", "retreat"}:
            raise ValueError("只能删除 approach、target 或 retreat 示教帧")
        labels = {"approach": "姿态 1", "target": "姿态 2", "retreat": "姿态 3"}
        path = FLOW_DIR / f"{code}.json"
        with self.lock:
            if self.state["running"]:
                raise RuntimeError("当前仍有动作或示教任务在运行，不能删除关键帧")
            flow = json.loads(path.read_text(encoding="utf-8"))
            target = next((frame for frame in flow.get("keyframes", []) if frame.get("name") == keyframe), None)
            if target is None:
                raise ValueError(f"流程缺少关键帧：{keyframe}")
            if target.get("joints") is None:
                raise RuntimeError(f"{info['name']} · {labels[keyframe]}尚未记录，无需删除")

            backup_dir = PROJECT_DIR / "data" / "keyframe_backups" / "deleted"
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            backup_path = backup_dir / f"{stamp}_{code}_{keyframe}.json"
            suffix = 1
            while backup_path.exists():
                backup_path = backup_dir / f"{stamp}_{code}_{keyframe}_{suffix}.json"
                suffix += 1
            backup_path.write_text(json.dumps(flow, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            target["joints"] = None
            target.pop("recorded_at", None)
            target.pop("recorded_from", None)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(json.dumps(flow, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(path)
            message = f"已删除 {info['name']} · {labels[keyframe]}；原数据已备份"
            self.state.update({
                "point": code,
                "phase": "deleted",
                "message": message,
                "updated_at": time.time(),
            })
        self._record_event("teach_deleted", True, message, point=code, keyframe=keyframe,
                           output=str(backup_path))
        return self.get_state()

    def _teach(self, code: str, name: str, keyframe: str, label: str) -> None:
        command = [
            sys.executable,
            str(FLOW_RUNNER),
            "--point", code,
            "--teach", keyframe,
            "--namespace", self.namespace,
            "--bridge-url", self.bridge_url,
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
            output = (result.stdout if result.returncode == 0 else result.stderr).strip()
            if result.returncode != 0:
                raise RuntimeError(output or f"示教程序退出码 {result.returncode}")
            phase, message = "taught", f"已记录 {name} · {label}"
            ok = True
        except Exception as exc:  # noqa: BLE001
            output = str(exc)
            phase, message = "error", f"{name} · {label}记录失败：{exc}"
            ok = False
        with self.lock:
            self.state.update({"running": False, "phase": phase, "message": message, "updated_at": time.time()})
        self._record_event("teach", ok, message, point=code, keyframe=keyframe, output=output)

    def _run(self, code: str, name: str, reverse: bool = False, round_trip: bool = False) -> None:
        with self.lock:
            phase = "returning" if reverse else "cycling" if round_trip else "running"
            message = (
                f"正在倒序返回：{name}" if reverse
                else f"正在演示 {name}：正向三帧 → 等待 5 秒 → 倒序三帧 → 七轴同步归零 → 放手" if round_trip
                else f"正在执行三帧：{name}"
            )
            self.state.update({
                "phase": phase,
                "message": message,
                "updated_at": time.time(),
            })
        command = [
            sys.executable,
            str(FLOW_RUNNER),
            "--point", code,
            "--execute",
            "--namespace", self.namespace,
            "--speed-scale", str(self.speed_scale),
            "--bridge-url", self.bridge_url,
        ]
        if reverse:
            command.append("--reverse")
        elif round_trip:
            command.append("--round-trip")
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=240, check=False)
            output = (result.stdout if result.returncode == 0 else result.stderr).strip()
            if result.returncode != 0:
                raise RuntimeError(output or f"流程退出码 {result.returncode}")
            phase = "returned" if reverse else "cycle_done" if round_trip else "done"
            message = (
                f"{name} 三帧倒序返回完成" if reverse
                else f"{name} 点穴返程、关节归零与手势恢复均已完成" if round_trip
                else f"{name} 三帧执行完成"
            )
            ok = True
        except Exception as exc:  # noqa: BLE001
            output = str(exc)
            action = "倒序返回" if reverse else "往返演示" if round_trip else "执行"
            phase, message = "error", f"{name}{action}失败：{exc}"
            ok = False
        with self.lock:
            self.state.update({"running": False, "phase": phase, "message": message, "updated_at": time.time()})
        kind = "return_done" if reverse else "cycle_done" if round_trip else "execute_done"
        self._record_event(kind, ok, message, point=code, output=output)


class AcupointNode(Node):
    def __init__(self, shared: SharedView, font_path: Path) -> None:
        super().__init__("fivefinger_acupoint_demo")
        self.shared = shared
        self.dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_100)
        self.parameters = cv2.aruco.DetectorParameters_create()
        self.parameters.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
        self.tracked: dict[int, TrackedMarker] = {}
        self.last_transform: Optional[np.ndarray] = None
        self.last_transform_at = 0.0
        self.last_frame_time = 0.0
        self.fps_ema = 0.0
        self.font = ImageFont.truetype(str(font_path), 25) if font_path.is_file() else None
        self.small_font = ImageFont.truetype(str(font_path), 19) if font_path.is_file() else None

        qos = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
            history=HistoryPolicy.KEEP_LAST,
            depth=3,
        )
        self.create_subscription(Image, "/camera/color/image_raw", self.on_image, qos)

    @staticmethod
    def message_to_bgr(message: Image) -> np.ndarray:
        if message.encoding.lower() not in ("rgb8", "bgr8"):
            raise ValueError(f"不支持的彩色图像编码：{message.encoding}")
        raw = np.frombuffer(message.data, dtype=np.uint8)
        rows = raw.reshape(message.height, message.step)
        image = rows[:, : message.width * 3].reshape(message.height, message.width, 3)
        if message.encoding.lower() == "rgb8":
            return cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        return image.copy()

    def detect_markers(self, gray: np.ndarray) -> tuple[list[np.ndarray], Optional[np.ndarray]]:
        corners, ids, _ = cv2.aruco.detectMarkers(gray, self.dictionary, parameters=self.parameters)
        found = set() if ids is None else set(ids.flatten().tolist())

        # Retry temporarily missing tags in a small upscaled area around their
        # last position. This improves the two 18 px tags without processing a
        # full 4K upscaled frame.
        recovered_corners: list[np.ndarray] = []
        recovered_ids: list[int] = []
        height, width = gray.shape
        now = time.monotonic()
        for marker_id, marker in list(self.tracked.items()):
            if marker_id in found or now - marker.last_seen > 2.5:
                continue
            cx, cy = marker.center
            radius = 42
            x0, y0 = max(0, int(cx) - radius), max(0, int(cy) - radius)
            x1, y1 = min(width, int(cx) + radius), min(height, int(cy) + radius)
            if x1 - x0 < 30 or y1 - y0 < 30:
                continue
            scale = 3.0
            roi = cv2.resize(gray[y0:y1, x0:x1], None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            local_corners, local_ids, _ = cv2.aruco.detectMarkers(roi, self.dictionary, parameters=self.parameters)
            if local_ids is None:
                continue
            for local_corner, local_id in zip(local_corners, local_ids.flatten().tolist()):
                if int(local_id) != marker_id:
                    continue
                mapped = local_corner.copy()
                mapped[..., 0] = mapped[..., 0] / scale + x0
                mapped[..., 1] = mapped[..., 1] / scale + y0
                recovered_corners.append(mapped)
                recovered_ids.append(marker_id)
                found.add(marker_id)
                break

        if recovered_ids:
            corners = list(corners) + recovered_corners
            extra = np.asarray(recovered_ids, dtype=np.int32).reshape(-1, 1)
            ids = extra if ids is None else np.vstack((ids, extra))
        return list(corners), ids

    def update_tracking(self, corners: list[np.ndarray], ids: Optional[np.ndarray], now: float) -> list[int]:
        visible: list[int] = []
        if ids is not None:
            for marker_corners, marker_id_raw in zip(corners, ids.flatten().tolist()):
                marker_id = int(marker_id_raw)
                if marker_id not in MARKER_LAYOUT:
                    continue
                center = marker_corners.reshape(4, 2).mean(axis=0).astype(np.float64)
                old = self.tracked.get(marker_id)
                if old is None or now - old.last_seen > 2.5:
                    filtered = center
                elif float(np.linalg.norm(center - old.center)) < 100.0:
                    filtered = old.center * 0.72 + center * 0.28
                else:
                    continue
                self.tracked[marker_id] = TrackedMarker(filtered, now)
                visible.append(marker_id)

        for marker_id in list(self.tracked):
            if now - self.tracked[marker_id].last_seen > 2.5:
                del self.tracked[marker_id]
        return sorted(visible)

    def estimate_transform(self, now: float) -> tuple[Optional[np.ndarray], list[int]]:
        ids = sorted(self.tracked)
        panel_ids = (11, 1, 3, 19)
        if all(marker_id in self.tracked for marker_id in panel_ids):
            top_left, top_right, bottom_left, bottom_right = (
                self.tracked[marker_id].center for marker_id in panel_ids
            )
            top_center = (top_left + top_right) * 0.5
            bottom_center = (bottom_left + bottom_right) * 0.5
            top_vector = top_right - top_left
            bottom_vector = bottom_right - bottom_left
            direction = top_vector + bottom_vector
            direction_norm = float(np.linalg.norm(direction))
            if direction_norm > 1e-6:
                direction = direction / direction_norm
                top_width = float(np.linalg.norm(top_vector))
                bottom_width = float(np.linalg.norm(bottom_vector))
                # The mannequin panel is a trapezoid in the camera view: its
                # upper edge is wider than its lower edge. Enforce that shape
                # so one noisy lower tag cannot pull a single corner inward.
                top_width = max(top_width, bottom_width * 1.08)
                bottom_width = min(bottom_width, top_width * 0.92)
                fitted = np.float32([
                    top_center - direction * top_width * 0.5,
                    top_center + direction * top_width * 0.5,
                    bottom_center - direction * bottom_width * 0.5,
                    bottom_center + direction * bottom_width * 0.5,
                ])
                source = np.float32([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0], [1.0, 1.0]])
                transform = cv2.getPerspectiveTransform(source, fitted)
            else:
                transform = None
            if transform is not None and np.isfinite(transform).all():
                self.last_transform = transform
                self.last_transform_at = now
                return transform, ids
        if len(ids) == 3:
            source = np.float32([MARKER_LAYOUT[i] for i in ids])
            target = np.float32([self.tracked[i].center for i in ids])
            affine = cv2.getAffineTransform(source, target)
            transform = np.vstack((affine, [0.0, 0.0, 1.0]))
            if np.isfinite(transform).all():
                self.last_transform = transform
                self.last_transform_at = now
                return transform, ids
        if self.last_transform is not None and now - self.last_transform_at <= 1.0:
            return self.last_transform, ids
        return None, ids

    @staticmethod
    def project(transform: np.ndarray, point: tuple[float, float]) -> tuple[int, int]:
        src = np.asarray([[[point[0], point[1]]]], dtype=np.float32)
        dst = cv2.perspectiveTransform(src, transform)[0, 0]
        return int(round(float(dst[0]))), int(round(float(dst[1])))

    @staticmethod
    def marker_outline(marker_corners: np.ndarray) -> np.ndarray:
        """Return a stable rectangular outline for display without altering detection geometry."""
        points = marker_corners.reshape(4, 2).astype(np.float32)
        rectangle = cv2.minAreaRect(points)
        box = cv2.boxPoints(rectangle)
        center = box.mean(axis=0)
        angles = np.arctan2(box[:, 1] - center[1], box[:, 0] - center[0])
        ordered = box[np.argsort(angles)].reshape(1, 4, 2)
        return ordered.astype(np.float32)

    def draw_overlay(
        self,
        bgr: np.ndarray,
        corners: list[np.ndarray],
        ids: Optional[np.ndarray],
        transform: Optional[np.ndarray],
        tracked_ids: list[int],
    ) -> np.ndarray:
        annotated = bgr.copy()
        if ids is not None:
            expected_indices = [index for index, marker_id in enumerate(ids.flatten().tolist()) if int(marker_id) in MARKER_LAYOUT]
            if expected_indices:
                cv2.aruco.drawDetectedMarkers(
                    annotated,
                    [self.marker_outline(corners[index]) for index in expected_indices],
                    ids[expected_indices],
                    borderColor=(40, 230, 80),
                )

        if transform is None:
            cv2.rectangle(annotated, (22, 22), (610, 82), (0, 80, 210), -1)
            cv2.putText(annotated, "Waiting for at least 3 anchor tags", (38, 61), cv2.FONT_HERSHEY_SIMPLEX, 0.85, (255, 255, 255), 2, cv2.LINE_AA)
            return annotated

        boundary = np.float32([[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]])
        polygon = cv2.perspectiveTransform(boundary, transform).round().astype(np.int32)
        tint = annotated.copy()
        cv2.fillPoly(tint, polygon, (38, 76, 98))
        cv2.addWeighted(tint, 0.13, annotated, 0.87, 0, annotated)
        cv2.polylines(annotated, polygon, True, (160, 220, 255), 2, cv2.LINE_AA)

        labels = []
        for number, chinese, code, canonical, color, label_offset in ACUPOINTS:
            x, y = self.project(transform, canonical)
            cv2.circle(annotated, (x, y), 17, (15, 20, 25), -1, cv2.LINE_AA)
            cv2.circle(annotated, (x, y), 13, color, 3, cv2.LINE_AA)
            cv2.line(annotated, (x - 22, y), (x + 22, y), color, 2, cv2.LINE_AA)
            cv2.line(annotated, (x, y - 22), (x, y + 22), color, 2, cv2.LINE_AA)
            cv2.putText(annotated, str(number), (x - 7, y + 7), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (255, 255, 255), 2, cv2.LINE_AA)
            labels.append((x + label_offset[0], y + label_offset[1], f"{number}  {chinese}  {code}", color))

        if self.font is not None:
            rgb = cv2.cvtColor(annotated, cv2.COLOR_BGR2RGB)
            pil = PilImage.fromarray(rgb)
            draw = ImageDraw.Draw(pil)
            for x, y, label, color in labels:
                rgb_color = (color[2], color[1], color[0])
                text_box = draw.textbbox((x, y), label, font=self.small_font)
                draw.rounded_rectangle((x - 5, y - 3, text_box[2] + 7, y + 31), radius=7, fill=(8, 18, 24))
                draw.text((x, y), label, font=self.small_font, fill=rgb_color)
            draw.rounded_rectangle((22, 20, 420, 61), radius=8, fill=(8, 18, 24))
            draw.text((34, 27), f"定位正常  标记: {','.join(map(str, tracked_ids))}", font=self.small_font, fill=(145, 245, 180))
            annotated = cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR)
        return annotated

    def on_image(self, message: Image) -> None:
        try:
            bgr = self.message_to_bgr(message)
            now = time.monotonic()
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            corners, ids = self.detect_markers(gray)
            visible_ids = self.update_tracking(corners, ids, now)
            transform, tracked_ids = self.estimate_transform(now)
            annotated = self.draw_overlay(bgr, corners, ids, transform, tracked_ids)
            inferred = []
            if transform is not None:
                for number, chinese, code, canonical, _color, _offset in ACUPOINTS:
                    x, y = self.project(transform, canonical)
                    inferred.append({
                        "number": number,
                        "name": chinese,
                        "code": code,
                        "x_px": x,
                        "y_px": y,
                        "u": canonical[0],
                        "v": canonical[1],
                    })

            if self.last_frame_time:
                instant_fps = 1.0 / max(now - self.last_frame_time, 1e-6)
                self.fps_ema = instant_fps if not self.fps_ema else self.fps_ema * 0.85 + instant_fps * 0.15
            self.last_frame_time = now

            ok, encoded = cv2.imencode(".jpg", annotated, [int(cv2.IMWRITE_JPEG_QUALITY), 84])
            if not ok:
                return
            self.shared.update(annotated, encoded.tobytes(), {
                "frame_ready": True,
                "tracking": transform is not None,
                "fps": round(self.fps_ema, 1),
                "visible_ids": visible_ids,
                "tracked_ids": tracked_ids,
                "acupoints": inferred,
                "received_at": time.time(),
            })
        except Exception as exc:  # noqa: BLE001
            LOG.warning("处理相机帧失败：%s", exc)


def make_handler(shared: SharedView, actions: ActionController):
    class Handler(BaseHTTPRequestHandler):
        server_version = "FiveFingerAcupointDemo/1.0"

        def log_message(self, format_: str, *args) -> None:
            LOG.debug(format_, *args)

        def send_bytes(self, payload: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.end_headers()
            self.wfile.write(payload)

        def read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 8192:
                raise ValueError("请求内容为空或过大")
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/":
                page = CONSOLE_PATH.read_text(encoding="utf-8") if CONSOLE_PATH.is_file() else PAGE
                self.send_bytes(page.encode("utf-8"), "text/html; charset=utf-8")
                return
            if self.path.startswith("/assets/"):
                asset_path = (WEB_ROOT / self.path.lstrip("/")).resolve()
                try:
                    asset_path.relative_to(WEB_ROOT.resolve())
                except ValueError:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                if not asset_path.is_file():
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                content_type = mimetypes.guess_type(asset_path.name)[0] or "application/octet-stream"
                self.send_bytes(asset_path.read_bytes(), content_type)
                return
            if self.path == "/status.json":
                status = shared.get_status()
                status["action"] = actions.get_state()
                status["flows"] = actions.catalog()
                status["needle_flow"] = actions.needle_flow_info()
                payload = json.dumps(status, ensure_ascii=False).encode("utf-8")
                self.send_bytes(payload, "application/json; charset=utf-8")
                return
            if self.path == "/api/acupoints":
                payload = json.dumps({"items": actions.catalog(), "action": actions.get_state()}, ensure_ascii=False).encode("utf-8")
                self.send_bytes(payload, "application/json; charset=utf-8")
                return
            if self.path == "/api/preflight":
                payload = actions.preflight()
                status = HTTPStatus.OK if payload.get("ok") else HTTPStatus.SERVICE_UNAVAILABLE
                self.send_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)
                return
            if self.path == "/stream.mjpg":
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.end_headers()
                last_frame = -1
                try:
                    while True:
                        with shared.condition:
                            shared.condition.wait_for(lambda: shared.frame_no != last_frame, timeout=2.0)
                            jpeg, frame_no = shared.jpeg, shared.frame_no
                        if jpeg is None or frame_no == last_frame:
                            continue
                        last_frame = frame_no
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpeg)).encode("ascii") + b"\r\n\r\n")
                        self.wfile.write(jpeg + b"\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/api/agent":
                try:
                    request = self.read_json()
                    payload = interpret_agent_query(str(request.get("text", "")))
                    status = HTTPStatus.OK
                except Exception as exc:  # noqa: BLE001
                    payload = {"ok": False, "error": str(exc)}
                    status = HTTPStatus.BAD_REQUEST
                self.send_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)
                return
            if self.path == "/api/teach":
                try:
                    request = self.read_json()
                    payload = {"ok": True, "action": actions.teach(
                        str(request.get("point", "")),
                        str(request.get("keyframe", "")),
                    )}
                    status = HTTPStatus.ACCEPTED
                except Exception as exc:  # noqa: BLE001
                    payload = {"ok": False, "error": str(exc), "action": actions.get_state()}
                    status = HTTPStatus.CONFLICT
                self.send_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)
                return
            if self.path == "/api/delete-keyframe":
                try:
                    request = self.read_json()
                    payload = {"ok": True, "action": actions.delete_keyframe(
                        str(request.get("point", "")),
                        str(request.get("keyframe", "")),
                    )}
                    status = HTTPStatus.OK
                except Exception as exc:  # noqa: BLE001
                    payload = {"ok": False, "error": str(exc), "action": actions.get_state()}
                    status = HTTPStatus.CONFLICT
                self.send_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)
                return
            if self.path == "/api/execute":
                try:
                    request = self.read_json()
                    payload = {"ok": True, "action": actions.request(str(request.get("point", "")))}
                    status = HTTPStatus.ACCEPTED
                except Exception as exc:  # noqa: BLE001
                    payload = {"ok": False, "error": str(exc), "action": actions.get_state()}
                    status = HTTPStatus.CONFLICT
                self.send_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)
                return
            if self.path == "/api/needle/add-step":
                try:
                    request = self.read_json()
                    payload = {"ok": True, "action": actions.needle_add_step(
                        str(request.get("stage", "")), str(request.get("type", ""))
                    )}
                    status = HTTPStatus.OK
                except Exception as exc:  # noqa: BLE001
                    payload = {"ok": False, "error": str(exc), "action": actions.get_state()}
                    status = HTTPStatus.CONFLICT
                self.send_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)
                return
            if self.path == "/api/needle/record-step":
                try:
                    request = self.read_json()
                    payload = {"ok": True, "action": actions.needle_record_step(
                        str(request.get("stage", "")), str(request.get("step", ""))
                    )}
                    status = HTTPStatus.OK
                except Exception as exc:  # noqa: BLE001
                    payload = {"ok": False, "error": str(exc), "action": actions.get_state()}
                    status = HTTPStatus.CONFLICT
                self.send_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)
                return
            if self.path == "/api/needle/delete-step":
                try:
                    request = self.read_json()
                    payload = {"ok": True, "action": actions.needle_delete_step(
                        str(request.get("stage", "")), str(request.get("step", ""))
                    )}
                    status = HTTPStatus.OK
                except Exception as exc:  # noqa: BLE001
                    payload = {"ok": False, "error": str(exc), "action": actions.get_state()}
                    status = HTTPStatus.CONFLICT
                self.send_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)
                return
            if self.path == "/api/needle/move-step":
                try:
                    request = self.read_json()
                    payload = {"ok": True, "action": actions.needle_move_step(
                        str(request.get("stage", "")), str(request.get("step", "")),
                        str(request.get("direction", ""))
                    )}
                    status = HTTPStatus.OK
                except Exception as exc:  # noqa: BLE001
                    payload = {"ok": False, "error": str(exc), "action": actions.get_state()}
                    status = HTTPStatus.CONFLICT
                self.send_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)
                return
            if self.path == "/api/needle/execute":
                try:
                    self.read_json()
                    payload = {"ok": True, "action": actions.request_needle()}
                    status = HTTPStatus.ACCEPTED
                except Exception as exc:  # noqa: BLE001
                    payload = {"ok": False, "error": str(exc), "action": actions.get_state()}
                    status = HTTPStatus.CONFLICT
                self.send_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)
                return
            if self.path == "/api/return":
                try:
                    request = self.read_json()
                    payload = {"ok": True, "action": actions.request(
                        str(request.get("point", "")), reverse=True, round_trip=False
                    )}
                    status = HTTPStatus.ACCEPTED
                except Exception as exc:  # noqa: BLE001
                    payload = {"ok": False, "error": str(exc), "action": actions.get_state()}
                    status = HTTPStatus.CONFLICT
                self.send_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)
                return
            if self.path == "/api/emergency-stop":
                try:
                    payload = {"ok": True, "action": actions.emergency_stop()}
                    status = HTTPStatus.OK
                except Exception as exc:  # noqa: BLE001
                    payload = {"ok": False, "error": str(exc), "action": actions.get_state()}
                    status = HTTPStatus.SERVICE_UNAVAILABLE
                self.send_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)
                return
            if self.path in {"/api/robot-enable", "/api/robot-disable"}:
                try:
                    request = self.read_json()
                    payload = {"ok": True, "action": actions.set_robot_enabled(
                        str(request.get("arm", "")), self.path.endswith("enable")
                    )}
                    status = HTTPStatus.OK
                except Exception as exc:  # noqa: BLE001
                    payload = {"ok": False, "error": str(exc), "action": actions.get_state()}
                    status = HTTPStatus.CONFLICT
                self.send_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)
                return
            if self.path != "/snapshot":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                path = shared.save_snapshot()
                payload = {"ok": True, "file": str(path)}
                status = HTTPStatus.OK
            except Exception as exc:  # noqa: BLE001
                payload = {"ok": False, "error": str(exc)}
                status = HTTPStatus.CONFLICT
            self.send_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    return Handler


def parse_args() -> argparse.Namespace:
    project_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="假人背部 ArUco 穴位实时叠加演示")
    parser.add_argument("--host", default="127.0.0.1", help="网页监听地址，默认仅本机")
    parser.add_argument("--port", type=int, default=8091, help="网页端口")
    parser.add_argument("--font", type=Path, default=Path("/mnt/c/Windows/Fonts/msyh.ttc"), help="中文字体")
    parser.add_argument("--snapshot-dir", type=Path, default=project_dir / "data" / "acupoint_demo_captures")
    parser.add_argument("--test-seconds", type=float, default=0.0, help="无网页测试指定秒数后退出")
    parser.add_argument("--test-image", type=Path, help="使用已有图片进行离线画面验收")
    parser.add_argument("--test-output", type=Path, default=project_dir / "data" / "acupoint_demo_test.jpg")
    parser.add_argument("--enable-robot", action="store_true", help="显式允许网页触发已完成示教的真机流程")
    parser.add_argument("--robot-namespace", default="robot1")
    parser.add_argument("--robot-bridge-url", default="http://127.0.0.1:8766",
                        help="Task1 HTTP 动作桥地址，用于只读预检、MoveJ 与软件急停")
    parser.add_argument("--speed-scale", type=float, default=1.0, help="真机速度缩放，限制为 (0, 1]")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not math.isfinite(args.speed_scale) or not 0 < args.speed_scale <= 1.0:
        raise SystemExit("--speed-scale 必须在 (0, 1]")
    shared = SharedView(args.snapshot_dir)
    actions = ActionController(shared, args.enable_robot, args.robot_namespace, args.speed_scale,
                               args.robot_bridge_url)
    # Populate bridge readiness immediately so the enable/disable controls do
    # not depend on a separate manual "check connection" click after startup.
    actions.preflight()
    rclpy.init()
    node = AcupointNode(shared, args.font)
    server = None
    server_thread = None
    if args.test_image is not None:
        bgr = cv2.imread(str(args.test_image))
        if bgr is None:
            raise SystemExit(f"无法读取测试图片：{args.test_image}")
        now = time.monotonic()
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        corners, ids = node.detect_markers(gray)
        visible_ids = node.update_tracking(corners, ids, now)
        transform, tracked_ids = node.estimate_transform(now)
        annotated = node.draw_overlay(bgr, corners, ids, transform, tracked_ids)
        args.test_output.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(args.test_output), annotated)
        print(json.dumps({
            "output": str(args.test_output),
            "tracking": transform is not None,
            "visible_ids": visible_ids,
            "tracked_ids": tracked_ids,
        }, ensure_ascii=False, indent=2), flush=True)
        node.destroy_node()
        rclpy.shutdown()
        return
    if args.test_seconds <= 0:
        server = ThreadingHTTPServer((args.host, args.port), make_handler(shared, actions))
        server_thread = threading.Thread(target=server.serve_forever, name="acupoint-web", daemon=True)
        server_thread.start()
        LOG.info("穴位演示网页：http://%s:%d", args.host, args.port)
        if args.enable_robot:
            LOG.warning("真机动作已启用；只有定位稳定且流程示教完整时才允许执行")
        else:
            LOG.info("真机动作未启用；网页仅显示画面与示教状态")
    try:
        if args.test_seconds > 0:
            deadline = time.monotonic() + args.test_seconds
            while rclpy.ok() and time.monotonic() < deadline:
                rclpy.spin_once(node, timeout_sec=0.1)
            with shared.lock:
                test_image = None if shared.bgr is None else shared.bgr.copy()
                status = dict(shared.status)
            if test_image is None:
                raise RuntimeError("测试期间没有收到相机画面")
            args.test_output.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(args.test_output), test_image)
            print(json.dumps({"output": str(args.test_output), "status": status}, ensure_ascii=False, indent=2), flush=True)
        else:
            rclpy.spin(node)
    except KeyboardInterrupt:
        LOG.info("收到退出请求")
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
