# ROS2 bag 清洗与 LeRobot v2.1 转换

根据双臂实测关节状态识别录制过程中的无效等待，统一裁剪所有话题并压缩时间轴。当前采用 episode36 已确认效果的算法，适用于 ROS2 SQLite3 bag。

裁剪阶段识别到的内部静止区间，无论长短，都直接删除；不检查手部变化，不做任务阶段识别。RGB、depth、触觉、双臂、双手及控制命令同步裁剪。

## 安装与使用

需要 Python 3.11+；生成预览还需要 PATH 中有 `ffmpeg`、`ffprobe`。

```bash
cd /home/user/convert_data_job-worker
python3 -m venv .venv
.venv/bin/pip install -e '.[test,preview]'

# 只分析，不导出 bag
.venv/bin/bag-worker inspect /path/to/raw_bag

# 裁剪单包
.venv/bin/bag-worker trim /path/to/raw_bag --output outputs/new_bag

# 批量裁剪
.venv/bin/bag-worker batch /path/to/raw_bags --output outputs/new_batch

# 生成三路 RGB 预览
.venv/bin/python scripts/preview_batch.py outputs/new_batch
```

默认参数见 [configs/idle.yaml](configs/idle.yaml)，可通过 `--config configs/idle.yaml` 显式指定。`inspect --report /path/to/new.json` 可保存检测报告。

输入以只读方式打开。输出目录必须不存在，也不能位于输入目录内部；已有输出不会覆盖。批处理逐包执行，失败记录在 `batch_report.json` 中。返回码 0 表示成功，2 表示输入错误或批次中存在失败。

Python 接口：

```python
from bag_worker.idle import trim_bag

report = trim_bag('/path/to/raw_bag', '/path/to/new_bag')
```

## 检测规则

所有包使用同一套参数：

- 使用左右臂实测 `JointState`，各 7 个关节，按关节名称对齐。
- 以较慢状态流的原生记录时间取样，因果匹配另一臂最近的过去状态；允许的最大年龄为较慢流中位周期的 1.5 倍。当前样本约为 200 Hz。
- 在每帧前后合计 100 ms 的完整窗口内，每个关节的最大值与最小值之差不超过 `0.0001 rad`，判为静止。
- 沿用原算法的 `i-start > 3` 计数条件，计数包含进入边界帧；删除范围为 `[times[start+1], times[i-1])`。
- 只删除前后都观察到运动的内部区间。开头、结尾、缺帧、非有限值及不完整窗口处保留；超过中位周期 3 倍的状态断流会中断检测。

没有 2 秒门槛，不按时长分类，不做手部或控制目标筛选，不合并相邻区间。已经检出的短 Hold 会删除，但不保证所有短 Hold 都被发现；额外的独立补检尚未实现。

## 输出与时间处理

每个输出包含 `.db3`、`metadata.yaml`、`trim_report.json`，来源有采集状态时另存 `source_collection_state.json`。

所有话题使用相同的删除区间和时间映射。保留消息的 bag 记录时间及已知结构中的标准 ROS Time 扣除已删除时长；零时间、Duration、整数设备计数器及非时间数据保持原样。

未知自定义消息的消息体保持原样，仅调整 bag 记录时间，详情记录在 `unmodified_payload_types`。可通过 `--message-types` 提供对应 `.msg/.idl` 定义，也可使用输入 bag 的 `message_types/` 目录。

本工具不校准设备时钟，也不执行 30 Hz 重采样。预览的 30 fps 仅用于观看。已裁剪的输入会被拒绝再次处理；导出使用 `.partial` 临时目录，成功后提交。

支持 SQLite3 分卷、未使用 bag 层压缩的 ROS2 bag；时间字节改写支持 CDR1。暂不支持 ROS1、MCAP 和压缩 bag。

## 当前样本结果

已生成的 bag、预览及验证报告位于 `outputs/baseline36_restored_20261016/`。视频位于其 `previews/` 目录，底部显示输出时间和对应原始时间。

