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

转换需要有效的采集验证状态：原始包提供 `collection_state.json`；本工具清洗后的包提供 `source_collection_state.json` 和 `trim_report.json`。本地 job 要求采集状态为 finalized、验证失败列表为空，且文件完整、消息计数一致。批处理命令应在采集和传输结束后运行；自动接收模式可以提前启动，持续扫描配置的输入目录，执行稳定观察、完整性和录制质量检查。

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

## 自动接收新上传的数据

接收器只监控配置的 Sharpa/Wuji A/B 输入目录，自动发现 `episode数字` 目录并登记队列，不依赖上传器实现、人工标记或回调。新 episode 出现后，先按默认 10 秒扫描、3 次一致且稳定 60 秒观察，再检查归档状态、metadata 所列分卷、SQLite、消息数和必需数据流；目录出现本身不代表已经完整。`.uploading/` 等不匹配 episode 名称的目录不会被递归扫描，不要求上传方使用这些目录。

首次启动必须显式配置 `initial_history`：服务器模板设为 `all`，接收已有包和未来新包；本地模板仍为 `none`，跳过首次扫描时已有的包，也支持指定历史清单。该范围持久保存，重启恢复队列并补查遗漏。通过体检后执行既定“清洗 rosbag → LeRobot v2.1 → 校验”，原始包只读，源变化时丢弃本次尝试并重试。

四条路由分别输出到 `data-lerobot/{sharpa,wuji}/{A,B}/`。每批从第一个包成功追加起计时，默认 72 小时；批内持续追加，到期后通过全批校验封存，后续成功包进入下一批。每套完整数据集命名为 `起始原始包序号_结束原始包序号_data_lerobot`，例如 `36_68_data_lerobot`；序号范围允许缺号，实际纳入清单和封存状态可用 `ingest-worker status` 查看。活动批名称随范围更新，封存后固定；每批 LeRobot 内部编号从 0 开始，已处理来源跨批去重。没有下一条时仍会自动处理和封存，不生成空批次。

```bash
python -m convest.ingest.cli check-config --config configs/ingest.local.yaml
python -m convest.ingest.cli run --config configs/ingest.local.yaml
```

本地模板仅指向项目 `outputs/`；服务和服务器配置只是模板，未自动部署。接收检查、首次历史范围、异常源变化及数据集版本发布说明见 [自动接收说明](docs/automatic_ingestion.md)。

接收体检每次尝试完整执行一次。服务器模板使用 `copy_input: false`：直接只读原始 bag，清洗结果另存临时目录，省去原始副本复制及其读回校验；原始包不删除、不移动、不改写。全过程保留变化检测和最终源文件哈希，输出来源对比、视频、深度和触觉校验不变。`copy_input: true` 保留副本模式，旧配置未填写时仍默认复制。`status` 展示细分阶段及哈希字节进度；`drain` 可在当前包发布并清理后进入维护，`resume` 恢复原队列。检查复用边界和安全升级见上述说明。

直接读取的自动接收流程将源文件最终哈希统一放在发布前执行，省去 job 内对同一原始 bag 的一次重复整文件读取。job 在输出校验通过后先记录 `awaiting_source_verification`，不声明完成；接收器完整复核所有原始文件内容、快照及契约后才记录完成并发布。独立 job 和复制模式仍保留原有最终哈希；视频、来源数值、发布副本及追加验证不变。

后续服务器路径、依赖、首次启动范围和验收步骤见 [服务器部署准备](docs/server_deployment.md)。

## 转换前体检与清单

可以先检查输入，不生成清洗 bag 或数据集：

```bash
convert-worker inspect --config configs/my_wuji.yaml \
  --report outputs/reports/input_check.json

# 只检查清单中的来源；存在未就绪项时返回码为 2。
convert-worker check-list lists/my_dataset.txt --config configs/my_wuji.yaml \
  --report outputs/reports/list_check.json
```

