#!/usr/bin/env python3
"""Validate and execute one of the editable task 3-5 robot flows."""

from __future__ import annotations

import argparse
import json
import math
import time
import urllib.error
import urllib.request
from pathlib import Path


FLOW_DIR = Path(__file__).resolve().parent / "task_flows"
TASK_IDS = {"tap", "massage", "cupping"}
GESTURES = {
    "pinch": [0, 96, 0, 255, 255, 255],
    "release": [255, 71, 255, 255, 255, 255],
    "fist_ready": [255, 255, 0, 0, 0, 0],
    "fist": [0, 0, 0, 0, 0, 0],
    "grab": [155, 0, 119, 119, 112, 157],
    "massage_grab": [0, 0, 0, 0, 0, 0],
    "massage_on": [0, 0, 255, 0, 0, 0],
}
GESTURE_LABELS = {
    "pinch": "捏", "release": "放", "fist_ready": "握拳准备", "fist": "握拳", "grab": "抓",
    "massage_grab": "抓筋膜枪", "massage_on": "开枪",
}
GESTURE_PAUSE_SECONDS = 1.5
def execution_flow(flow: dict) -> dict:
    """Preserve all taught stages; hold each massage pose for three seconds."""
    if flow.get("id") != "massage":
        return flow
    return {**flow, "stages": [
        {**stage, "steps": [
            dict(step, hold=3.0) if stage.get("id") == "massage" and step.get("type") == "pose"
            else dict(step) for step in stage.get("steps") or []
        ]} for stage in flow["stages"]
    ]}


def load_flow(task_id: str) -> dict:
    if task_id not in TASK_IDS:
        raise ValueError(f"未知任务：{task_id}")
    flow = json.loads((FLOW_DIR / f"{task_id}.json").read_text(encoding="utf-8"))
    if flow.get("schema") != "fivefinger.task-flow/v1" or flow.get("id") != task_id:
        raise ValueError(f"{task_id} 流程格式无效")
    if not isinstance(flow.get("stages"), list) or not flow["stages"]:
        raise ValueError(f"{task_id} 流程必须包含阶段")
    if task_id != "tap" and any(
        step.get("type") in {"fist_ready", "fist"}
        for stage in flow["stages"] for step in stage.get("steps") or []
    ):
        raise ValueError("握拳准备和握拳仅用于捶背舒展")
    if task_id != "cupping" and any(
        step.get("type") == "grab"
        for stage in flow["stages"] for step in stage.get("steps") or []
    ):
        raise ValueError("抓手势仅用于无火罐法")
    massage_gestures = {"massage_grab", "massage_on"}
    if task_id != "massage" and any(
        step.get("type") in massage_gestures
        for stage in flow["stages"] for step in stage.get("steps") or []
    ):
        raise ValueError("筋膜枪手势仅用于筋膜枪按摩")
    for stage in flow["stages"]:
        for step in stage.get("steps") or []:
            if step.get("type") == "massage_grab" and step.get("arm") != "right":
                raise ValueError("抓筋膜枪手势只能使用右手")
            if step.get("type") == "massage_on" and step.get("arm") != "left":
                raise ValueError("开枪手势只能使用左手")
    return flow


def validate_arm(value: object, label: str) -> str:
    arm = str(value or "")
    if arm not in {"left", "right"}:
        raise ValueError(f"{label} 未指定左臂或右臂")
    return arm


def validate_joints(value: object, label: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 7:
        raise ValueError(f"{label} 尚未记录完整的 7 关节位置")
    joints = [float(item) for item in value]
    if not all(math.isfinite(item) for item in joints):
        raise ValueError(f"{label} 含有无效关节值")
    return joints


def readiness(flow: dict) -> tuple[bool, list[str]]:
    flow = execution_flow(flow)
    missing: list[str] = []
    for stage in flow["stages"]:
        steps = stage.get("steps") or []
        if not steps:
            missing.append(f"{stage['label']}没有步骤")
        for index, step in enumerate(steps, 1):
            label = step.get("label", f"{stage['label']}步骤{index}")
            try:
                validate_arm(step.get("arm"), label)
            except ValueError as exc:
                missing.append(str(exc))
            if step.get("type") == "pose":
                if step.get("joints") is None:
                    missing.append(f"{label}未记录")
                elif step.get("feedback_version") != 1:
                    missing.append(f"{label}为旧反馈记录，需覆盖重录")
            elif step.get("type") not in GESTURES:
                missing.append(f"{label}类型无效")
    return not missing, missing


def bridge_request(base_url: str, path: str, body: dict | None = None,
                   timeout: float = 45.0) -> dict:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}", data=payload,
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
    flow = execution_flow(flow)
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
            arm = validate_arm(step.get("arm"), step.get("label", f"步骤{current}"))
            arm_label = "左" if arm == "left" else "右"
            step_type = step.get("type")
            if step_type == "pose":
                joints = validate_joints(step.get("joints"), step.get("label", f"步骤{current}"))
                print(f"  [{current}/{total}] {step.get('label', arm_label + '臂姿态')}", flush=True)
                bridge_request(bridge_url, "/move_joint", {
                    "arm": arm, "joints": joints, "speed": speed, "acceleration": acceleration,
                }, timeout=timeout)
                time.sleep(max(0.0, float(step.get("hold", 0.0))))
            else:
                positions = GESTURES[step_type]
                label = GESTURE_LABELS[step_type]
                print(f"  [等待] {arm_label}手{label}之前停留 {GESTURE_PAUSE_SECONDS:g} 秒", flush=True)
                time.sleep(GESTURE_PAUSE_SECONDS)
                print(f"  [{current}/{total}] {arm_label}手{label} {positions}", flush=True)
                bridge_request(bridge_url, "/hand", {
                    "hand": arm, "positions": positions, "speed_scale": 1.0,
                }, timeout=timeout)
                print(f"  [等待] {arm_label}手{label}完成后停留 {GESTURE_PAUSE_SECONDS:g} 秒", flush=True)
                time.sleep(GESTURE_PAUSE_SECONDS)
        wait_seconds = max(0.0, float(stage.get("wait_seconds", 0.0)))
        if wait_seconds:
            print(f"  [等待] {wait_seconds:g} 秒", flush=True)
            time.sleep(wait_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description="任务三至五关键帧真机演示")
    parser.add_argument("--task", choices=sorted(TASK_IDS), required=True)
    parser.add_argument("--execute", action="store_true", help="确认执行；省略时只校验")
    parser.add_argument("--bridge-url", default="http://127.0.0.1:8766")
    parser.add_argument("--speed-scale", type=float, default=1.0)
    parser.add_argument("--interface-timeout", type=float, default=45.0)
    args = parser.parse_args()
    if not math.isfinite(args.speed_scale) or not 0 < args.speed_scale <= 1.0:
        parser.error("--speed-scale 必须在 (0, 1]")
    flow = load_flow(args.task)
    ready, missing = readiness(flow)
    if not ready:
        raise SystemExit("流程尚未完成示教：" + "；".join(missing))
    if not args.execute:
        print(f"{flow['name']}流程校验通过；未发送任何真机动作。", flush=True)
        return 0
    execute(flow, args.bridge_url, args.speed_scale, args.interface_timeout)
    print(f"{flow['name']}完成。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
