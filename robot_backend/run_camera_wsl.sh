#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/setup_wsl.sh"

# IR is not used by the RGB-D web pipeline.  Disabling it removes one
# 1280x800@30 stream and makes Gemini 2 much more stable over USB/IP.
# Limit both streams to 15 FPS to reduce USB/IP load during depth-frame
# dropouts. Keep resolution, registration and synchronization unchanged.
ros2 run orbbec_camera orbbec_camera_node --ros-args \
    -r __ns:=/camera \
    -r __node:=camera \
    -p enable_color:=true \
    -p enable_depth:=true \
    -p enable_ir:=false \
    -p color_fps:=15 \
    -p depth_fps:=15 \
    -p depth_registration:=true \
    -p enable_point_cloud:=false \
    -p publish_tf:=true \
    -p depth_format:=Y16 \
    -p ir_format:=Y8 \
    -p color_format:=MJPG \
    -p enable_frame_sync:=true \
    -p uvc_backend:=libuvc
