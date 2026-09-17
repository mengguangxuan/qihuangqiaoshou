#!/usr/bin/env python3
"""WSL ROS2 -> localhost HTTP bridge for Task 1.

This process never starts a motion by itself.  It only exposes blocking,
validated wrappers around the existing LBot ROS2 services and hand topics.
"""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import subprocess
import threading
import time
from typing import Any

import rclpy
from geometry_msgs.msg import PoseStamped
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from sensor_msgs.msg import JointState
from std_msgs.msg import UInt8MultiArray

from lbot_arm_interfaces.srv import (
    ForwardKinematics, GetCurrentFrame, InverseKinematics, MoveJ, MoveJP, SetEmergency,
)


HAND_NOMINAL_FULL_RANGE_SECONDS = 0.35
HAND_COMMAND_PERIOD_SECONDS = 0.02


class BridgeFailure(RuntimeError):
    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category
        self.details = {}


def ros_fields(value):
    if hasattr(value, "get_fields_and_field_types"):
        return {name: ros_fields(getattr(value, name)) for name in value.get_fields_and_field_types()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {key: ros_fields(item) for key, item in value.items()}
    try:
        return [ros_fields(item) for item in value]
    except TypeError:
        return repr(value)


class LBotBridge(Node):
    def __init__(self, timeout: float, robot_namespace: str, robot_ip: str):
        super().__init__("fivefinger_task1_http_bridge")
        self.diagnostic_context = threading.local()
        self.timeout = timeout
        self.robot_namespace = "/" + robot_namespace.strip("/") if robot_namespace.strip("/") else ""
        self.robot_ip = robot_ip
        self.state_lock = threading.Lock()
        self.joints: dict[str, list[float]] = {"left": [], "right": []}
        self.poses: dict[str, dict[str, Any]] = {"left": {}, "right": {}}
        self.updated_at: dict[str, float] = {"left": 0.0, "right": 0.0}
        self.service_clients: dict[tuple[str, str], Any] = {}
        for arm in ("left", "right"):
            prefix = f"{self.robot_namespace}/{arm}_arm"
            self.service_clients[(arm, "move_joint")] = self.create_client(MoveJ, f"{prefix}/move_joint")
            self.service_clients[(arm, "move_pose")] = self.create_client(MoveJP, f"{prefix}/move_pose")
            self.service_clients[(arm, "ik")] = self.create_client(InverseKinematics, f"{prefix}/inverse_kinematics")
            self.service_clients[(arm, "fk")] = self.create_client(ForwardKinematics, f"{prefix}/forward_kinematics")
            self.service_clients[(arm, "frame")] = self.create_client(GetCurrentFrame, f"{prefix}/get_current_tool_frame")
            self.service_clients[(arm, "emergency")] = self.create_client(SetEmergency, f"{prefix}/set_emergency_stop")
            self.create_subscription(JointState, f"{prefix}/joint_states", lambda msg, a=arm: self._joint(a, msg), 10)
            self.create_subscription(PoseStamped, f"{prefix}/pose_states", lambda msg, a=arm: self._pose(a, msg), 10)
        self.hand_publishers = {
            arm: self.create_publisher(UInt8MultiArray, f"{self.robot_namespace}/{arm}_hand/set_l6_joint", 10)
            for arm in ("left", "right")
        }
        # Gesture 1 is the task's released/open posture.  The task requires an
        # empty hand at startup, so it is also the safe initial ramp origin.
        self.hand_positions = {arm: [255] * 6 for arm in ("left", "right")}
        self.hand_locks = {arm: threading.Lock() for arm in ("left", "right")}
        self.hand_abort = threading.Event()

    def _joint(self, arm: str, message: JointState) -> None:
        with self.state_lock:
            self.joints[arm] = [float(v) for v in message.position]
            self.updated_at[arm] = time.time()

    def _pose(self, arm: str, message: PoseStamped) -> None:
        pose = message.pose
        with self.state_lock:
            self.poses[arm] = {
                "position": [pose.position.x, pose.position.y, pose.position.z],
                "quaternion_xyzw": [pose.orientation.x, pose.orientation.y, pose.orientation.z, pose.orientation.w],
                "frame_id": message.header.frame_id,
            }

    def _call(self, client: Any, request: Any) -> Any:
        started = time.monotonic()
        context = getattr(self, "diagnostic_context", None)
        request_id = getattr(context, "request_id", "")
        details = {"request_id": request_id, "service": client.srv_name,
                   "request": ros_fields(request)}
        if hasattr(self, "state_lock"):
            with self.state_lock:
                details["joint_state_before"] = {arm: list(joints) for arm, joints in self.joints.items()}
                details["joint_state_age_s"] = {arm: time.time() - stamp if stamp else None
                                                for arm, stamp in self.updated_at.items()}
        self.get_logger().info(json.dumps({"event": "ros_request", **details}, ensure_ascii=False))
        response = None
        try:
            if not client.service_is_ready() and not client.wait_for_service(timeout_sec=1.0):
                raise BridgeFailure("ros_service_unavailable", f"ROS2 ??????{client.srv_name}")
            future = client.call_async(request)
            event = threading.Event()
            future.add_done_callback(lambda _: event.set())
            if not event.wait(self.timeout):
                raise BridgeFailure("ros_timeout", f"ROS2 ?????{client.srv_name}?????????????")
            error = future.exception()
            if error:
                raise BridgeFailure("ros_call_error", f"ROS2 ?????{client.srv_name}: {error}")
            response = future.result()
            if response is None:
                raise BridgeFailure("ros_call_error", "ROS2 ?????")
            if hasattr(response, "success") and not response.success:
                category = "kinematics_failure" if client.srv_name.endswith(("/inverse_kinematics", "/forward_kinematics")) else "motion_failure_unknown" if client.srv_name.endswith(("/move_joint", "/move_pose", "/move_linear")) else "service_failure_unknown"
                raise BridgeFailure(category, f"ROS2 ???????{client.srv_name}")
            return response
        except Exception as exc:
            failure = exc if isinstance(exc, BridgeFailure) else BridgeFailure("ros_call_error", str(exc))
            failure.details = {**details, "response": ros_fields(response),
                               "elapsed_s": round(time.monotonic() - started, 4)}
            self.get_logger().error(json.dumps({"event": "ros_failure", "category": failure.category,
                                               "error": str(failure), **failure.details}, ensure_ascii=False))
            raise failure from (exc if failure is not exc else None)
        finally:
            self.get_logger().info(json.dumps({"event": "ros_complete", **details,
                "response": ros_fields(response), "elapsed_s": round(time.monotonic() - started, 4)}, ensure_ascii=False))

    @staticmethod
    def _arm(value: Any) -> str:
        arm = str(value)
        if arm not in {"left", "right"}:
            raise ValueError("arm/hand 必须是 left 或 right")
        return arm

    @staticmethod
    def _vector(value: Any, size: int, name: str) -> list[float]:
        if not isinstance(value, list) or len(value) != size:
            raise ValueError(f"{name} 必须是长度 {size} 的数组")
        result = [float(v) for v in value]
        if any(not (-1000.0 < v < 1000.0) for v in result):
            raise ValueError(f"{name} 包含异常值")
        return result

    @staticmethod
    def _fill_pose(request: Any, body: dict[str, Any]) -> None:
        position = LBotBridge._vector(body.get("position"), 3, "position")
        euler = LBotBridge._vector(body.get("euler"), 3, "euler")
        request.position.x, request.position.y, request.position.z = position
        request.euler.x, request.euler.y, request.euler.z = euler

    def health(self) -> dict[str, Any]:
        robot_reachable = subprocess.run(
            ["ping", "-c", "1", "-W", "1", self.robot_ip],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        ).returncode == 0
        missing = [client.srv_name for client in self.service_clients.values() if not client.service_is_ready()]
        with self.state_lock:
            now = time.time()
            ages = {arm: round(now - stamp, 3) if stamp else None for arm, stamp in self.updated_at.items()}
        stale = [arm for arm, age in ages.items() if age is None or not 0 <= age <= 1.0]
        hand_subscribers = {arm: publisher.get_subscription_count() for arm, publisher in self.hand_publishers.items()}
        missing_hands = [arm for arm, count in hand_subscribers.items() if count < 1]
        frames: dict[str, str] = {}
        for arm in ("left", "right"):
            if self.service_clients[(arm, "frame")].service_is_ready():
                try:
                    response = self._call(self.service_clients[(arm, "frame")], GetCurrentFrame.Request())
                    # The current LBot driver fills frame.name but leaves the
                    # legacy top-level name field empty.
                    frames[arm] = str(response.frame.name or response.name)
                except Exception as exc:  # noqa: BLE001
                    frames[arm] = f"ERROR: {exc}"
        wrong_frames = [arm for arm, name in frames.items() if name != "Arm_Tip"]
        ok = robot_reachable and not missing and not stale and not wrong_frames and not missing_hands
        return {"ok": ok, "ros2": True, "missing_services": missing, "joint_state_age_s": ages,
                "robot_ip": self.robot_ip, "robot_reachable": robot_reachable, "hand_subscribers": hand_subscribers,
                "tool_frames": frames, "errors": ([] if robot_reachable else [f"真机地址不可达: {self.robot_ip}"])
                + ([f"关节状态过期: {stale}"] if stale else [])
                + ([f"灵巧手话题没有订阅者: {missing_hands}"] if missing_hands else [])
                + ([f"工具坐标系不是 Arm_Tip: {wrong_frames}"] if wrong_frames else [])}

    def state(self) -> dict[str, Any]:
        with self.state_lock:
            now = time.time()
            return {"ok": True, "joints": dict(self.joints), "poses": dict(self.poses),
                    "joint_state_age_s": {arm: now - stamp if stamp else None
                                          for arm, stamp in self.updated_at.items()},
                    "updated_at": dict(self.updated_at)}

    def move_joint(self, body: dict[str, Any]) -> dict[str, Any]:
        arm = self._arm(body.get("arm"))
        request = MoveJ.Request()
        request.joints = self._vector(body.get("joints"), 7, "joints")
        request.speed = float(body.get("speed", 0.3))
        request.acce = float(body.get("acceleration", 0.3))
        request.block = True
        self._call(self.service_clients[(arm, "move_joint")], request)
        return {"ok": True}

    def move_cartesian(self, body: dict[str, Any], linear: bool) -> dict[str, Any]:
        arm = self._arm(body.get("arm"))
        service_type = MoveJP  # The legacy linear route also uses MoveJP.
        request = service_type.Request()
        self._fill_pose(request, body)
        request.speed = float(body.get("speed", 0.3))
        request.acce = float(body.get("acceleration", 0.3))
        request.block = True
        key = "move_pose"
        self._call(self.service_clients[(arm, key)], request)
        return {"ok": True}

    def ik(self, body: dict[str, Any]) -> dict[str, Any]:
        arm = self._arm(body.get("arm"))
        request = InverseKinematics.Request()
        # The installed SDK dereferences the initial-joint pointer while
        # building its request. Never let the driver convert [] to nullptr.
        with self.state_lock:
            seed = list(self.joints[arm])
            stamp = self.updated_at[arm]
        if not stamp or not 0 <= time.time() - stamp <= 1.0:
            raise BridgeFailure("joint_state_stale", f"{arm} IK 缺少新鲜关节状态，拒绝发送空初值")
        request.joints = self._vector(seed, 7, "IK initial joints")
        self._fill_pose(request, body)
        self.get_logger().info(f"IK request: arm={arm}, seed={request.joints}, position={body.get('position')}, euler={body.get('euler')}")
        response = self._call(self.service_clients[(arm, "ik")], request)
        return {"ok": True, "joints": [float(v) for v in response.joints]}

    def fk(self, body: dict[str, Any]) -> dict[str, Any]:
        arm = self._arm(body.get("arm"))
        request = ForwardKinematics.Request()
        request.joints = self._vector(body.get("joints"), 7, "joints")
        response = self._call(self.service_clients[(arm, "fk")], request)
        return {
            "ok": True,
            "position": [response.position.x, response.position.y, response.position.z],
            "euler": [response.euler.x, response.euler.y, response.euler.z],
        }

    def hand(self, body: dict[str, Any]) -> dict[str, Any]:
        hand = self._arm(body.get("hand"))
        values = body.get("positions")
        if not isinstance(values, list) or len(values) != 6:
            raise ValueError("positions 必须是 6 个 0..255 整数")
        target = [int(v) for v in values]
        if any(v < 0 or v > 255 for v in target):
            raise ValueError("手势值必须位于 0..255")
        speed_scale = float(body.get("speed_scale", 1.0))
        if not math.isfinite(speed_scale) or speed_scale <= 0:
            raise ValueError("speed_scale 必须是正有限数")
        with self.hand_locks[hand]:
            start = list(self.hand_positions[hand])
            max_delta = max(abs(end - begin) for begin, end in zip(start, target))
            duration = HAND_NOMINAL_FULL_RANGE_SECONDS * max_delta / 255.0 / speed_scale
            steps = max(1, math.ceil(duration / HAND_COMMAND_PERIOD_SECONDS))
            period = duration / steps
            for step in range(1, steps + 1):
                if self.hand_abort.is_set():
                    raise BridgeFailure("emergency_stop", f"{hand} 手势被急停中断")
                ratio = step / steps
                current = [round(begin + (end - begin) * ratio)
                           for begin, end in zip(start, target)]
                message = UInt8MultiArray()
                message.data = current
                self.hand_publishers[hand].publish(message)
                self.hand_positions[hand] = current
                if step < steps and self.hand_abort.wait(period):
                    raise BridgeFailure("emergency_stop", f"{hand} 手势被急停中断")
        return {"ok": True, "speed_scale": speed_scale,
                "duration_seconds": duration, "steps": steps}

    def emergency(self, enabled: bool) -> dict[str, Any]:
        if enabled:
            self.hand_abort.set()
        else:
            self.hand_abort.clear()
        errors: list[str] = []
        for arm in ("left", "right"):
            try:
                request = SetEmergency.Request()
                request.emergency = bool(enabled)
                self._call(self.service_clients[(arm, "emergency")], request)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{arm}: {exc}")
        if errors:
            raise RuntimeError("；".join(errors))
        return {"ok": True, "emergency": bool(enabled)}


class Handler(BaseHTTPRequestHandler):
    bridge: LBotBridge

    def log_message(self, format: str, *args: Any) -> None:
        self.bridge.get_logger().info(format % args)

    def _body(self) -> dict[str, Any]:
        size = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(size).decode("utf-8")) if size else {}

    def _send(self, status: int, value: dict[str, Any]) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        try:
            if self.path == "/health":
                value = self.bridge.health()
                self._send(200 if value["ok"] else 503, value)
            elif self.path == "/state":
                self._send(200, self.bridge.state())
            else:
                self._send(404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": str(exc), "category": getattr(exc, "category", "bridge_internal_error"),
                             "details": getattr(exc, "details", {})})

    def do_POST(self) -> None:  # noqa: N802
        self.bridge.diagnostic_context.request_id = self.headers.get("X-Request-ID", "")
        try:
            body = self._body()
            routes = {
                "/move_joint": lambda: self.bridge.move_joint(body),
                "/move_pose": lambda: self.bridge.move_cartesian(body, False),
                "/move_linear": lambda: self.bridge.move_cartesian(body, True),
                "/inverse_kinematics": lambda: self.bridge.ik(body),
                "/forward_kinematics": lambda: self.bridge.fk(body),
                "/hand": lambda: self.bridge.hand(body),
                "/emergency_stop": lambda: self.bridge.emergency(bool(body.get("emergency", True))),
            }
            if self.path not in routes:
                self._send(404, {"error": "not found"})
                return
            self._send(200, routes[self.path]())
        except (ValueError, json.JSONDecodeError) as exc:
            self._send(400, {"error": str(exc), "category": "invalid_request"})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": str(exc), "category": getattr(exc, "category", "bridge_internal_error"),
                             "details": getattr(exc, "details", {})})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--service-timeout", type=float, default=30.0)
    parser.add_argument("--robot-namespace", default="/robot1")
    parser.add_argument("--robot-ip", default="192.168.10.21")
    args = parser.parse_args()
    rclpy.init()
    bridge = LBotBridge(args.service_timeout, args.robot_namespace, args.robot_ip)
    Handler.bridge = bridge
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(bridge)
    spin_thread = threading.Thread(target=executor.spin, daemon=True)
    spin_thread.start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    bridge.get_logger().info(f"Task 1 action bridge listening on {args.host}:{args.port}")
    try:
        server.serve_forever()
    finally:
        server.server_close()
        executor.shutdown()
        bridge.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
