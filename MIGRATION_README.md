# 岐黄巧手完整平台迁移说明

本目录现在同时包含网页、机器人后端、Gemini 2 相机启动脚本、8766 动作桥、五个寻穴流程、无针点穴七阶段流程和统一启动入口。不要再单独复制 `dist/`，否则只能得到没有机器人后端的静态网页。

## 目录内容

- `dist/`：平台网页与图片资源。
- `robot_backend/`：ROS2 图像处理、AprilTag 定位、关键帧记录与执行、8766 动作桥和相机启动脚本。
- `robot_backend/acupoint_flows/`：五个穴位各自的三帧示教数据及上场/返回数据。
- `robot_backend/needle_flow.json`：无针点穴七阶段的姿态与固定手势编排数据。
- `robot_backend/run_needle_flow.py`：固定使用左臂执行无针点穴七阶段流程。
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

首次安装本版时，在 WSL 中进入本目录，先安装反馈驱动补丁：

```bash
bash ./robot_backend/task1/install_feedback_fix.sh
```

脚本备份并修补现有 `lbot_ws` 的驱动源码，只编译 `lbot_driver`，保留原 SDK 和接口。编译完成后，在无动作运行时重启 LBot 驱动，再启动平台。补丁使反馈时间戳只在控制器原始数据推进时更新；超过 0.5 秒没有新反馈时停止发布缓存数据，录制将提示反馈过期。勿仅更新网页/动作桥而遗漏此驱动补丁。

在 Windows PowerShell 中进入本目录，运行：

```powershell
.\start_qihuang_platform.ps1
```

然后只打开：

```text
http://127.0.0.1:8008/
```

这个地址由机器人后端直接提供网页、视频流、状态接口、关键帧记录和动作控制，不再需要额外启动静态网页服务器。

启动过程会重新创建唯一的 8766 动作桥，但不会发送使能、掉使能或运动命令；机械臂保持启动前的物理状态，页面恢复上一次左右臂控制记录。

使能按钮使用 `/robot1/{left,right}_arm/set_enable`；紧急停止单独使用 `set_emergency_stop`。旧版误用急停接口产生的“使能成功”日志不作为真实使能记录恢复，没有新记录时显示“使能状态未确认”。

停止平台：

```powershell
.\stop_qihuang_platform.ps1
```

## 更换端口或 WSL 名称

```powershell
.\start_qihuang_platform.ps1 -WslDistro Ubuntu-24.04 -Port 8008 -SpeedScale 1.0
```

五个穴位流程的运动参数为 `speed=1.5`、`acceleration=1.0`；速度缩放必须在 `(0, 1]`，默认值为 `1.0`。

## 无针点穴七阶段

“无针点穴”栏目固定使用左臂，阶段顺序为：拿大针、刺大针、拿小针、刺小针、等待 5 秒、拿回大针、拿回小针。每个阶段可在网页中追加任意数量的左臂姿态关键帧，并插入两种固定左手动作：

- 捏：`0 71 0 255 255 255`
- 放：`255 71 255 255 255 255`

阶段内部步骤可上下排序或删除；所有非等待阶段至少需要一个步骤，且姿态必须完成记录后，一键执行按钮才会开放。
