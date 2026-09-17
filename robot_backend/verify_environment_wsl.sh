#!/usr/bin/env bash
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source /opt/ros/jazzy/setup.bash

ORBBEC_WS_PATH="${FIVEFINGER_ORBBEC_WS:-$HOME/orbbec_ws}"
LBOT_WS_PATH="${FIVEFINGER_LBOT_WS:-$HOME/lbot_ws}"

test -f "$ORBBEC_WS_PATH/install/setup.bash" || { echo "缺少 Orbbec 工作空间：$ORBBEC_WS_PATH" >&2; exit 1; }
test -f "$LBOT_WS_PATH/install/setup.bash" || { echo "缺少 LBot 工作空间：$LBOT_WS_PATH" >&2; exit 1; }
test -f "$SCRIPT_DIR/../dist/index.html" || { echo "缺少网页入口 dist/index.html" >&2; exit 1; }
test -f "$SCRIPT_DIR/acupoint_flows/GV14.json" || { echo "缺少穴位关键帧数据" >&2; exit 1; }

python3 -c 'import cv2, numpy, PIL, rclpy; assert hasattr(cv2, "aruco")'
echo "岐黄巧手运行环境检查通过"
