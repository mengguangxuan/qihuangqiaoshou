import unittest
from unittest.mock import patch

import run_task_flow as runner


def complete_flow():
    return {
        "schema": "fivefinger.task-flow/v1",
        "id": "tap",
        "name": "测试流程",
        "speed": 1.5,
        "acceleration": 1.0,
        "stages": [
            {"id": "prepare", "label": "准备", "steps": [
                {"id": "a", "type": "pose", "arm": "left", "label": "左姿态",
                 "joints": [1.0] * 7, "feedback_version": 1},
                {"id": "b", "type": "pinch", "arm": "right", "label": "右手捏"},
                {"id": "b2", "type": "fist_ready", "arm": "left", "label": "左手握拳准备"},
                {"id": "b3", "type": "fist", "arm": "right", "label": "右手握拳"},
            ]},
            {"id": "return", "label": "返回", "steps": [
                {"id": "c", "type": "pose", "arm": "right", "label": "右姿态",
                 "joints": [2.0] * 7, "feedback_version": 1},
            ]},
        ],
    }


class TaskFlowTests(unittest.TestCase):
    def test_cupping_configuration_has_only_take_stage(self):
        flow = runner.load_flow("cupping")
        self.assertEqual(["take"], [stage["id"] for stage in flow["stages"]])
        ready, missing = runner.readiness(flow)
        self.assertTrue(ready, missing)

    def test_readiness_requires_arm_and_recorded_pose(self):
        flow = complete_flow()
        flow["stages"][0]["steps"][0]["joints"] = None
        ready, missing = runner.readiness(flow)
        self.assertFalse(ready)
        self.assertTrue(any("未记录" in item for item in missing))

    def test_execute_routes_each_step_to_its_recorded_arm(self):
        commands = []
        sleeps = []

        def fake_request(_base, path, body=None, timeout=45.0):
            del timeout
            commands.append((path, body))
            return {"ok": True}

        with patch.object(runner, "bridge_request", side_effect=fake_request), \
                patch.object(runner.time, "sleep", side_effect=sleeps.append):
            runner.execute(complete_flow(), "http://127.0.0.1:8766")

        moves = [body for path, body in commands if path == "/move_joint"]
        hands = [body for path, body in commands if path == "/hand"]
        self.assertEqual(["left", "right"], [item["arm"] for item in moves])
        self.assertEqual("right", hands[0]["hand"])
        self.assertEqual(runner.GESTURES["pinch"], hands[0]["positions"])
        self.assertEqual("left", hands[1]["hand"])
        self.assertEqual([255, 255, 0, 0, 0, 0], hands[1]["positions"])
        self.assertEqual("right", hands[2]["hand"])
        self.assertEqual([0, 0, 0, 0, 0, 0], hands[2]["positions"])
        self.assertEqual(6, sleeps.count(runner.GESTURE_PAUSE_SECONDS))

    def test_cupping_grab_uses_exact_values_for_both_hands(self):
        flow = {
            "schema": "fivefinger.task-flow/v1", "id": "cupping", "name": "罐法",
            "stages": [{"id": "take", "label": "抓取", "steps": [
                {"id": "left", "type": "grab", "arm": "left"},
                {"id": "right", "type": "grab", "arm": "right"},
            ]}],
        }
        commands = []
        with patch.object(runner, "bridge_request", side_effect=lambda _base, path, body=None, timeout=45.0: commands.append((path, body)) or {"ok": True}), \
                patch.object(runner.time, "sleep"):
            runner.execute(flow, "http://127.0.0.1:8766")
        hands = [body for path, body in commands if path == "/hand"]
        self.assertEqual(["left", "right"], [item["hand"] for item in hands])
        self.assertTrue(all(item["positions"] == [155, 0, 119, 119, 112, 157] for item in hands))

    def test_massage_preserves_all_stages_and_only_holds_massage_three_seconds(self):
        flow = {
            "schema": "fivefinger.task-flow/v1", "id": "massage", "name": "筋膜枪按摩",
            "stages": [
                {"id": "take", "label": "拿取筋膜枪", "steps": [
                    {"id": "grab", "type": "massage_grab", "arm": "right"},
                ]},
                {"id": "start", "label": "开启筋膜枪", "steps": [
                    {"id": "on", "type": "massage_on", "arm": "left"},
                ]},
                {"id": "massage", "label": "按摩", "steps": [
                    {"id": "pose", "type": "pose", "arm": "right", "joints": [0.2] * 7,
                     "feedback_version": 1},
                ]},
                {"id": "stop", "label": "关闭筋膜枪", "steps": [
                    {"id": "stop-pose", "type": "pose", "arm": "left", "joints": [0.3] * 7,
                     "feedback_version": 1},
                ]},
            ],
        }
        commands = []
        with patch.object(runner, "bridge_request", side_effect=lambda _base, path, body=None, timeout=45.0: commands.append((path, body)) or {"ok": True}), \
                patch.object(runner.time, "sleep") as sleep:
            runner.execute(flow, "http://127.0.0.1:8766")

        hands = [body for path, body in commands if path == "/hand"]
        self.assertEqual([runner.GESTURES["massage_grab"], runner.GESTURES["massage_on"]],
                         [body["positions"] for body in hands])
        moves = [body for path, body in commands if path == "/move_joint"]
        self.assertEqual(2, len(moves))
        self.assertEqual([0.2] * 7, moves[0]["joints"])
        self.assertEqual([0.3] * 7, moves[1]["joints"])
        self.assertEqual([1.5, 1.5, 1.5, 1.5, 3.0, 0.0],
                         [call.args[0] for call in sleep.call_args_list])

    def test_massage_configuration_has_four_requested_stages(self):
        flow = runner.load_flow("massage")
        self.assertEqual(
            ["take", "start", "massage", "stop"],
            [stage["id"] for stage in flow["stages"]],
        )


if __name__ == "__main__":
    unittest.main()
