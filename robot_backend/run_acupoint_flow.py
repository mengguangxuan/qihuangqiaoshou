#!/usr/bin/env python3
"""Preview, teach, or execute one left-arm acupoint flow.

Preview is the default.  Hardware commands require both a complete three-frame
teaching sequence and the explicit ``--execute`` flag.
"""

from __future__ import annotations

import argparse
import json
import math
import time
import urllib.error
import urllib.request
from pathlib import Path


FLOW_DIR = Path(__file__).resolve().parent / "acupoint_flows"
TEACHABLE = {"approach", "target", "retreat"}
ARMS = {"left", "right"}
POINT_GESTURE = [0, 0, 255, 0, 0, 0]
REST_GESTURE = [255, 255, 255, 255, 255, 255]
ZERO_JOINTS = [0.0] * 7


def flow_path(code: str) -> Path:
    candidate = FLOW_DIR / f"{code}.json"
    if candidate.parent != FLOW_DIR or not candidate.is_file():
        raise ValueError(f"未知穴位：{code}")
    return candidate


def load_flow(code: str) -> tuple[Path, dict]:
    path = flow_path(code)
    flow = json.loads(path.read_text(encoding="utf-8"))
    if flow.get("schema") != "fivefinger.acupoint-flow/v1":
        raise ValueError(f"不支持的流程格式：{path}")
    return path, flow


def validate_joints(value: object, label: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 7:
        raise ValueError(f"{label} 尚未示教完整的 7 关节位置")
    joints = [float(item) for item in value]
    if not all(math.isfinite(item) for item in joints):
        raise ValueError(f"{label} 含有无效关节值")
    return joints


def validate_arm(value: object) -> str:
    arm = str(value or "").strip().lower()
    if arm not in ARMS:
        raise ValueError("穴位流程的 arm 必须是 left 或 right")
    return arm


def readiness(flow: dict) -> tuple[bool, list[str]]:
    missing = [frame.get("name", "?") for frame in flow.get("keyframes", [])
               if frame.get("name") in TEACHABLE and frame.get("joints") is None]
    return not missing, missing


def capture_joint_state(namespace: str, timeout: float, arm: str = "left") -> list[float]:
    import rclpy
    from sensor_msgs.msg import JointState

    sample: list[float] | None = None
    rclpy.init()
    node = rclpy.create_node("fivefinger_acupoint_teach", namespace="/" + namespace.strip("/"))

    def receive(message: JointState) -> None:
        nonlocal sample
        if len(message.position) >= 7:
            sample = [round(float(value), 6) for value in message.position[:7]]

    arm = validate_arm(arm)
    subscription = node.create_subscription(JointState, f"{arm}_arm/joint_states", receive, 10)
    try:
        deadline = time.monotonic() + timeout
        while sample is None and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.1)
        if sample is None:
            arm_label = "左臂" if arm == "left" else "右臂"
            raise RuntimeError(f"没有收到{arm_label} joint_states，确认驱动与 robot1 命名空间已启动")
        return sample
    finally:
        node.destroy_subscription(subscription)
        node.destroy_node()
        rclpy.shutdown()


def capture_joint_state_from_bridge(bridge_url: str, timeout: float, arm: str = "left") -> list[float]:
    arm = validate_arm(arm)
    arm_label = "左臂" if arm == "left" else "右臂"
    state = bridge_request(bridge_url, "/state", timeout=timeout)
    ages = state.get("joint_state_age_s") or {}
    age = ages.get(arm)
    if age is None or float(age) > 0.5:
        raise RuntimeError(f"8766 返回的{arm_label} joint_states 已过期：{age}")
    joints = (state.get("joints") or {}).get(arm)
    return [round(value, 6) for value in validate_joints(joints, f"{arm_label}当前姿态")]


