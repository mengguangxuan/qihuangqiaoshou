#!/usr/bin/env bash
set -eo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPONENT="${1:-}"

case "$COMPONENT" in
  camera)
    exec bash "$ROOT/run_camera_wsl.sh"
    ;;
  camera_bridge)
    exec bash "$ROOT/run_bridge_wsl.sh"
    ;;
  lbot_driver)
    source /opt/ros/jazzy/setup.bash
    source "${FIVEFINGER_LBOT_WS:-$HOME/lbot_ws}/install/setup.bash"
    export ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
    exec ros2 launch lbot_driver lbot_start_driver.launch.py
    ;;
  action_bridge)
    exec bash "$ROOT/task1/run_robot_bridge_wsl.sh"
    ;;
  *)
    echo "unknown component: $COMPONENT" >&2
    exit 2
    ;;
esac
