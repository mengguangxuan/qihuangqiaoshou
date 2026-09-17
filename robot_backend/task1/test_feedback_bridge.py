"""Run in the sourced ROS2 environment; no node, connection, or motion created."""
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from sensor_msgs.msg import JointState
from task1_robot_bridge import LBotBridge


class FeedbackTests(unittest.TestCase):
    def test_republishing_old_sample_does_not_refresh_it(self):
        bridge = SimpleNamespace(state_lock=threading.Lock(), source_stamps={'left': 0},
                                 updated_at={'left': 0}, joints={'left': []})
        message = JointState()
        message.position = [0.1] * 7
        message.header.stamp.sec = int(time.time()) - 10
        LBotBridge._joint(bridge, 'left', message)
        received = bridge.updated_at['left']
        LBotBridge._joint(bridge, 'left', message)
        self.assertEqual(bridge.updated_at['left'], received)
        self.assertGreater(time.time() - received, 9)

    def test_stationary_fresh_sample_is_accepted(self):
        bridge = SimpleNamespace(state_lock=threading.Lock(), source_stamps={'left': 0},
                                 updated_at={'left': 0}, joints={'left': []})
        message = JointState()
        message.position = [0.1] * 7
        message.header.stamp.sec = int(time.time()) - 5
        LBotBridge._joint(bridge, 'left', message)
        old_time = bridge.updated_at['left']
        message.header.stamp.sec += 5
        LBotBridge._joint(bridge, 'left', message)
        self.assertGreater(bridge.updated_at['left'], old_time)
        self.assertEqual(bridge.joints['left'], [0.1] * 7)

    def test_enable_targets_only_real_enable_service(self):
        call = Mock()
        bridge = SimpleNamespace(_arm=LBotBridge._arm, _call=call,
                                 state_lock=threading.Lock(), enable_state={},
                                 service_clients={('left', 'enable'): 'left/set_enable',
                                                  ('right', 'enable'): 'right/set_enable'})
        for arm in ('left', 'right'):
            for enabled in (True, False):
                result = LBotBridge.set_enable(bridge, {'arm': arm, 'enable': enabled})
                self.assertTrue(result['ok'])
                client, request = call.call_args.args
                self.assertEqual(client, arm + '/set_enable')
                self.assertEqual(request.enable, enabled)
        self.assertEqual(call.call_count, 4)


if __name__ == '__main__':
    unittest.main()
