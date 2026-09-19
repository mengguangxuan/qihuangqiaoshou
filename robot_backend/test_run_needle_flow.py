import copy
import unittest
from unittest.mock import patch

import run_needle_flow as needle_runner


def complete_flow():
    stages = []
    for index, stage_id in enumerate(needle_runner.STAGE_IDS, 1):
        steps = [] if stage_id == "wait_5s" else [
            {"id": f"pose-{index}", "type": "pose", "label": f"姿态 {index}", "joints": [float(index)] * 7, "feedback_version": 1}
        ]
        if stage_id == "take_large":
            steps.append({"id": "pinch", "type": "pinch"})
        if stage_id == "insert_small":
            steps.append({"id": "release", "type": "release"})
        stage = {"id": stage_id, "label": stage_id, "steps": steps}
        if stage_id == "wait_5s":
            stage["wait_seconds"] = 5.0
        stages.append(stage)
    return {
        "schema": "fivefinger.needle-flow/v1",
        "arm": "left",
        "speed": 1.5,
        "acceleration": 1.0,
        "stages": stages,
    }


class NeedleFlowTests(unittest.TestCase):
    def test_legacy_feedback_cannot_be_executed(self):
        flow = complete_flow()
        del flow["stages"][0]["steps"][0]["feedback_version"]
        ready, missing = needle_runner.readiness(flow)
        self.assertFalse(ready)
        self.assertTrue(any("需覆盖重录" in item for item in missing))

    def test_empty_non_wait_stage_is_not_ready(self):
        flow = complete_flow()
        flow["stages"][0]["steps"] = []
        ready, missing = needle_runner.readiness(flow)
        self.assertFalse(ready)
        self.assertIn("take_large没有步骤", missing)

    def test_unrecorded_pose_is_not_ready(self):
        flow = complete_flow()
        flow["stages"][1]["steps"][0]["joints"] = None
        ready, missing = needle_runner.readiness(flow)
        self.assertFalse(ready)
        self.assertTrue(any("未记录" in item for item in missing))

    def test_execute_keeps_stage_order_and_fixed_hand_values(self):
        flow = copy.deepcopy(complete_flow())
        commands = []
        sleeps = []
        events = []

        def fake_request(_base, path, body=None, timeout=45.0):
            del timeout
            if path == "/health":
                return {"ok": True}
            commands.append((path, body))
            events.append((path, body))
            return {"ok": True}

        def fake_sleep(seconds):
            sleeps.append(seconds)
            events.append(("sleep", seconds))

        with patch.object(needle_runner, "bridge_request", side_effect=fake_request), \
                patch.object(needle_runner.time, "sleep", side_effect=fake_sleep):
            needle_runner.execute(flow, "http://127.0.0.1:8766")

        move_values = [body["joints"][0] for path, body in commands if path == "/move_joint"]
        self.assertEqual([1.0, 2.0, 3.0, 4.0], move_values)
        hand_values = [body["positions"] for path, body in commands if path == "/hand"]
        self.assertEqual([needle_runner.GESTURES["pinch"], needle_runner.GESTURES["release"]], hand_values)
        self.assertEqual([0, 96, 0, 255, 255, 255], hand_values[0])
        self.assertEqual([255, 71, 255, 255, 255, 255], hand_values[1])
        for index, (kind, _) in enumerate(events):
            if kind == "/hand":
                self.assertEqual(("sleep", 1.5), events[index - 1])
                self.assertEqual(("sleep", 1.5), events[index + 1])
        first_move = next(body for path, body in commands if path == "/move_joint")
        self.assertEqual(1.5, first_move["speed"])
        self.assertEqual(1.0, first_move["acceleration"])
        self.assertIn(5.0, sleeps)


if __name__ == "__main__":
    unittest.main()
