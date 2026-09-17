#!/usr/bin/env bash
set -eo pipefail
source /opt/ros/jazzy/setup.bash
LBOT_WS_PATH="${FIVEFINGER_LBOT_WS:-$HOME/lbot_ws}"
if [[ ! -f "$LBOT_WS_PATH/install/setup.bash" ]]; then
    echo "找不到 LBot ROS2 工作空间：$LBOT_WS_PATH" >&2
    echo "请设置 FIVEFINGER_LBOT_WS 为实际路径。" >&2
    exit 1
fi
source "$LBOT_WS_PATH/install/setup.bash"
export ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/task1_robot_bridge.py" --port 8766