体检检查采集状态、分卷、SQLite、消息计数、SHA-256、必需话题、自定义消息及命名关节。报告给出每路时间索引去重后的样本数、中位频率、时间间隔中位数/P99/最大值，以及 RGB、深度、触觉图的端点编码与尺寸。体检会读取源文件计算哈希；图像检查覆盖首尾样本，不替代正式转换中的全部选中帧解码和连续性校验。

`--report` 可省略；指定时必须是项目 `outputs/` 下的新文件，不能位于输入目录内，也不覆盖已有报告。原始包尚未传输完成或缺少 metadata 的 episode 目录会列为未就绪，不会静默漏掉。

TXT 清单支持数字、`episode1`、连续范围 `1-3`、空白或中英文逗号分隔以及 `#` 注释。重复、反向范围和缺失来源会报错：

```text
# 手动选择的同一机器人、同一任务数据
1-3
8
```

体检面向原始采集目录；已有清洗包在独立转换时通过保留的采集状态和裁剪报告校验。体检与 job 都支持 `--episode-list`；`--episode` 与 `--episode-list` 互斥。清单只是人工选择，不代表自动判断任务成功或数据质量等级。

## 完整工作流

```bash
# 检查、裁剪、转换并校验配置目录下的全部 episode。
convert-worker run --config configs/my_wuji.yaml \
  --run-root outputs/jobs/my_dataset

# 中断恢复、重试失败任务，或处理新增的包。
convert-worker run --config configs/my_wuji.yaml \
  --run-root outputs/jobs/my_dataset --resume

# 按清单执行完整链路；后续追加清单后用同一路径和 --resume。
convert-worker run --config configs/my_wuji.yaml \
  --run-root outputs/jobs/selected_dataset --episode-list lists/my_dataset.txt

# 仅处理指定包；--episode 可重复传入。
convert-worker run --config configs/my_wuji.yaml \
  --run-root outputs/jobs/subset --episode episode1
```

`--workers` 支持 1–4，默认 1，控制同时转换的 bag 数。每个来源独立暂存，按确定顺序提交。文件锁阻止同一任务或数据集被并发写入。

`--resume` 校验配置与来源，复用已完成的裁剪、跳过已提交转换，并重新执行校验。它不会删除历史已提交数据。更换任务、契约或转换参数时应使用新的输出目录。

任务状态和失败原因保存在 `jobs/` 与 `summary.json`。已提交转换若验证失败，记录为 `verification_failed` 并保留 `conversion_committed: true`；其他通过验证的来源仍可标记完成。数据集级验证失败会返回非零，不会把部分通过误报为整批通过。本节命令为本地批处理；常驻扫描、持久队列和接收检查见上面的自动接收说明。

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

# 仅校验数据集自身，可在没有原始包、原配置路径的机器上运行。
convert-worker verify outputs/lerobot/my_dataset

