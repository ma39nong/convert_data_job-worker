# ROS2 bag 静止段自动裁剪

根据双臂实测关节状态识别录制过程中的无效等待，统一裁剪所有话题并压缩时间轴。当前采用 episode36 已确认效果的算法，适用于 ROS2 SQLite3 bag。

识别到的内部静止区间，无论长短，都直接删除；不检查手部变化，不做任务阶段识别。RGB、depth、触觉、双臂、双手及控制命令同步裁剪。

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

参考工程 `/home/user/data_clean_UI`、`/home/user/convert_data` 未修改，调研见 [docs/architecture.md](docs/architecture.md)。job 调度与 LeRobot v2.1 转换尚未实现，属于后续阶段。
