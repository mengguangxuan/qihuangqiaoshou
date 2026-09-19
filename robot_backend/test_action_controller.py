import threading
import sys
import types
import unittest
import numpy as np
from http import HTTPStatus
from unittest.mock import MagicMock, patch

# The controller logic under test does not use ROS, while the application
# module imports ROS message classes for its camera node.  Provide minimal
# import-only stand-ins on Windows, where the ROS runtime is intentionally
# hosted in WSL.
if "rclpy" not in sys.modules:
    fake_rclpy = types.ModuleType("rclpy")
    fake_rclpy_node = types.ModuleType("rclpy.node")
    fake_rclpy_qos = types.ModuleType("rclpy.qos")
    fake_sensor_msgs = types.ModuleType("sensor_msgs")
    fake_sensor_msgs_msg = types.ModuleType("sensor_msgs.msg")

    fake_rclpy_node.Node = type("Node", (), {})
    for name in ("DurabilityPolicy", "HistoryPolicy", "QoSProfile", "ReliabilityPolicy"):
        setattr(fake_rclpy_qos, name, type(name, (), {}))
    fake_sensor_msgs_msg.Image = type("Image", (), {})
    sys.modules.update({
        "rclpy": fake_rclpy,
        "rclpy.node": fake_rclpy_node,
        "rclpy.qos": fake_rclpy_qos,
        "sensor_msgs": fake_sensor_msgs,
        "sensor_msgs.msg": fake_sensor_msgs_msg,
    })

import acupoint_demo


class TrackingFallbackTests(unittest.TestCase):
    def test_missing_top_left_uses_other_three_corners_and_neck_tag(self):
        node = object.__new__(acupoint_demo.AcupointNode)
        node.last_transform = None
        node.last_transform_at = 0.0
        expected = np.asarray([
            [780.0, 40.0, 430.0],
            [25.0, 690.0, 210.0],
            [0.0015, 0.0008, 1.0],
        ])
        node.tracked = {}
        for marker_id in (1, 2, 3, 19):
            u, v = acupoint_demo.MARKER_LAYOUT[marker_id]
            projected = expected @ np.asarray([u, v, 1.0])
            center = projected[:2] / projected[2]
            node.tracked[marker_id] = acupoint_demo.TrackedMarker(center, 10.0)

        transform, ids = node.estimate_transform(10.0)

        self.assertEqual([1, 2, 3, 19], ids)
        self.assertIsNotNone(transform)
        for point in ((0.5, -0.2), (0.23, 0.31), (0.68, 0.72)):
            actual = np.asarray(node.project(transform, point))
            expected_point = expected @ np.asarray([*point, 1.0])
            expected_point = np.rint(expected_point[:2] / expected_point[2]).astype(int)
            np.testing.assert_allclose(actual, expected_point, atol=1)


class EmergencyReleaseTests(unittest.TestCase):
    def make_controller(self):
        controller = object.__new__(acupoint_demo.ActionController)
        controller.lock = threading.Lock()
        controller.event_lock = threading.Lock()
        controller.events = []
        controller.state = {
            "enabled": True,
            "arm_enabled": {"left": False, "right": False},
            "robot_enabled": False,
            "running": False,
            "phase": "emergency_stop",
            "message": "已急停",
            "bridge": {"checked": True, "ok": True},
            "updated_at": 0.0,
        }
        controller._bridge_request = MagicMock(
            return_value=(HTTPStatus.OK, {"ok": True})
        )
        controller._record_event = MagicMock()
        return controller

    def test_release_clears_emergency_without_enabling_arms(self):
        controller = self.make_controller()

        state = controller.emergency_release()

        controller._bridge_request.assert_called_once_with(
            "POST", "/emergency_stop", {"emergency": False}
        )
        self.assertEqual("emergency_released", state["phase"])
        self.assertEqual({"left": False, "right": False}, state["arm_enabled"])
        self.assertFalse(state["robot_enabled"])
        controller._record_event.assert_called_once()
        self.assertEqual("emergency_release", controller._record_event.call_args.args[0])


class PreflightRecoveryTests(unittest.TestCase):
    def test_transient_stale_feedback_is_retried_until_fresh(self):
        controller = object.__new__(acupoint_demo.ActionController)
        controller.lock = threading.Lock()
        controller.bridge_url = "http://127.0.0.1:8766"
        controller.state = {"bridge": {}, "updated_at": 0.0}
        stale_health = {
            "ok": False,
            "errors": ["关节状态过期: ['left', 'right']"],
        }
        fresh_health = {"ok": True, "errors": []}
        robot_state = {"ok": True, "joint_state_age_s": {"left": 0.02, "right": 0.02}}
        controller._bridge_request = MagicMock(side_effect=[
            (HTTPStatus.SERVICE_UNAVAILABLE, stale_health),
            (HTTPStatus.OK, robot_state),
            (HTTPStatus.OK, fresh_health),
            (HTTPStatus.OK, robot_state),
        ])

        with patch.object(acupoint_demo.time, "sleep") as sleep:
            result = controller.preflight()

        self.assertTrue(result["ok"])
        self.assertEqual(4, controller._bridge_request.call_count)
        sleep.assert_called_once_with(0.25)

    def test_non_feedback_failure_is_not_retried(self):
        controller = object.__new__(acupoint_demo.ActionController)
        controller.lock = threading.Lock()
        controller.bridge_url = "http://127.0.0.1:8766"
        controller.state = {"bridge": {}, "updated_at": 0.0}
        health = {"ok": False, "errors": ["真机地址不可达: 192.168.10.21"]}
        robot_state = {"ok": True}
        controller._bridge_request = MagicMock(side_effect=[
            (HTTPStatus.SERVICE_UNAVAILABLE, health),
            (HTTPStatus.OK, robot_state),
        ])

        with patch.object(acupoint_demo.time, "sleep") as sleep:
            result = controller.preflight()

        self.assertFalse(result["ok"])
        self.assertEqual(2, controller._bridge_request.call_count)
        sleep.assert_not_called()