| 包 | 删除区间数 | 删除时长（秒） |
| --- | ---: | ---: |
| episode34 | 3 | 5.666 |
| episode35 | 6 | 5.806 |
| episode36 | 2 | 4.936 |
| episode41 | 7 | 5.623 |
| episode54 | 10 | 19.445 |
| episode55 | 16 | 18.179 |
| 合计 | 44 | 59.654 |

episode36 删除 `[4.261966570, 4.276901890)` 和 `[12.446107999, 17.366930256)` 秒，导出的 45,066 条消息与已认可版本逐条一致。原预览保留在 `outputs/previews/episode36_corrected.mp4`，回归依据保留在 `docs/smoke_trim_report.json`。

## 验证

```bash
# 单元测试与可选的真实样本回归
BAG_WORKER_REAL_ROOT=/home/user/franka_teleop_data/bags/gello_sharpa_tactile \
BAG_WORKER_REAL_EPISODE36=/home/user/franka_teleop_data/bags/gello_sharpa_tactile/episode36 \
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q

# 验证批次输出：重新检测区间并逐消息核对数据、时间与元数据
.venv/bin/python scripts/verify_baseline_batch.py outputs/new_batch
```

未配置环境变量时跳过真实样本测试。逐消息验证脚本面向当前单分卷样本，结果写入批次目录的 `baseline_verification.json`。数据一致性检查不能替代视频效果确认。

## 目录与后续范围

- `src/bag_worker/`：bag 读写、时间映射、静止检测和 CLI。
- `configs/idle.yaml`：统一检测参数。
- `scripts/`：预览生成和输出验证。
- `tests/`：裁剪、时间映射及检测回归测试。
- `docs/`：参考工程调研与基线验证依据。
- `outputs/`：生成的 bag、视频和报告，不纳入 Git。

参考工程 `/home/user/data_clean_UI`、`/home/user/convert_data` 未修改，调研见 [docs/architecture.md](docs/architecture.md)。本地 job 与 LeRobot v2.1 转换见下文；外部平台和远程队列尚未接入。

## 裁剪后转换为 LeRobot v2.1

第二阶段提供可恢复的本地 job：输入就绪检查 → 自动裁剪 → 30 Hz 对齐 → LeRobot 写出 → 全量校验。Wuji 与 Sharpa 使用独立配置和数据集。

| 配置 | 原始输入 | 输出特征 |
| --- | --- | --- |
| `configs/wuji.yaml` | `/home/user/franka_teleop_data/bags/gello_wuji` | 108 维 state、54 维绝对 action、三路 RGB、原始 uint16 深度、实测接管状态 |
| `configs/sharpa.yaml` | `/home/user/franka_teleop_data/bags/gello_sharpa_tactile` | 116 维 state、58 维绝对 action、三路 RGB、原始 uint16 深度、十路 mono8 触觉形变图、十路 float64 六维力/力矩、实测双臂接管状态 |

Sharpa 没有记录手部接管状态，输出不构造该字段；手部必须有新鲜绝对命令。所有关节按名称映射，保留参考契约的顺序和弧度值。

```bash
.venv/bin/pip install -e '.[test,preview,conversion]'

# 默认读取该机器人目录下的全部 episode；首次运行不用 --resume。
.venv/bin/convert-worker run --config configs/wuji.yaml \
  --run-root outputs/jobs/wuji_stage2
.venv/bin/convert-worker run --config configs/sharpa.yaml \
  --run-root outputs/jobs/sharpa_stage2

# 继续未完成任务或追加新包；单包可加 --episode episode995，可重复指定。
.venv/bin/convert-worker run --config configs/wuji.yaml \
  --run-root outputs/jobs/wuji_stage2 --resume

# 独立验证数据结构、完整视频和全部数值模态。
.venv/bin/convert-worker verify outputs/jobs/wuji_stage2/dataset --compare-source
```

