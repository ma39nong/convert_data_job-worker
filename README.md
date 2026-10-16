# ROS2 bag 清洗与 LeRobot v2.1 转换

面向双 FR3 搭配 Wuji 或 Sharpa 灵巧手的离线数据处理工具。根据双臂实测关节状态自动删除录制中的静止区间，再将清洗后的数据转换为包含 RGB、深度及可选触觉的 LeRobot v2.1 数据集。

```text
原始 ROS2 bag → 输入完整性检查 → 静止段裁剪 → 30 Hz 对齐 → LeRobot v2.1 → 输出校验
```

原始数据以只读方式访问。裁剪、转换和校验均可独立运行，也可通过可恢复的本地 job 串联执行。

## 支持范围

| 机器人 | State / Action 维度 | 输出模态 |
| --- | --- | --- |
| 双 FR3 + Wuji | 108 / 54 | 三路 RGB、头部 uint16 深度、双臂双手状态与动作、接管状态 |
| 双 FR3 + Sharpa | 116 / 58 | 三路 RGB、头部 uint16 深度、双臂双手状态与动作、双臂接管状态、双手十指形变图及六维力/力矩 |

输入为未使用 bag 层压缩的 ROS2 SQLite3 bag，支持分卷；消息时间字段改写支持 CDR1。关节按名称映射，动作保存为绝对关节目标，单位为弧度。消息与关节契约见 [schemas/](schemas/)。

转换需要有效的采集验证状态：原始包提供 `collection_state.json`；本工具清洗后的包提供 `source_collection_state.json` 和 `trim_report.json`。本地 job 要求采集状态为 finalized、验证失败列表为空，且文件完整、消息计数一致。请在采集和传输结束后启动。

暂不支持 ROS1、MCAP、压缩 bag 或任意机器人话题布局。Sharpa 手部未记录接管状态时，使用新鲜的绝对命令，不构造手部接管字段。

## 安装

需要 Python 3.11+。转换不依赖 ROS 安装；生成预览另需 PATH 中可用的 `ffmpeg` 和 `ffprobe`。

```bash
git clone https://github.com/ma39nong/convert_data_job-worker.git
cd convert_data_job-worker
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[conversion,preview]'
```

仅使用 bag 裁剪功能时，可使用 `pip install -e .`。开发测试需额外安装 `test` 依赖。

## 配置

复制对应的机器人配置，再修改输入路径和数据集信息：

```bash
cp configs/wuji.yaml configs/my_wuji.yaml
# Sharpa 使用 configs/sharpa.yaml 作为模板。
```

| 字段 | 用途 |
| --- | --- |
| `source_root` | 对应机器人的原始 bag 根目录 |
| `output_root` | 独立转换时的默认输出目录 |
| `repo_id` | 数据集标识，格式为 `namespace/dataset`；不代表自动上传 |
| `task` | 当前采集任务的语言描述，需按实际任务修改 |
| `contract` / `schema_dir` | 关节、话题契约及自定义消息定义 |

相对路径以项目根目录为基准解析。转换输出必须位于项目内；job 输出必须位于 `outputs/` 下。Wuji 和 Sharpa 应分别配置、分别输出，避免混入同一数据集。

## 完整工作流

```bash
# 检查、裁剪、转换并校验配置目录下的全部 episode。
convert-worker run --config configs/my_wuji.yaml \
  --run-root outputs/jobs/my_dataset

# 中断恢复、重试失败任务，或处理新增的包。
convert-worker run --config configs/my_wuji.yaml \
  --run-root outputs/jobs/my_dataset --resume

# 仅处理指定包；--episode 可重复传入。
convert-worker run --config configs/my_wuji.yaml \
  --run-root outputs/jobs/subset --episode episode1
```

`--workers` 支持 1–4，默认 1，控制同时转换的 bag 数。每个来源独立暂存，按确定顺序提交。文件锁阻止同一任务或数据集被并发写入。

`--resume` 校验配置与来源，复用已完成的裁剪、跳过已提交转换，并重新执行校验。它不会删除历史已提交数据。更换任务、契约或转换参数时应使用新的输出目录。

任务状态和失败原因保存在 `jobs/` 与 `summary.json`。当前提供本地执行，不包含远程任务队列、常驻监控或传输完成通知协议。

## 独立运行

```bash
# 只分析静止区间。
bag-worker inspect /data/bags/episode1 --report outputs/inspection.json

# 裁剪单包或批量裁剪；输出目录必须不存在。
bag-worker trim /data/bags/episode1 --output outputs/cleaned_bag
bag-worker batch /data/bags --output outputs/cleaned_batch

# 为清洗后的 bag 生成三路 RGB 预览。
python scripts/preview_batch.py outputs/cleaned_batch

# 将已有清洗包转换为 LeRobot，不再次裁剪。
convert-worker convert --config configs/my_wuji.yaml \
  --source-root outputs/cleaned_batch --output outputs/lerobot/my_dataset

# 校验索引、时间、数值和完整视频；另与来源 bag 逐帧比对。
convert-worker verify outputs/lerobot/my_dataset --compare-source
```