# 另外启用独立来源审计，需要来源 bag、契约及消息定义可访问。
convert-worker verify outputs/lerobot/my_dataset --compare-source
```

独立裁剪可通过 `--config configs/idle.yaml` 指定检测参数；串联 job 使用内置默认裁剪参数。裁剪输出不能位于输入目录内，已裁剪的包会被拒绝再次裁剪。转换续跑使用 `--resume`，不能将任意非空目录作为已有数据集接管。

## 静止段裁剪

裁剪使用左右臂各 7 个实测关节，逐消息按关节名称对齐。以较慢状态流的原生记录时间为参考，匹配另一臂最近的过去状态；默认允许的状态年龄为较慢流中位周期的 1.5 倍。

默认在前后合计 100 ms 的完整窗口内，每个关节的最大值与最小值之差不超过 `0.0001 rad` 时，标记为静止。候选计数要求超过 `stationary_frames=3`，该计数包含进入边界帧；删除区间为 `[times[start+1], times[i-1])`，保留边界状态。参数见 [configs/idle.yaml](configs/idle.yaml)。

仅删除前后均观察到运动的内部静止区间。录制首尾保留；非有限值、配对状态过旧、超过中位周期 3 倍的状态断流及不完整窗口会中断静止证据。遇到这类边界时，分别保留并裁剪两侧已经完整观测到的静止子段，不丢弃之前积累的静止证据，也不跨越不确定区间合并裁剪。每个子段保留边界状态。手部运动不参与裁剪判定，不另外合并相邻区间。

检测版本 3 增加独立的静止块覆盖检查：直接检查每个完整静止块的安全内部是否已覆盖，并检查删除区间未穿过运动或无效帧；检查失败即停止导出。结果写入 `auto_idle_detection.effectiveness_audit`。该检查用于验证既定规则是否执行完整，不代表识别任务阶段或证明任务成功。

RGB、深度、触觉、关节和控制消息等全部话题使用共同的删除区间，并同步压缩时间轴。这里删除的是主动识别的等待，与转换阶段遇到缺失数据时的处理不同。

## 时间轴、帧率与采样方法

### 有效范围与时间戳

转换使用采集报告的 `source_effective_start_time_ns` 到 `source_effective_end_time_ns`。清洗后的采集报告在内存中映射到压缩时间轴；不重复执行固定时长的首尾裁剪，也不扩展到整个 bag 的起止范围。选中范围最外侧不满足有效性要求的采样点仍可能被裁掉。

默认以消息 `header.stamp` 建立整数纳秒时间索引。到达乱序的消息按源时间稳定排序，相同时间保留索引遍历顺序中的最后一条。标准消息在清洗时已经改写过的时间戳不再次映射。

未知自定义类型在裁剪时保留消息体，只改写 bag 记录时间，并列在 `trim_report.json` 的 `unmodified_payload_types` 中。转换时：

- 被标记为未改写的 `ArmCommandStatus`、`HandTelemetryStatus` 使用压缩后的 bag 记录时间。
- 其他被标记为未改写的类型，其 header 时间通过裁剪映射转换。
- 未被标记的消息继续使用 header 时间。

零 header、缺少必需流、错误序列化、重复或缺失关节、缺少速度及非有限关节值会报错，不用零值伪造状态。不要把 `source_collection_state.json` 直接改名为 `collection_state.json`，否则会混用两条时间轴。

### 原始频率不同，怎样统一到 30 Hz

当前转换目标固定为 **30 Hz**，在有效时间窗口内建立等间隔采样网格。每个时刻、每路数据分别选择时间不晚于该时刻的最近消息，即因果采样保持（零阶保持，ZOH）：

```text
原始图像时间：     0 ms          50 ms         100 ms
原始图像：          A              B              C
输出网格：        0 ms    33.3 ms    66.7 ms    100 ms
输出使用：          A        A          B          C
```

这说明 20 Hz 相机在转换为 30 Hz 时会自然重复部分图像；200 Hz 关节状态则只选取网格时刻可用的过去样本。深度与触觉也分别按自身时间戳采样，不按消息序号与 RGB 强行配对。

**重复使用过去帧不是生成新观测，也不一定表示源数据丢帧。** 程序不取未来帧、不做线性插值、光流补帧或神经网络补帧。统一输出帧率也不会校准设备时钟偏移。

每路 RGB 保留原分辨率，使用 H264/yuv420p，默认 CRF 23、`veryfast`。三路输出视频均为 30 fps，第一帧 PTS=0；Parquet 第 i 行与各路视频第 i 帧对应。视频有损编码不保证 RGB 像素逐位相同；深度和触觉数值不经过该视频编码路径。

### 状态、动作与接管

状态和动作分别读取实测关节与命令，不把实测位置直接当作正常接管时的动作。动作保持绝对关节目标，不预先计算手臂 delta。

有实际接管状态的分组，接管时要求命令新鲜；明确停用后保持停用瞬间可用的最后目标。录制开始即停用且没有历史目标时，使用实测位置初始化。接管状态本身仍需新鲜、有效；重新接管要求匹配新命令，允许状态与命令最多 20 ms 的发布偏差。

因此，“停用后保持目标”不要求该历史命令一直满足 150 ms 的年龄限制，但不是允许接管状态或实测状态断流。Sharpa 手部没有实际接管流，不使用这种停用推断；其绝对命令必须持续满足新鲜度要求。

## 缺帧、过期与有限保持

### 默认阈值

```yaml
fps: 30
max_staleness_ms: 150
camera_short_gap_max_frames: 2
camera_short_gap_max_age_ms: 250
min_segment_frames: 2
```

| 参数 | 实际含义 |
| --- | --- |
| `fps` | 本目标固定为 30，所有模态共用输出网格 |
| `max_staleness_ms` | 正常采样允许的最大消息年龄；适用于实测状态、有效接管状态、接管中的命令、RGB、深度及触觉 |
| `camera_short_gap_max_frames` | 每处内部视觉缺口可接受的连续输出采样点数，支持 0、1、2；0 关闭 RGB 与深度接缝的有限保持 |
| `camera_short_gap_max_age_ms` | 有限保持时允许的最大实际图像年龄，不能靠改时间戳满足此条件 |
| `min_segment_frames` | 选中有效 episode 的最短帧数，不是动作窗口长度，也不能消除训练端的末尾 padding |

“消息年龄”是当前网格时间减去被选中消息的采样时间。没有过去样本、消息过期、接管状态无效或活动命令不满足条件，都会让相应采样点无效。

### RGB 怎样处理短缺口

允许继续使用已经选中的过去图像，但必须同时满足：

1. 缺口位于窗口内部，不是首尾不完整区间。
2. 按三路 RGB 无效点的**并集**计算，连续缺口不超过两个输出采样点。
3. 缺口及其相邻边界的低维状态、动作、接管状态均有效。
4. 缺口内每路 RGB 都有过去图像，且实际年龄不超过 250 ms。

“两帧”指 30 Hz 输出网格上的两个点，两个采样周期合计约 66.7 ms，不是允许源相机任意丢两帧，也不是允许图像额外老化 250 ms。250 ms 是图像的总年龄上限。

多处独立短缺口分别检查，例如分别缺 1、1、2 个点可以逐处容忍；多路相机交错形成的长缺口不能拆开规避限制。连续 3 个无效点不会只修前两个；两点以内但图像太旧、没有过去图像或低维数据无效，也不能保持。

这里只允许视觉模态继续引用原样本，不复制整条观测与动作记录，不修改图像原始时间。

### 深度与触觉的处理

普通深度和触觉遵守 150 ms 的严格年龄限制。深度有一个只适用于本工具裁剪接缝的例外：

- 深度缺口为内部区间，连续点数不超过 `camera_short_gap_max_frames`。
- 缺口及相邻网格范围内存在 `trim_report.json` 记录的实际接缝。
- 缺口内各点同时属于已经接受的头部 RGB 短缺口。
- 其他模态及相邻边界均有效；所选深度图来自过去，实际年龄不超过 `camera_short_gap_max_age_ms`。

满足条件时继续使用原深度图，记录每个点的真实图像年龄、源时间及接缝位置。没有清洗映射的原包、不在接缝附近的深度缺口、仅深度异常而 RGB 正常的情况，均不使用此例外。

RGB 与深度曝光时间不要求完全一致：已接受的头部 RGB 短缺口与深度短缺口有交集即可，不要求深度缺口的每一帧都落在 RGB 缺口内。其余限制仍同时适用：必须紧邻已记录的裁剪接缝、最多 2 个输出点、实际样本年龄不超过 250 ms、两侧边界及其他模态有效；报告会记录交集帧数和实际样本年龄。

触觉形变图、力/力矩不使用视觉宽限。它们仍可在正常 150 ms 范围内因果保持；超过范围就无效。深度零值原样保存，不代表程序已经重建了缺失距离。

### 仍有缺口时怎样处理

有限保持后，只允许裁掉选中范围最外侧的无效网格点。两个有效部分之间仍有任意内部无效点时，整条录制记为质量排除；本目标不会拆成多个 `part_N`，也不会删除未知缺失区间后把两侧拼成相邻训练帧。

一条合格录制生成一条 episode。当前仅支持 `segments: full-all`，不提供阶段识别或四阶段导出。质量排除与程序失败分别记录；命令正常结束也可能存在质量排除，应检查汇总报告。

## 怎样查看缺帧及保持情况

`inspect` 的报告描述转换前的来源时间索引；完整 job 的总报告位于 job 根目录的 `quality_report.json`，包含原始时长与消息数、删除时长/消息数及其比例，并关联输入频率、转换和验证指标。独立转换的数据集报告位于 `dataset/conversion/quality_report.json`。其中汇总对齐无效点、实际导出帧数、各流最大接受年龄、重复采样比例、采样年龄 P50/P99/最大值、有限保持、质量排除和验证失败。重复比例只读取小尺寸审计列，不加载整批深度或触觉数组。停用命令允许保持历史目标，其采样年龄应结合接管状态理解。

新转换来源记录还保存 `cleaning`，包含删除时长、删除消息数及时间映射；已有记录缺少该项时显示为未记录，不把缺失值当作零。程序记录对齐网格上的无效点、各流未匹配点和有限保持情况，**目前没有独立的源相机丢帧率或网络丢包率统计**。应区分以下口径：

| 指标 | 含义与限制 |
| --- | --- |
| 源丢帧率 | 需要来源的期望频率、序列号及采集契约；不能仅用导出帧数反推，当前转换报告不直接给出 |
| 对齐无效率 | `invalid_frames / grid_frames`；统计整个有效窗口在保持处理后仍无效的输出点，包含可能被裁掉的首尾，不等于源丢帧率 |
| 单流未匹配比例 | `unmatched[流名] / grid_frames`；是该流检查后的网格计数，多路可能重叠，不能直接相加；状态有效标志等另有检查，不能单凭该值判断整行有效 |
| RGB 保持点数 | `camera_short_gap_tolerance.accepted_frames`；按缺口并集计数，指超过正常新鲜度后被有限接受的点，不含正常升帧造成的所有重复图像 |
| 深度接缝保持点数 | `depth_splice_tolerance.accepted_frames`；只统计满足接缝例外的深度点 |
| 导出重复比例 | 可按同一路连续输出使用相同源采样时间的比例计算；低帧率升到 30 Hz 本身就会产生重复，不能直接视为丢帧 |

成功提交来源的 `dataset/conversion/records/*.json` 中，`alignment` 包含：

- `grid_frames`、`invalid_frames`、`retained_frames`、`segment_count`：整个 validated 对齐窗口的统计。
- `unmatched`、`max_age_ms`：各流的检查计数及通过该流年龄检查的样本最大年龄；包含视觉宽限接受的年龄，不仅统计最终导出点。
- `camera_short_gap_tolerance.gaps`：RGB 缺口边界、涉及的相机、接受点、图像源时间与实际年龄。
- `depth_splice_tolerance.gaps`：深度接缝保持的网格范围、接缝、源时间及实际年龄。
- `selected_export`：真正导出的帧范围与帧数。对齐窗口统计不能直接当作最终导出统计。
- `splice_audit`：接缝前后各关节组的位置与动作变化，仅报告，不反向改写裁剪结果。

被质量排除的来源没有成功提交的 episode 记录；其缺口位置和长度在 `dataset/conversion/rejections.json` 中。程序异常位于 `errors.json`。转换汇总的 `excluded_by_quality`、`errors` 与 job 根目录的 `summary.json` 应分别检查。

例如查看已有数据集的对齐统计，不读取视频或大尺寸传感器数组。缺少的可选统计字段表示未记录，示例输出 `None`，不能按零次保持解释：

```bash
python - outputs/jobs/my_dataset/dataset <<'PYCODE'
from pathlib import Path
import json
import sys

root = Path(sys.argv[1])
for path in sorted((root / "conversion/records").glob("*.json")):
    record = json.loads(path.read_text())
    a = record["alignment"]
    n = a["grid_frames"]
    print({
        "source": record["source"],
        "grid_frames": n,
        "invalid_grid_ratio": a["invalid_frames"] / n if n else None,
        "exported_frames": a["selected_export"]["frames"],
        "rgb_tolerated_points": a["camera_short_gap_tolerance"]["accepted_frames"],
        "depth_splice_tolerated_points": a.get("depth_splice_tolerance", {}).get("accepted_frames"),
    })
PYCODE
```

普通升帧产生的重复可通过 Parquet 的 `audit.sample_timestamp_ns` 检查：按 metadata 中该列的 `names` 找到相机或传感器列，统计相邻行使用相同非零时间的次数，再除以相邻行对数。该列保存当前采样时间索引，不是原始包序列号；有重复 header 或裁剪时间被钳制时，也不能据此推断唯一的原始消息数量。

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

`dataset/meta/` 包含 `info.json`、`tasks.jsonl`、`episodes.jsonl` 和 `episodes_stats.jsonl`。每 1000 条 episode 一个 chunk，表格位于 `data/chunk-*/episode_*.parquet`，RGB 视频位于 `videos/chunk-*/observation.images.cam*/episode_*.mp4`。

`conversion/manifest.json` 保存转换配置与契约指纹；`records/` 是每个已提交来源的记录；`verification.json` 保存数值与视频校验结果，独立 loader 检查另写 `loader_verification.json`。这些记录与标准 LeRobot 元数据共同保留，便于恢复和来源审计。

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

### 关节顺序与统计

State 与 action 均为 float32。State 按 `[左臂位置、左臂速度、右臂位置、右臂速度、左手位置、左手速度、右手位置、右手速度]` 排列；action 按 `[左臂、右臂、左手、右手]` 排列。双臂各 7 维，Wuji 每手 20 维，Sharpa 每手 22 维。具体关节名写入 `meta/info.json` 的 feature names。

Wuji 的 `observation.engaged` 为左臂、右臂、左手、右手四个实际接管状态；两种机器人都提供 `observation.arm_engaged`。Sharpa 不输出不存在的手部接管字段。

`meta/episodes_stats.jsonl` 保存每 episode 的 min、max、mean、std、count。关节向量及力/力矩按维度统计；深度和形变图使用可广播的全局标量统计。RGB 每段每路最多均匀抽取 100 帧，并进行空间下采样，按 [0,1] 通道值统计，不是全量像素遍历。

这些数据集统计不等同于训练端的 normalization stats。训练若进行关节重排、手臂 delta 或动作窗口变换，需要按相应训练配置另行计算。新增深度和触觉特征也需要训练端配置对应模型输入；生成数据集不会自动修改模型或启动训练。

### 动作窗口与 episode 边界

输出按 episode 独立保存，不把不同录制拼成一条时间序列。附带的 loader 检查使用 50 步 action 窗口，并检查 episode 尾部重复最后一个动作及 `action_is_pad` 标记。实际训练的窗口长度和 padding loss/mask 处理由训练端决定，不由转换器修改。

30 Hz 下，50 个动作采样点从第一个到最后一个相隔约 1.63 秒。将 `min_segment_frames` 设为 50 不能消除 episode 最后若干起点的 padding；训练侧仍需明确处理。训练/验证划分应按 `source_recording_id` 分组，避免同一来源的衍生版本进入不同集合。

## 校验范围与失败定位

默认 `verify` 只依赖本工具输出目录中的数据、metadata 和提交记录，不检查原始包或原机器上的契约路径，可用于搬迁后的离线结构校验。它检查每条 episode 的字段、嵌套数组形状、dtype、NaN/inf、索引、30 Hz 时间网格、统计、完整视频解码及逐帧 PTS。RGB 有损编码，不做源像素逐位相等断言。

`--compare-source` 额外启用逐时刻的独立采样审计：复用只读消息解析和关节契约，但不调用转换阶段的 `align` / `prepare`。重新推导状态、动作、接管保持、RGB 短缺口及深度接缝条件，再逐行对比关节、无损传感器数组和审计时间。该检查既能发现写出错误，也能检出转换对齐函数输出与约定不符的情况。

验证中每条 episode 单独记录结果，一条损坏不会阻止检查其余 episode：

| 文件 | 内容 |
| --- | --- |
| `conversion/verification.json` | 数据集总结果；运行中为 `running`，完成后为 `passed` 或 `failed`，包含逐 episode 结果 |
| `conversion/verification/episode_*.json` | 每条已提交 episode 的验证结果与错误 |
| job 根目录 `quality_report.json` | 整条链路的输入、裁剪比例、转换及验证汇总 |
| `conversion/quality_report.json` | 数据集质量指标、排除原因和验证错误；使用对应验证的完成时间标识 |
| `conversion/rejections.json` | 因内部缺口或最短长度条件而排除的来源 |
| `conversion/errors.json` | 转换程序错误 |
| `jobs/*.json`、job 根目录 `summary.json` | 裁剪/转换/验证状态及原始数据完整性结果 |

`convert-worker verify` 的关键验证使用显式异常检查，使用 `python -O` 也不会关闭校验。验证和转换共用数据集文件锁；验证报告需要输出目录可写。`--resume` 可重新校验已提交文件，但不自动覆盖或修复损坏的训练文件，数据内容损坏应定位后重新生成。

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

## π0.5 的 RGB 训练兼容性

深度和触觉可以保留在数据集中，由训练适配明确选择三路 RGB、实测关节位置、动作和任务文本。额外列不会自动变成模型输入；标准 LeRobot loader 仍可能读取这些列，因此存储完整性和读取性能是不同问题。

Wuji 的 108D 原始状态和 54D 动作可使用现有 Wuji 适配。Sharpa 的原始状态为 116D 位置/速度，动作为 58D 的“左臂、右臂、左手、右手”，不能直接套用只接受物理顺序 58D 状态的旧训练配置。`tomato-openpi` 的独立配置 `training_configs.experiments.tomato_sharpa.multimodal_rgb.tomato_fr3_sharpa_lora_v1`（或 `fullft_v1`）先严格核对字段名称，选取位置并重排为“左臂、左手、右臂、右手”；三路相机保持 cam0 外部、cam2 左腕、cam1 右腕。原有 58D baseline 不变。

训练应指向一套固定、已封存的批次，重新计算与该批数据、适配和 horizon 绑定的统计。该训练适配属于独立训练工程，不是接收器依赖。实际来源、归一化和 JAX 数据输入验证摘要见 [集成验证记录](docs/integration_validation.json)；记录只证明数据链路已验证，不代表训练或实机效果已验证。

`--compare-source` 覆盖所选状态、动作、无损传感器和采样时间，并完整解码 RGB。可另外执行只读的 RGB 内容抽查，比较每集首/中/尾和裁剪接缝附近的源图像：

```bash
python scripts/check_rgb_source.py --dataset /path/to/dataset --output outputs/new_rgb_report.json
```

该抽查报告 MAE/PSNR，不新增质量门槛，也不将有损 RGB 宣称为逐像素无损。清洗与 30 Hz 对齐本来就会删除等待段、丢弃未选中的高频消息或重复低频图像；LeRobot 不是所有 ROS 话题的逐消息备份，原始 rosbag 始终保留。执行来源比较需要当时的清洗包仍可访问；自动接收器在删除中间包前完成比较并归档结果。