def record_keyframe(path: Path, flow: dict, keyframe: str, namespace: str, timeout: float,
                    bridge_url: str) -> None:
    if keyframe not in TEACHABLE:
        raise ValueError(f"只能示教：{', '.join(sorted(TEACHABLE))}")
    arm = validate_arm(flow.get("arm"))
    arm_label = "左臂" if arm == "left" else "右臂"
    joints = capture_joint_state_from_bridge(bridge_url, timeout, arm)
    for frame in flow["keyframes"]:
        if frame.get("name") == keyframe:
            frame["joints"] = joints
            frame["recorded_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            frame["recorded_from"] = "http://127.0.0.1:8766/state"
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(json.dumps(flow, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(path)
            print(f"已记录 {flow['point']['name']} / {frame['label']}：{joints}（来源：8766 最新{arm_label}状态）", flush=True)
            return
    raise ValueError(f"流程缺少关键帧：{keyframe}")


def bridge_request(base_url: str, path: str, body: dict | None = None, timeout: float = 45.0) -> dict:
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


def execute(flow: dict, bridge_url: str, speed_scale: float, timeout: float,
            reverse: bool = False, round_trip: bool = False,
            turnaround_pause: float = 5.0) -> None:
    ready, missing = readiness(flow)
    if not ready:
        raise RuntimeError(f"流程尚未完成示教：{', '.join(missing)}")
    health = bridge_request(bridge_url, "/health", timeout=timeout)
    if not health.get("ok"):
        raise RuntimeError("动作桥健康检查未通过")
    arm = validate_arm(flow.get("arm"))
    by_name = {frame.get("name"): frame for frame in flow["keyframes"]}
    forward_names = ["approach", "target", "retreat"]
    sequences = [list(reversed(forward_names))] if reverse else [forward_names]
    if round_trip:
        sequences.append(list(reversed(forward_names)))

    # A complete return sends one seven-axis MoveJ target.  Do not replay the
    # staging poses here: that would create several visibly separate moves even
    # though each individual service request already contains all seven joints.
    should_zero = reverse or round_trip
    total = sum(len(sequence) for sequence in sequences) + (1 if should_zero else 0)
    step = 0
    gesture_active = False
    try:
        if not reverse:
            print(f"[手势] {arm} 手比出 1", flush=True)
            bridge_request(bridge_url, "/hand", {
                "hand": arm, "positions": POINT_GESTURE, "speed_scale": 1.0,
            })
            gesture_active = True
        for sequence_index, ordered_names in enumerate(sequences):
            if sequence_index:
                pause = max(0.0, float(turnaround_pause))
                print(f"[等待] 正向三帧已完成，停留 {pause:g} 秒", flush=True)
                time.sleep(pause)
                print(f"[手势] {arm} 手保持 1，开始倒序返回", flush=True)
            for frame_name in ordered_names:
                frame = by_name[frame_name]
                joints = validate_joints(frame.get("joints"), frame.get("label", frame.get("name", "?")))
                step += 1
                print(f"[{step}/{total}] {frame['label']}", flush=True)
                bridge_request(bridge_url, "/move_joint", {
                    "arm": arm,
                    "joints": joints,
                    "speed": float(flow.get("speed", 0.8)) * speed_scale,
                    "acceleration": float(flow.get("acceleration", 0.8)) * speed_scale,
                })
                time.sleep(max(0.0, float(frame.get("hold", 0.0))))
        if should_zero:
            step += 1
            print(f"[{step}/{total}] {arm} 臂七轴同步归零（单次 MoveJ）", flush=True)
            bridge_request(bridge_url, "/move_joint", {
                "arm": arm,
                "joints": ZERO_JOINTS,
                "speed": float(flow.get("speed", 0.8)) * speed_scale,
                "acceleration": float(flow.get("acceleration", 0.8)) * speed_scale,
            })
        if gesture_active:
            if should_zero:
                print(f"[手势] {arm} 臂返程与关节归零完成，手势恢复", flush=True)
            else:
                print(f"[手势] {arm} 臂三帧完成，手势恢复", flush=True)
            bridge_request(bridge_url, "/hand", {
                "hand": arm, "positions": REST_GESTURE, "speed_scale": 1.0,
            })
            gesture_active = False
    finally:
        if gesture_active:
            try:
                bridge_request(bridge_url, "/hand", {
                    "hand": arm, "positions": REST_GESTURE, "speed_scale": 1.0,
                })
            except Exception:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description="五穴左臂上场、示教与执行")
    parser.add_argument("--point", required=True, choices=[path.stem for path in sorted(FLOW_DIR.glob("*.json"))])
    parser.add_argument("--execute", action="store_true", help="确认执行真机动作；省略时只预览")
    parser.add_argument("--reverse", action="store_true", help="将三帧按撤离、指向、接近倒序执行")
    parser.add_argument("--round-trip", action="store_true",
                        help="正向执行三帧，等待 5 秒，再倒序执行三帧")
    parser.add_argument("--teach", choices=sorted(TEACHABLE), help="记录当前左臂关节为指定关键帧")
    parser.add_argument("--namespace", default="robot1")
    parser.add_argument("--bridge-url", default="http://127.0.0.1:8766")
    parser.add_argument("--speed-scale", type=float, default=1.0)
    parser.add_argument("--interface-timeout", type=float, default=6.0)
    args = parser.parse_args()
    if not math.isfinite(args.speed_scale) or not 0 < args.speed_scale <= 1.0:
        parser.error("--speed-scale 必须在 (0, 1]")

    path, flow = load_flow(args.point)
    if args.teach:
        if args.execute or args.reverse or args.round_trip:
            parser.error("--teach 与 --execute/--reverse/--round-trip 不能同时使用")
        record_keyframe(path, flow, args.teach, args.namespace, args.interface_timeout, args.bridge_url)
        return 0

    ready, missing = readiness(flow)
    if not args.execute:
        if args.reverse or args.round_trip:
            parser.error("--reverse/--round-trip 必须与 --execute 一起使用")
        print(json.dumps({
            "point": flow["point"],
            "arm": flow["arm"],
            "ready": ready,
            "missing": missing,
            "keyframes": flow["keyframes"],
            "note": "仅预览，没有发送机械臂命令",
        }, ensure_ascii=False, indent=2), flush=True)
        return 0
    if args.reverse and args.round_trip:
        parser.error("--reverse 与 --round-trip 不能同时使用")
    execute(
        flow,
        args.bridge_url,
        args.speed_scale,
        args.interface_timeout,
        reverse=args.reverse,
        round_trip=args.round_trip,
    )
    return 0


if __name__ == "__main__":
    import sys
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("流程已中断，不再发送后续命令。", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"流程失败：{exc}", file=sys.stderr)
        raise SystemExit(1)