独立裁剪可通过 `--config configs/idle.yaml` 指定检测参数；串联 job 使用内置默认裁剪参数。裁剪输出不能位于输入目录内，已裁剪的包会被拒绝再次裁剪。转换续跑使用 `--resume`，不能将任意非空目录作为已有数据集接管。

## 裁剪与对齐规则

裁剪根据左右臂各 7 个实测关节判断静止，以较慢状态流的原生时间取样，并匹配另一臂最近的过去状态。默认在合计 100 ms 的完整窗口内，每个关节的变化范围不超过 `0.0001 rad` 时判为静止。候选帧计数条件及边界参数见 [configs/idle.yaml](configs/idle.yaml)。

仅删除前后均观察到运动的内部静止区间，保留录制首尾及缺少可靠证据的位置。手部运动不参与裁剪判定。RGB、深度、触觉、关节和控制消息等全部话题使用共同的删除区间，并同步压缩时间轴。

转换使用采集报告中的有效窗口，映射清洗后的时间，再以 30 Hz 选择每个采样时刻之前最近的有效消息，不进行线性插值：

- 状态、动作、接管状态及各传感器默认要求消息年龄不超过 150 ms。
- RGB 内部短缺口允许最多两个输出采样点的有限保持，图像年龄不超过 250 ms。
- 深度仅在已记录的裁剪接缝处、与已接受的头部 RGB 短缺口重合且其他模态及边界均有效时，使用同样的两帧/250 ms 限制。保留实际图像年龄，不重写图像时间戳。
- 触觉、状态及动作不使用上述宽限。处理后仍有内部无效区间时，整条录制记为质量排除；仅允许裁掉无效首尾。

裁剪阶段不重采样，也不校准设备时钟。已知消息中的标准 ROS Time 与记录时间共同映射；未知自定义类型保留消息体，并在裁剪报告中列出。转换对报告中标记为未改写的遥操状态消息使用压缩后的记录时间，避免重复映射旧 header。

## 输出

```text
outputs/jobs/my_dataset/
├── manifest.json                 # 任务配置与指纹
├── jobs/                         # 各来源状态与完整性记录
├── cleaned/                      # 清洗后的 bag 与裁剪报告
├── dataset/
│   ├── meta/                     # LeRobot v2.1 元数据与统计
│   ├── data/chunk-000/           # 每 episode 的 Parquet
│   ├── videos/chunk-000/         # 三路 RGB MP4
│   └── conversion/               # 来源、时间对齐、提交与验证记录
└── summary.json
```

RGB 使用 H264 有损编码；深度与触觉使用 Parquet 原生定长数值数组，流式分批写出：

| 特征 | 内容 |
| --- | --- |
| `observation.state` / `action` | 关节状态与绝对动作 |
| `observation.images.cam0` / `cam1` / `cam2` | 头部、左腕、右腕 RGB |
| `observation.depths.cam0` | 原始 uint16 深度 |
| `observation.tactile.{side}.{finger}.deformation` | 原始 mono8 形变图，仅 Sharpa |
| `observation.tactile.{side}.{finger}.wrench` | float64 力 xyz、力矩 xyz，仅 Sharpa |
| `source_timestamp_ns` | 当前时间轴上的采样时间 |
| `audit.original_timestamp_ns` | 映射到原始录制时间轴的网格时间 |
| `audit.sample_timestamp_ns` / `audit.receive_timestamp_ns` | 各流所选消息的采样时间与记录时间 |

深度零值不填补，深度比例与触觉标定不推断，相机内参随来源记录保存。接缝处的原始网格时间选择删除区间之后的分支。力/力矩按轴统计，深度及形变图使用全局标量统计，具体方法写入特征说明。

新增深度和触觉特征需要训练端配置对应模型输入；生成数据集不会自动修改模型或启动训练。

## 测试与读取检查

```bash
pip install -e '.[test,conversion,preview]'
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q
```

默认运行合成数据测试；需要本地真实样本的回归测试按环境条件跳过。输出校验不能替代对实际动作和裁剪效果的人工确认。

如已有安装 LeRobot v2.1 的独立环境，可检查标准 loader、传感器值及动作窗口：

```bash
/path/to/lerobot-env/bin/python scripts/check_lerobot_loader.py \
  outputs/jobs/my_dataset/dataset
```

该检查使用 PyAV，缓存写入项目内，不修改训练环境。来源与消息定义许可见 [移植记录](docs/conversion_provenance.json) 和 [消息定义许可](schemas/LICENSE.gello-retarget)。
