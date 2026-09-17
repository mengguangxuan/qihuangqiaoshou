#!/usr/bin/env bash
set -eo pipefail

# ROS2's generated setup files reference optional variables that may not be
# defined yet.  Keep nounset disabled while sourcing ROS2 and the workspace.
set +u

# Portable WSL environment loader.  Set FIVEFINGER_ORBBEC_WS before sourcing
# this file if the teammate keeps the Orbbec ROS2 workspace elsewhere.
source /opt/ros/jazzy/setup.bash

# All camera, bridge and robot-driver nodes run inside this WSL instance.
# LOCALHOST avoids DDS multicast discovery failures in WSL mirrored networking.
export ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST

ORBBEC_WS_PATH="${FIVEFINGER_ORBBEC_WS:-$HOME/orbbec_ws}"
if [[ ! -f "$ORBBEC_WS_PATH/install/setup.bash" ]]; then
    echo "找不到 Orbbec ROS2 工作空间：$ORBBEC_WS_PATH" >&2
    echo "请先安装/编译 Orbbec ROS2 驱动，或设置 FIVEFINGER_ORBBEC_WS 为实际路径。" >&2
    return 1 2>/dev/null || exit 1
fi
source "$ORBBEC_WS_PATH/install/setup.bash"

export FIVEFINGER_PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export FIVEFINGER_ORBBEC_WS="$ORBBEC_WS_PATH"
export GEMINI2_BRIDGE_PORT="${GEMINI2_BRIDGE_PORT:-8767}"

echo "ROS2 Jazzy + Orbbec 环境已加载"
echo "项目目录：$FIVEFINGER_PROJECT_DIR"
echo "Orbbec 工作空间：$FIVEFINGER_ORBBEC_WS"
