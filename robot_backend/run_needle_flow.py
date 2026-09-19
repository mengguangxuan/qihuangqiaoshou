#!/usr/bin/env python3
"""Validate and execute the five-stage left-arm needle demonstration."""

from __future__ import annotations

import argparse
import json
import math
import time
import urllib.error
import urllib.request
from pathlib import Path


FLOW_PATH = Path(__file__).resolve().parent / "needle_flow.json"
GESTURES = {
    "pinch": [0, 96, 0, 255, 255, 255],
    "release": [255, 71, 255, 255, 255, 255],
}
GESTURE_PAUSE_SECONDS = 1.5
STAGE_IDS = (
    "take_large", "insert_large", "take_small", "insert_small",
    "wait_5s",
)


def load_flow(path: Path = FLOW_PATH) -> dict:
    flow = json.loads(path.read_text(encoding="utf-8"))
    if flow.get("schema") != "fivefinger.needle-flow/v1":
        raise ValueError("不支持的无针点穴流程格式")
    if flow.get("arm") != "left":
        raise ValueError("无针点穴流程必须固定使用左臂")
    stages = flow.get("stages")
    if not isinstance(stages, list) or tuple(stage.get("id") for stage in stages) != STAGE_IDS:
        raise ValueError("无针点穴流程必须保持固定的五阶段顺序")
    return flow


def validate_joints(value: object, label: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 7:
        raise ValueError(f"{label} 尚未记录完整的 7 关节位置")
    joints = [float(item) for item in value]
    if not all(math.isfinite(item) for item in joints):
        raise ValueError(f"{label} 含有无效关节值")
    return joints


def readiness(flow: dict) -> tuple[bool, list[str]]:
    missing: list[str] = []
    for stage in flow["stages"]:
        steps = stage.get("steps") or []
        if stage["id"] != "wait_5s" and not steps:
            missing.append(f"{stage['label']}没有步骤")
        for index, step in enumerate(steps, 1):
            if step.get("type") == "pose" and step.get("joints") is None:
                missing.append(f"{stage['label']}姿态{index}未记录")
            elif step.get("type") == "pose" and step.get("feedback_version") != 1:
                missing.append(f"{stage['label']}姿态{index}为旧反馈记录，需覆盖重录")
            elif step.get("type") not in {"pose", "pinch", "release"}:
                missing.append(f"{stage['label']}步骤{index}类型无效")
    return not missing, missing


def bridge_request(base_url: str, path: str, body: dict | None = None,
                   timeout: float = 45.0) -> dict:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=payload,
        method="GET" if body is None else "POST",
        headers={"Content-Type": "application/json"} if payload is not None else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            value = json.loads(exc.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            value = {"error": str(exc)}
        raise RuntimeError(value.get("error") or f"动作桥返回 {exc.code}") from exc
    if not value.get("ok"):
        raise RuntimeError(value.get("error") or f"动作桥请求失败：{path}")
    return value


def execute(flow: dict, bridge_url: str, speed_scale: float = 1.0,
            timeout: float = 45.0) -> None:
    ready, missing = readiness(flow)
    if not ready:
        raise RuntimeError("流程尚未完成示教：" + "；".join(missing))
    bridge_request(bridge_url, "/health", timeout=timeout)
    speed = float(flow.get("speed", 1.5)) * speed_scale
    acceleration = float(flow.get("acceleration", 1.0)) * speed_scale
    total = sum(len(stage.get("steps") or []) for stage in flow["stages"])
    current = 0
    for stage_index, stage in enumerate(flow["stages"], 1):
        print(f"[阶段 {stage_index}/{len(flow['stages'])}] {stage['label']}", flush=True)
        for step in stage.get("steps") or []:
            current += 1
            step_type = step.get("type")
            if step_type == "pose":
                joints = validate_joints(step.get("joints"), step.get("label", f"步骤{current}"))
                print(f"  [{current}/{total}] {step.get('label', '左臂姿态')}", flush=True)
                bridge_request(bridge_url, "/move_joint", {
                    "arm": "left",
                    "joints": joints,
                    "speed": speed,
                    "acceleration": acceleration,
                }, timeout=timeout)
                time.sleep(max(0.0, float(step.get("hold", 0.0))))
            else:
                positions = GESTURES[step_type]
                label = "捏" if step_type == "pinch" else "放"
                print(f"  [等待] {label}之前停留 {GESTURE_PAUSE_SECONDS:g} 秒", flush=True)
                time.sleep(GESTURE_PAUSE_SECONDS)
                print(f"  [{current}/{total}] 左手{label} {positions}", flush=True)
                bridge_request(bridge_url, "/hand", {
                    "hand": "left", "positions": positions, "speed_scale": 1.0,
                }, timeout=timeout)
                print(f"  [等待] {label}完成后停留 {GESTURE_PAUSE_SECONDS:g} 秒", flush=True)
                time.sleep(GESTURE_PAUSE_SECONDS)
        wait_seconds = max(0.0, float(stage.get("wait_seconds", 0.0)))
        if wait_seconds:
            print(f"  [等待] {wait_seconds:g} 秒", flush=True)
            time.sleep(wait_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description="无针点穴五阶段真机演示")
    parser.add_argument("--execute", action="store_true", help="确认执行真机动作；省略时只校验")
    parser.add_argument("--bridge-url", default="http://127.0.0.1:8766")
    parser.add_argument("--speed-scale", type=float, default=1.0)
    parser.add_argument("--interface-timeout", type=float, default=45.0)
    args = parser.parse_args()
    if not math.isfinite(args.speed_scale) or not 0 < args.speed_scale <= 1.0:
        parser.error("--speed-scale 必须在 (0, 1]")
    flow = load_flow()
    ready, missing = readiness(flow)
    if not ready:
        raise SystemExit("流程尚未完成示教：" + "；".join(missing))
    if not args.execute:
        print("无针点穴五阶段流程校验通过；未发送任何真机动作。", flush=True)
        return 0
    execute(flow, args.bridge_url, args.speed_scale, args.interface_timeout)
    print("无针点穴五阶段演示完成。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