每个 job 目录保存 `manifest.json`、`jobs/episode*.json`、`cleaned/`、`dataset/` 和 `summary.json`。原始包需采集完成、分卷齐全、SQLite 和消息计数一致，并通过源快照与 SHA-256 检查；未就绪或失败的包记录原因，不会静默转换。请在传输完成后启动，文件传输过程与本程序之间目前没有完成通知协议。

`--resume` 校验配置及已提交来源，复用已裁剪 bag，跳过已提交转换；全量验证会重新执行。中断后清理本任务拥有的未提交文件，并从日志恢复。对原始包的改动会使恢复失败。当前是本地批处理及文件锁，不包含远程队列、平台领取协议或常驻监控。

已有清洗 bag 可独立转换，不必再次裁剪：

```bash
.venv/bin/convert-worker convert --config configs/wuji.yaml \
  --source-root /path/to/cleaned_wuji_bags \
  --output outputs/lerobot/wuji_new
```

有效范围来自采集验证报告；清洗后的报告在内存中映射到压缩时间轴。状态、动作、RGB、深度、触觉采用过去最近样本，默认最大年龄 150 ms，不做插值。仅 RGB 的内部短缺口允许最多两个 30 Hz 采样点、图像年龄不超过 250 ms 的有限保持。深度仅在已记录的裁剪接缝处、与已接受的头部 RGB 短缺口重合、其他模态和边界均有效时，允许同样的两帧/250 ms 因果保持；每次保留真实图像年龄并单独记录，不改写图像时间戳。触觉、状态和动作不使用宽限，非接缝的深度缺口仍严格检查。选中范围内部仍有缺口时整条包记为质量排除，仅允许裁掉无效首尾，不擅自拼接真实缺失区间。

深度写入 `observation.depths.cam0`，触觉写入 `observation.tactile.{left,right}.{finger}.{deformation,wrench}`，均为 Parquet 原生定长数值数组，按行组流式写出；RGB 单独保存 H264 MP4。原始深度零值不填补，形变图不做有损编码，力/力矩保留 float64。力/力矩统计逐轴计算；深度和形变图使用可广播的全局标量统计，统计方法写入特征说明。单位、深度比例及触觉标定不凭空推断；相机内参记录在每个来源的转换记录中。新增模态可被标准 LeRobot loader 读取，但训练模型仍需配置相应输入，转换不会自动修改模型。

`source_timestamp_ns` 是当前（可能已压缩）的采样时间；`audit.original_timestamp_ns` 还原到原始录制时间，接缝处选择删除区间之后的分支。`audit.sample_timestamp_ns` 与 `audit.receive_timestamp_ns` 按 metadata 中的流名称保存每路被选中消息的采样时间和记录时间。停用状态缺少历史命令、使用实测位置初始化时，命令审计时间以零标明。未知遥操状态的旧 header 依据清洗报告改用压缩后的记录时间，避免混用两条时间轴。

校验包含逐帧数值/传感器数组与清洗 bag 对比、时间与索引检查、视频全量解码，以及裁剪接缝的状态/动作变化报告。接缝报告不反向修改裁剪决定。

使用本机已经安装 LeRobot v2.1 的解释器可进行真实 loader 检查，缓存只写入新工程：

```bash
/home/user/pi05/.venv/bin/python scripts/check_lerobot_loader.py \
  outputs/jobs/wuji_stage2/dataset
```

该检查读取每条 episode 的首、中、尾，核对深度/触觉原生值、50 步动作窗口与末尾 padding，不启动训练。后端移植来源及版本见 `docs/conversion_provenance.json`；源消息定义的许可保存在 `schemas/LICENSE.gello-retarget`。

本机验证结果：Wuji 995、997、1003 共 3 条 episode / 2,763 帧；Sharpa 34、35、36、41、54、55 共 6 条 episode / 7,094 帧。两组分别输出到 `outputs/jobs/wuji_stage2/dataset` 和 `outputs/jobs/sharpa_stage2/dataset`。77 项测试通过，包含两进程提交、文件锁、中断恢复和真实裁剪回归；两组数据通过全量数值/视频校验及标准 LeRobot loader 检查。详细交付记录见 `docs/stage2_validation.json`。
