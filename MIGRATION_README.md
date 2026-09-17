# 岐黄巧手完整平台迁移说明

本目录现在同时包含网页、机器人后端、Gemini 2 相机启动脚本、8766 动作桥、五个穴位的关键帧数据和统一启动入口。不要再单独复制 `dist/`，否则只能得到没有机器人后端的静态网页。

## 目录内容

- `dist/`：平台网页与图片资源。
- `robot_backend/`：ROS2 图像处理、AprilTag 定位、关键帧记录与执行、8766 动作桥和相机启动脚本。
- `robot_backend/acupoint_flows/`：五个穴位各自的三帧示教数据及上场/返回数据。
- `start_qihuang_platform.ps1`：完整平台的一键启动入口。
- `stop_qihuang_platform.ps1`：停止网页与穴位后端，相机和机器人驱动保持运行。

## 目标电脑前置条件

1. Windows 已安装 WSL2、Ubuntu 24.04 和 `usbipd-win`。
2. WSL 中已安装 ROS2 Jazzy。
3. Orbbec 工作空间默认位于 `~/orbbec_ws`；如果位置不同，设置 `FIVEFINGER_ORBBEC_WS`。
4. LBot 工作空间默认位于 `~/lbot_ws`；如果位置不同，设置 `FIVEFINGER_LBOT_WS`。
5. WSL 的 Python 已安装 `robot_backend/requirements-wsl.txt` 中的依赖。
6. Gemini 2 首次使用时，先在管理员 PowerShell 中执行一次 `usbipd bind --busid <BUSID> --force`。

可先在 WSL 中检查环境：

```bash
bash ./robot_backend/verify_environment_wsl.sh
```

## 启动

在 Windows PowerShell 中进入本目录，运行：

```powershell
.\start_qihuang_platform.ps1
```

然后只打开：

```text
http://127.0.0.1:8008/
```

这个地址由机器人后端直接提供网页、视频流、状态接口、关键帧记录和动作控制，不再需要额外启动静态网页服务器。

启动过程会重新创建唯一的 8766 动作桥，但不会自动使能机器人，也不会发送运动命令。现场确认安全后，必须由操作员在页面点击“机器人使能”。

停止平台：

```powershell
.\stop_qihuang_platform.ps1
```

## 更换端口或 WSL 名称

```powershell
.\start_qihuang_platform.ps1 -WslDistro Ubuntu-24.04 -Port 8008 -SpeedScale 1.0
```

五个穴位流程的运动参数为 `speed=1.5`、`acceleration=1.0`；速度缩放必须在 `(0, 1]`，默认值为 `1.0`。
