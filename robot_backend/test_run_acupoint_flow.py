import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run_acupoint_flow as flow_runner


BASE_FLOW = {
    "point": {"name": "测试穴位"},
    "arm": "left",
    "keyframes": [
        {"name": "approach", "label": "接近位", "joints": [0.1] * 7},
        {"name": "target", "label": "指向位", "joints": None},
        {"name": "retreat", "label": "撤离位", "joints": None},
    ],
}


class RecordKeyframeTests(unittest.TestCase):
    def test_duplicate_capture_is_saved_without_similarity_judgement(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "flow.json"
            flow = copy.deepcopy(BASE_FLOW)
            path.write_text(json.dumps(flow, ensure_ascii=False), encoding="utf-8")

            with patch.object(flow_runner, "capture_joint_state_from_bridge", return_value=[0.1] * 7):
                flow_runner.record_keyframe(path, flow, "target", "robot1", 2.0, "http://127.0.0.1:8766")

            saved = json.loads(path.read_text(encoding="utf-8"))
            target = next(frame for frame in saved["keyframes"] if frame["name"] == "target")
            self.assertEqual([0.1] * 7, target["joints"])

    def test_distinct_capture_is_written_with_source_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "flow.json"
            flow = copy.deepcopy(BASE_FLOW)
            path.write_text(json.dumps(flow, ensure_ascii=False), encoding="utf-8")
            new_joints = [0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.2]

            with patch.object(flow_runner, "capture_joint_state_from_bridge", return_value=new_joints):
                flow_runner.record_keyframe(path, flow, "target", "robot1", 2.0, "http://127.0.0.1:8766")

            saved = json.loads(path.read_text(encoding="utf-8"))
            target = next(frame for frame in saved["keyframes"] if frame["name"] == "target")
            self.assertEqual(new_joints, target["joints"])
            self.assertEqual("http://127.0.0.1:8766/state", target["recorded_from"])
            self.assertTrue(target["recorded_at"])

    def test_capture_reads_the_flow_configured_right_arm(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "flow.json"
            flow = copy.deepcopy(BASE_FLOW)
            flow["arm"] = "right"
            path.write_text(json.dumps(flow, ensure_ascii=False), encoding="utf-8")

            with patch.object(flow_runner, "capture_joint_state_from_bridge", return_value=[0.2] * 7) as capture:
                flow_runner.record_keyframe(path, flow, "target", "robot1", 2.0, "http://127.0.0.1:8766")

            capture.assert_called_once_with("http://127.0.0.1:8766", 2.0, "right")


class ExecuteOrderTests(unittest.TestCase):
    def run_flow(self, reverse=False, round_trip=False, arm="left"):
        flow = {
            "arm": arm,
            "speed": 0.8,
            "acceleration": 0.8,
            "keyframes": [
                {"name": "stage_1", "label": "上场", "joints": [9.0] * 7},
                {"name": "approach", "label": "接近位", "joints": [1.0] * 7},
                {"name": "target", "label": "指向位", "joints": [2.0] * 7},
                {"name": "retreat", "label": "撤离位", "joints": [3.0] * 7},
                {"name": "stage_1_return", "label": "退场", "joints": [9.0] * 7},
            ],
        }
        moved = []
        commanded_arms = []
        hand_commands = []
        sleeps = []

        def fake_bridge_request(_base_url, path, body=None, timeout=45.0):
            del timeout
            if path == "/health":
                return {"ok": True}
            if path == "/hand":
                hand_commands.append((body["hand"], body["positions"]))
                return {"ok": True}
            commanded_arms.append(body["arm"])
            moved.append(body["joints"][0])
            return {"ok": True}

        with patch.object(flow_runner, "bridge_request", side_effect=fake_bridge_request), \
                patch.object(flow_runner.time, "sleep", side_effect=sleeps.append):
            flow_runner.execute(
                flow,
                "http://127.0.0.1:8766",
                0.1,
                2.0,
                reverse=reverse,
                round_trip=round_trip,
            )
        return moved, sleeps, commanded_arms, hand_commands

    def test_execute_plays_only_three_taught_frames_forward(self):
        moved, _, _, _ = self.run_flow()
        self.assertEqual([1.0, 2.0, 3.0], moved)

    def test_return_plays_three_taught_frames_in_reverse(self):
        moved, _, _, hand_commands = self.run_flow(reverse=True)
        self.assertEqual([3.0, 2.0, 1.0], moved)
        self.assertEqual([], hand_commands)

    def test_round_trip_plays_forward_waits_five_seconds_then_reverses(self):
        moved, sleeps, _, hand_commands = self.run_flow(round_trip=True)
        self.assertEqual([1.0, 2.0, 3.0, 3.0, 2.0, 1.0], moved)
        self.assertEqual(5.0, sleeps[3])
        self.assertEqual([
            ("left", flow_runner.POINT_GESTURE),
            ("left", flow_runner.REST_GESTURE),
        ], hand_commands)

    def test_execute_uses_configured_right_arm(self):
        _, _, commanded_arms, hand_commands = self.run_flow(arm="right")
        self.assertEqual(["right", "right", "right"], commanded_arms)
        self.assertEqual([
            ("right", flow_runner.POINT_GESTURE),
            ("right", flow_runner.REST_GESTURE),
        ], hand_commands)


if __name__ == "__main__":
    unittest.main()
