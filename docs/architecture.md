# 参考工程调研与阶段边界

## data_clean_UI

参考 `/home/user/data_clean_UI` 的 `app.py`、`bag.py`、`timestamps.py`、`lerobot_data.py`、前端文件及测试组织。现有链路是人工预览/时间段选择/阶段标注 → 统一计划 → 全话题同步删除 → SQLite 分卷重写 → 压缩记录时间与标准 Time → 更新元数据/来源报告。另有 LeRobot 已有 episode 的人工裁剪，和自动识别原始 bag 静止段是两个入口。

本工程以其 `bag.py` 和 `timestamps.py` 为基础独立复制维护，保留分卷、类型感知时间映射及报告兼容协议；去除图像预览依赖和 UI 入口，加入自动双臂状态配对、内部静止段识别、CLI、批量执行。删除区间使用 SQLite 临时表中的有序索引查找，替代手工工程的长 SQL 条件，解除 500 段和 SQLite 表达式深度限制。未复制 UI、LeRobot 编辑功能或历史 exports。

## convert_data

参考 `/home/user/convert_data`，当前本地 `main`，远端已有 `origin/sharpa`；本轮只读查看，没有切换、合并或修改参考仓库。

main 的分层：CLI/config/selection → source（ROS2 读取、采集状态、关节归一化）→ recipe（时间窗口、动作/状态对齐、连续性检查）→ writer（LeRobot v2.1、ACT HDF5、DP3）→ pipeline（暂存、提交、恢复与追加）。

- Wuji 合约：双 FR3 7+7、双手 20+20。现有 Pi05 state 108、action 54；RGB 三路，30 Hz。合约中声明 cam0 depth，当前 LeRobot v2.1 writer 的 RGB 路径不能据此视为已经输出 depth；DP3 有独立深度读取路径。
- `origin/sharpa`：双手各 22 关节，state 116、action 58；有独立 source/recipe/schema，并有 `sharpa_pi05_depth.yaml`，使用 uint16 Parquet 深度数组和附加 FFV1 MKV。另有 58 维专用质量/对齐与 GUI，不能直接把整个分支覆盖 main，否则会混入或丢失其他分支的规则。
- `docs/sharpa_pi05.md` 明确原普通 Sharpa Pi05 格式不导出触觉；深度路径不等于已支持触觉导出。第二阶段需要单独检查触觉各话题的数据类型、频率、shape、单位及缺帧规则。
- main source 识别 `trim_report.json` + `source_collection_state.json` 并映射采集窗口，当前输出保留这些协议。第二阶段仍需检查所选 Sharpa 分支和转换模式是否按相同策略消费。

## 当前阶段提供的交接物

`trim_bag(source, output, config)` 返回报告，并生成可由 ROS2 reader 打开的新 bag；`inspect_bag` 可先分析。配置与识别报告独立于机器人手型号，全话题同步裁剪，因此无需为 Wuji/Sharpa 删除或重新编码 RGB/depth/触觉。

第二阶段现已在新工程独立移植参考后端，并实现本地 job、Wuji RGB+depth 和 Sharpa RGB+depth+触觉写出。具体命令与时间/质量策略见 README 的第二阶段章节。外部平台任务领取、远程队列和常驻监控尚未接入。


## 当前裁剪实现

CLI/API 使用 episode36 已认可的滑动窗口检测：直接删除检测到的内部双臂静止区间，不按时长分级，不读取手部或控制目标，不合并碎片。所有话题统一执行删除与时间映射。独立的短 Hold 补检尚未实现。

预览由 `scripts/preview_batch.py` 生成，逐消息验证由 `scripts/verify_baseline_batch.py` 执行；均不依赖参考工程的运行环境。
