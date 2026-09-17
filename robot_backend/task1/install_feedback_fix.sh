#!/usr/bin/env bash
# Build the local LBot driver fix. Does not stop a driver or send commands.
set -eo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LBOT_WS_PATH="${FIVEFINGER_LBOT_WS:-$HOME/lbot_ws}"
cd "$LBOT_WS_PATH"
SOURCE=src/lbot_driver/src/lbot_driver.cpp
echo '[1/3] Checking feedback patch'
if ! grep -q 'Qihuang feedback v1' "$SOURCE"; then
    git apply --recount --unidiff-zero --check "$ROOT/task1/feedback_driver.patch"
    cp -p "$SOURCE" "$SOURCE.before-qihuang-feedback-$(date +%Y%m%d-%H%M%S)"
    git apply --recount --unidiff-zero "$ROOT/task1/feedback_driver.patch"
fi
echo '[2/3] Building lbot_driver (existing interfaces and SDK preserved)'
source /opt/ros/jazzy/setup.bash
source "$LBOT_WS_PATH/install/setup.bash"
colcon build --packages-select lbot_driver --executor sequential --event-handlers console_direct+
echo '[3/3] Feedback driver built successfully; activate during controlled restart'
