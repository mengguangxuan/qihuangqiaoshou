import copy
import unittest
from unittest.mock import patch

import run_needle_flow as needle_runner


def complete_flow():
    stages = []
    for index, stage_id in enumerate(needle_runner.STAGE_IDS, 1):
        steps = [] if stage_id == "wait_5s" else [
            {"id": f"pose-{index}", "type": "pose", "label": f"姿态 {index}", "joints": [float(index)] * 7}
        ]
        if stage_id == "take_large":
            steps.append({"id": "pinch", "type": "pinch"})
        if stage_id == "retrieve_small":
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

        def fake_request(_base, path, body=None, timeout=45.0):
            del timeout
            if path == "/health":
                return {"ok": True}
            commands.append((path, body))
            return {"ok": True}

        with patch.object(needle_runner, "bridge_request", side_effect=fake_request), \
                patch.object(needle_runner.time, "sleep", side_effect=sleeps.append):
            needle_runner.execute(flow, "http://127.0.0.1:8766")

        move_values = [body["joints"][0] for path, body in commands if path == "/move_joint"]
        self.assertEqual([1.0, 2.0, 3.0, 4.0, 6.0, 7.0], move_values)
        hand_values = [body["positions"] for path, body in commands if path == "/hand"]
        self.assertEqual([needle_runner.GESTURES["pinch"], needle_runner.GESTURES["release"]], hand_values)
        first_move = next(body for path, body in commands if path == "/move_joint")
        self.assertEqual(1.5, first_move["speed"])
        self.assertEqual(1.0, first_move["acceleration"])
        self.assertIn(5.0, sleeps)


if __name__ == "__main__":
    unittest.main()