class TaskFlowGateTests(unittest.TestCase):
    def make_controller(self, tracking=False):
        controller = object.__new__(acupoint_demo.ActionController)
        controller.lock = threading.Lock()
        controller.event_lock = threading.Lock()
        controller.events = []
        controller.enabled = True
        controller.speed_scale = 1.0
        controller.shared = types.SimpleNamespace(
            lock=threading.Lock(), status={"tracking": tracking}
        )
        controller.state = {
            "enabled": True,
            "arm_enabled": {"left": True, "right": True},
            "robot_enabled": True,
            "running": False,
            "phase": "idle",
            "message": "",
            "bridge": {"checked": True, "ok": True},
            "updated_at": 0.0,
        }
        controller.preflight = MagicMock(return_value={"ok": True, "message": "动作桥已就绪"})
        controller.task_flow_info = MagicMock(return_value={
            "name": "测试流程", "arms": ["left", "right"], "ready": True, "missing": []
        })
        controller._record_event = MagicMock()
        controller._run_task_flow = MagicMock()
        return controller

    def test_cupping_can_start_without_apriltag_tracking(self):
        controller = self.make_controller(tracking=False)
        with patch.object(acupoint_demo.threading, "Thread") as thread_class:
            state = controller.request_task_flow("cupping")

        self.assertTrue(state["running"])
        self.assertEqual("task_queued", state["phase"])
        thread_class.return_value.start.assert_called_once_with()

    def test_other_task_flows_still_require_apriltag_tracking(self):
        controller = self.make_controller(tracking=False)
        with self.assertRaisesRegex(RuntimeError, "AprilTag 定位尚未稳定"):
            controller.request_task_flow("tap")

    def test_single_pose_request_queues_exact_recorded_pose(self):
        controller = self.make_controller(tracking=False)
        controller._load_task_flow = MagicMock(return_value={
            "speed": 1.5,
            "acceleration": 1.0,
            "stages": [{"id": "take", "label": "抓取训练罐", "steps": [{
                "id": "pose-1", "type": "pose", "arm": "right",
                "label": "右臂姿态 1", "joints": [0.25] * 7, "feedback_version": 1,
            }]}],
        })
        controller._run_task_pose = MagicMock()
        with patch.object(acupoint_demo.threading, "Thread") as thread_class:
            state = controller.request_task_pose("cupping", "take", "pose-1")

        self.assertTrue(state["running"])
        self.assertEqual("task_pose_queued", state["phase"])
        thread_args = thread_class.call_args.kwargs["args"]
        self.assertEqual("right", thread_args[2])
        self.assertEqual([0.25] * 7, thread_args[3])
        self.assertEqual(1.5, thread_args[4])
        self.assertEqual(1.0, thread_args[5])
        thread_class.return_value.start.assert_called_once_with()

    def test_single_pose_worker_sends_one_move_joint(self):
        controller = self.make_controller(tracking=False)
        controller.state["running"] = True
        controller._bridge_request = MagicMock(return_value=(HTTPStatus.OK, {"ok": True}))

        controller._run_task_pose("cupping", "左臂姿态 1", "left", [0.1] * 7, 1.5, 1.0)

        controller._bridge_request.assert_called_once_with("POST", "/move_joint", {
            "arm": "left", "joints": [0.1] * 7, "speed": 1.5, "acceleration": 1.0,
        }, timeout=60.0)
        self.assertFalse(controller.state["running"])
        self.assertEqual("task_pose_done", controller.state["phase"])

    def test_needle_single_pose_uses_left_arm_without_running_other_steps(self):
        controller = self.make_controller(tracking=False)
        controller._load_needle_flow = MagicMock(return_value={
            "speed": 1.5, "acceleration": 1.0,
            "stages": [{"id": "take", "label": "拿针", "steps": [
                {"id": "p1", "type": "pose", "joints": [0.2] * 7, "feedback_version": 1},
                {"id": "grip", "type": "pinch"},
            ]}],
        })
        with patch.object(acupoint_demo.threading, "Thread") as thread:
            controller.request_task_pose("needle", "take", "p1")
        self.assertEqual(("left", [0.2] * 7, 1.5, 1.0), thread.call_args.kwargs["args"][2:])
        thread.return_value.start.assert_called_once()

    def test_massage_grab_is_restricted_to_right_hand(self):
        controller = self.make_controller()
        with self.assertRaisesRegex(ValueError, "只能使用右手"):
            controller.task_add_step("massage", "take", "massage_grab", "left")

    def test_massage_on_is_restricted_to_left_hand(self):
        controller = self.make_controller()
        with self.assertRaisesRegex(ValueError, "只能使用左手"):
            controller.task_add_step("massage", "start", "massage_on", "right")


if __name__ == "__main__":
    unittest.main()
