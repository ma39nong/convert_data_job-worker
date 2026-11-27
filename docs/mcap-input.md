# 第一阶段：MCAP 原包输入，保持现有 LeRobot 输出

本阶段适用于 `auto-convert-transfer-ui`。新增任务默认 MCAP；历史任务缺少格式字段时仍按 ROSbag（SQLite3）处理，保留原路径、版本和队列身份。左侧“采集原包格式”可选择 MCAP / ROSbag，需正常停止、保存后重新开始。格式变化使用独立运行版本，不将历史已完成任务当作新格式重跑。

自动路径：原包为 `/data/data-demo/real/<project>/data/{mcap|rosbag}/<robot>/<scene>/{A,B,C,D}`；完整 LeRobot、normal 和 speedup 路径保持原设置。原 ROSbag 目录不迁移、不删除。

## 输入与清洗

选择包含 A/B 的场景父目录或单独 A/B 目录。C/D 会在场景下发现，只做原包归档，不转换、不计入 A+B 封包数。第一阶段录制目录名支持 `episode123`、`ep_001`、`ep-001`、`ep001`，维持现有数字范围批次命名；尚不接收任意无编号 UUID 目录或跨日期/session 的递归混合输入。

MCAP episode 需要 `metadata.yaml`（`storage_identifier: mcap`，文件清单与实际分卷一致），以及完整录制收尾标记。新采集使用 `manifest.status=complete` 和 `episode.json` 的 `episode_uid/start_time_ns/end_time_ns`；仍带旧 `collection_state.json` 的 MCAP 也可兼容。未收尾、缺分卷、CRC/消息数异常时保留原包。A/B 校验 ROS2 profile、嵌入 ROS2 CDR schema、Zstd Chunk CRC、Summary CRC、索引和实际消息数；这不是对录制器版本做完整上线认证。C/D 不读消息内容、CRC或转换质量，只检查来源稳定、录制完成与备份文件/共享资产完整性。

A/B 只读解码 MCAP，建立临时处理视图后复用既有清洗、转换及独立验证。Foxglove `CompressedVideo` 的 H.264 从带头的原始码流顺序解码，先解码再裁剪，避免删除 IDR 后使后续 P 帧失效；`timestamp` 与 `publish_time` 必须一致。整数 RVL 深度按提供的校验包中的解码器无损读取。RGB/深度/触觉的拍摄时间、其他消息接收时间继续遵守原裁剪规则。临时视图不作为原包上传，并在操作结束后删除；解码视图会占用临时磁盘空间，不承诺 MCAP 文件大小就是处理工作区大小。

输出仍是原有 30Hz、有完整数值 state/action 的 LeRobot；Wuji 108/54、Sharpa 116/58、双臂静止 100ms/1e-4rad、严格新鲜度与质量阈值不变。normal/speedup 的算法和 native training recipe 未修改。

## 新话题与设备映射

采集规范改变了话题和消息布局，不能只改文件后缀。兼容原有话题的 MCAP 可直接处理。标准设备话题需在 episode 内 `processing.yaml`，或其 `assets.config_ref` 指向的 `configs/processing.yaml` 中明确映射，例如：

```yaml
topic_map:
  /devices/fr3_left/state/joints: /left/franka/joint_states
  /devices/fr3_right/state/joints: /right/franka/joint_states
  /devices/sharpa_left/state/joints: /sharpa/left/joint_states
  /devices/sharpa_right/state/joints: /sharpa/right/joint_states
  /devices/fr3_left/action/command: /teleop/validated_arm_commands
  /devices/fr3_right/action/command: /teleop/validated_arm_commands
  /devices/sharpa_left/action/command: /sharpa/left/command
  /devices/sharpa_right/action/command: /sharpa/right/command
  /devices/rig/cameras/head/depth/image_raw/compressedDepth: /cam0/depth/image_raw
  /recording/arm_command_status: /teleop/arm_command_status
# 仅在名称确实等价时显式声明；绝不按数组下标猜左右或关节顺序。
joint_name_map: {}
```

这是配置示例，不是已经确认的新采集端实际 topic。其他必需触觉通道按同样方式映射到现有机型 contract；所有测量关节的 position/velocity 仍需真实提供。`assets.devices_ref` 指向的设备配置如已声明 RGB 的 `head/wrist_left/wrist_right` 角色，可以分别映射到 cam0/cam2/cam1；未明确声明时须手动映射。新规范的其他扩展模态保留在 MCAP 原包，本阶段只发布现有 LeRobot 支持的必需字段；32FC1 浮点深度不静默量化为uint16，留待新版字段协议适配。

现有转换依赖真实的控制接管、命令接受与故障状态，不能从“有动作消息”推断有效或将缺失状态全部填 true。支持采集端使用标准 `std_msgs/msg/String` 记录完整控制状态 JSON，再通过 `topic_map` 映射至原状态通道；JSON 字段须完整对应仓库 `schemas/ArmCommandStatus.msg` / `HandTelemetryStatus.msg`（Header/Time 也是对象，布尔字段必须为布尔值）。这只是转换器内部兼容视图，MCAP 仍无需自定义 ROS 消息。如果新采集端不提供这些信息，应先设计新的状态有效性适配并用真实样本确认，不能绕过检查。缺少映射、测量维数或状态证据时，任务提示错误并保留原包。

## 共享资产归档与删除

按 `episode.json assets` 解析自包含或三级共享引用。仅接收相对路径，拒绝软链接和越过共享/模型根的文件；引用文件有 SHA256 时须一致。外置 `_shared` 整体与显式模型目录先按原相对关系上传，整目录远端清单/SHA256 和凭据确认后再上传该 episode。远端引用必须落在配置的 MCAP 根内；已有同名不同内容的共享目录报冲突保留，不覆盖。不同 session 使用不同目录/版本，不能把不同标定静默复用到同名 `_shared`。

成功删除本机单条原包前，必须确认 episode 和外置共享资产的远端完整备份。恢复删除时也重新核验共享资产。共享配置/模型本身不随单条 episode 删除。A/B 仍先确保本地转换安全入批；C/D 在原包和共享资产备份确认后按开关释放。完整 LeRobot 与两训练版本按原有交付完成后清理规则执行。

本阶段外置共享资产上传使用 GUI 上传角色；旧 CLI 挂载上传器遇到该类 MCAP 会保留原包并明确提示使用 GUI，避免只备份 data.mcap 就误删。

## 与附件第二阶段的区别及验收

附件《数据格式要求》及 `lerobot-conformance` 的 `native_indexed_v2` 另行开发：原生频率、异步 state/action nullable、streams/indices、原生视频 remux、最近邻行映射等尚未加入当前训练链路。本阶段的 RGB 仍按现有 30Hz 转换流程编码，不能声称通过新版 remux/原生索引发布规范。`recorder-conformance` 用于录制器上线认证，不作为 C/D 日常质量门禁；用户提供的两个检查包保存在本次 outputs 参考目录，未安装为全局 skill 或接管现有流程。

新增回归使用真实 ROS2 CDR/MCAP/Zstd、标准 String 状态、H264 视频和共享引用，验证源文件不变、完整 LeRobot→normal/speedup 交付、原包删除、CRC/完成标记/缺状态拒绝、共享远端损坏时保留等行为。当前尚无用户真实 MCAP 录制；真实话题、关节、控制状态和共享布局的首批验收仍待采集端给出样本，不能把合成回归当生产录制器认证。

MCAP Python API 依据：[官方 ROS2 读写说明](https://mcap.dev/docs/python/ros2_noenv_example)、[Reader API](https://mcap.dev/docs/python/mcap-apidoc/mcap.reader)。
