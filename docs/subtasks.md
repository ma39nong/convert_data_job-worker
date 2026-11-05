# LeRobot 粗细阶段预标注与审核（试用版）

本模块在 `subtasks-data-convert` 分支开发，直接处理现有 LeRobot v2.1 的 Parquet + MP4。ROSbag 清洗、normal/speedup 生成与既有上传工作流保持原逻辑。源数据只读，标注和缓存另存；默认端口 8766，与工作流 GUI 的 8765 分开。

这是**预标注与审核工具**。当前本地检索基线对不同录制泛化不足，不能当成已经可靠的无人标注系统。JEV 接口已实现，但需要服务端凭据；未提供凭据时不会外发图像。本机真实调用的验收范围见 [验证记录](subtasks-validation.md)。

## 启动与试用

使用工程现有 `.venv`。新部署环境安装 `.[annotation]`，需要 Python 3.11+；缓存服务器数据另需现有 SSH 配置、rsync。无需运行原工作流，也不需要启动旧人工清洗 UI。

```bash
cd /home/user/convert_data_job-worker
# 新环境才需要：.venv/bin/python -m pip install -e '.[annotation]'
./scripts/start_subtasks.sh \
  --root outputs/subtasks/pilot \
  --output outputs/subtasks/review
```

浏览器打开 `http://127.0.0.1:8766`。本机试用缓存只有 Sharpa/Wuji 各一条参考 normal、一条不同录制的 normal、一条对应 speedup。部分缓存保留完整原始元数据，但只下载明确选择的 episode；左侧仅列出本机已缓存 Parquet 的条目。

对自己的本地数据，可重复指定 `--root /absolute/dataset_or_parent`，其中含 `meta/info.json`。输出目录必须与输入根互不包含。一个输出目录同时只允许一个审核进程；多个浏览器窗口通过 revision 检测覆盖冲突。

1. 选择 episode，同步查看三路相机。首次加载会完整解码检查视频尺寸、帧数及 PTS，后续按来源 SHA 缓存验收结果。
2. 在当前帧按 `M` 拆分，选择细动作，拖动播放定位或输入帧号后点击“定位”。开始/结束帧可直接修改，邻段边界同步移动；也可合并下一段。
3. 看片确认边界和标签后，勾选“已看片确认此片段与边界”。只要还有未确认或 unknown，整条就仍为 draft。
4. 点击“保存标注”，再切换下一条。Ctrl/Cmd+S 保存；空格播放/暂停，左右方向键逐帧，Shift+方向键移动 30 帧。
5. 全部任务是否成功由“整条任务结果”单独记录，默认 unknown。视频结束不自动推断成功、放回裸杆或停止。
6. 可下载当前已保存的 JSON。界面提醒未保存修改；保存历史始终另存。

浏览器三路播放会持续纠正时间偏差，但播放过程不是硬件帧锁。审核精确边界应暂停后逐帧定位；标注保存的帧号是权威坐标。

## 标签定义

| 粗标签 | 细标签 ID | 内容 |
|---|---|---|
| 拿起番茄 | `grasp_truss` | 右手抓稳整串番茄 |
| 拿起番茄 | `lift_truss` | 右手提起番茄串并移至中间位置 |
| 摘取番茄 | `grasp_fruit` | 左手接近并抓紧一颗番茄，右手稳定托持番茄串 |
| 摘取番茄 | `pick_and_place` | 左手摘下并放入左侧果盘，右手稳定托持番茄串 |
| 放回番茄 | `return_truss` | 右手将番茄串放回右侧白盘并松手 |
| 旋转番茄杆 | `rotate_stem` | 右手手指移动并旋转番茄杆 |
| 放回番茄 | `return_stem` | 确认全部摘完后，右手放回番茄杆 |
| 结束 | `stop` | 停止任务 |
| 其他 | `idle` / `retry` | 等待暂停 / 失败重试调整 |
| 待确认 | `unknown` | 无法确定或尚未标注 |

粗标签由细标签唯一推导，避免两层互相矛盾。连续相同粗标签在粗轨道合并显示，细轨道保留每次动作。动作可以循环任意次数，不套固定的 19 段时间模板。

## 自动预标注的两条路径

### JEV 多相机视觉识别

采用 TypeSafe 官方 [图像输入协议](https://docs.typesafe.ai/images)和 [Choice 协议](https://docs.typesafe.ai/primitives/choice)，固定 `jev-1.14.0`；图像接口目前被官方标为试验能力，效果必须用实际视频验证。

需要在启动服务的本机终端设置 `TYPESAFE_API_KEY`，密钥不写配置、不放进浏览器、不提交仓库。可以交互输入后导出环境变量：

```bash
read -rsp 'TypeSafe API key: ' TYPESAFE_API_KEY
export TYPESAFE_API_KEY
./scripts/start_subtasks.sh --root outputs/subtasks/pilot \
  --output outputs/subtasks/review --enable-jev
```

应先正常退出占用同一输出目录/端口的试用审核服务。`--enable-jev` 是明确启用图像外发；配合服务端 key 才能选择页面中的 JEV 方式。每次点击仅处理当前 episode，不自动遍历其他数据。

- 每 2 秒设置一个粗扫描锚点，每个请求带该时刻及前后各 1 秒的三路图像。
- 从固定动作类别选择；遮挡、无法判断时允许 unknown。
- 粗标签变化的相邻采样间再按约 0.25 秒间隔查询。该精度是采样间隔，不是已经验证的边界误差。
- 默认每条最多 256 个采样请求（包含命中缓存的逻辑请求），上限不自动提升。超过预算的细查明确记录，保留粗边界。
- 选中概率低于 0.6 或模型 confidence 低于 0.5 时留为 unknown；这两个阈值尚未用人工真值校准。
- 原始回答、实际模型、token 用量、请求内容摘要、对应图片及来源身份留存在输出目录 `jev/`，可恢复并复用精确请求缓存。发生网络失败不会发布半条新标签。
- 不自动重试外部请求，避免隐含重复计费；用户重试会复用已完成请求。可在输出 `jev/STOP` 创建文件，阻止下一次新请求；正在等待的单次请求最多 90 秒超时。删除该标记后可再次运行。

页面“生成 JEV 请求计划”只写本地元数据、显示粗扫描次数，**不调用 API、不外发图像**。命令行也支持：

```bash
.venv/bin/python -m convest.subtasks.cli list --root outputs/subtasks/pilot --output outputs/subtasks/review
# 从列表取对应 id；CLI 与正在使用同一输出目录的审核服务不能并跑。
.venv/bin/python -m convest.subtasks.cli jev-plan --root outputs/subtasks/pilot \
  --output outputs/subtasks/review --id EPISODE_ID
.venv/bin/python -m convest.subtasks.cli jev --root outputs/subtasks/pilot \
  --output outputs/subtasks/review --id EPISODE_ID --allow-remote-images --max-requests 256
```

已有语义标签时须明确选择替换（CLI `--replace`）；旧版本仍保留。所有自动输出均为未审核，任务结果不由模型擅自改成成功。

### 本地样例检索（辅助基线）

无外部账号时可试验：

- 没有匹配模型时，仅用关节运动变化建议分段点，标签保持 unknown。
- 先人工确认至少两类动作的片段，保存，再点“从已确认片段学习”。无需先标完整条视频，已确认片段即可作参考。
- 结合约 3Hz 采样的关节位置、速度和三相机空间色彩特征做近邻检索；与参考距离太远或类别接近时拒绝猜测。
- Sharpa/Wuji 按真实关节名称与相机 schema 分开建模，每类最多保留 300 个参考采样点，控制内存。
- 样例修改或来源变化后，旧模型拒绝复用，须重新学习。
- 不允许用模型覆盖其自己的参考 episode；应选择其他录制预标注。

**此基线没有通用视觉语义能力，对新机位、不同姿态、果串位置和细动作歧义可能表现很差。** `similarity_margin` 是距离分差，不是正确概率。试用附带的参考来自 Codex 查看抽帧制作的初标，明确标为 `unreviewed_pilot`，不是人工真值。默认训练仅使用人工确认的片段；允许草稿参考的 CLI 选项 `--allow-draft-pilot` 仅用于本次试验。

外部模型也可通过 `import --file predictions.json` 导入标准 source+segments JSON；导入必须匹配完整 source 指纹，并会清除其审核标志。此入口不直接调用其他供应商。

## 从 28 只读选择缓存

不把 `sftp://` 字符串当成本地路径。采用已存在的 SSH 别名 `28`，在服务器用 Python 只读列清单，然后 rsync 选择的 episode，前后两次远端全清单 SHA 与本地 SHA 核验通过才发布缓存。缓存目标已存在且不同则拒绝覆盖；临时文件失败后清理，只影响本次新建暂存目录。

```bash
.venv/bin/python -m convest.subtasks.cli inventory --host 28 \
  --dataset /mnt/data/train_platform/datasets/tomato_sharpa/normal/no-block-more/A/实际批次目录
.venv/bin/python -m convest.subtasks.cli fetch --host 28 \
  --dataset /mnt/data/train_platform/datasets/tomato_sharpa/normal/no-block-more/A/实际批次目录 \
  --episode 0 --episode 1 --destination outputs/subtasks/cache/sharpa-normal-selected
```

Wuji 对应 `tomato_wuji/normal/pick_more_1031`，speedup 则选各自 speedup 根下的对应批次。`inventory` 不递归下载数据；`fetch` 只下载明确选择的 Parquet、多相机视频、必要元数据及处理映射。服务器只执行读取操作；不修改远端、不上传、不删除源数据。

## normal → speedup 标签映射

先审核 normal，再在页面选择同源 speedup。候选菜单按 `source_recording_id` 过滤，后端仍严格核实：

1. 录制身份、原始有效窗口、FPS 一致，映射的 `source_length` 等于 normal 帧数。
2. 观测/action 映射有限、严格递增、在来源范围，符合下一步 action 关系。
3. 每个 speedup 观测的 `source_timestamp_ns` 等于 `normal[np.rint(source_index)]`，与现有 RGB 最近来源帧规则一致。
4. 依观测来源帧归属标签；不直接复制秒数，不以平均加速倍数换算。

被加速采样完全跳过的短段在 `collapsed_normal_segments` 报告。action 使用另一时刻，其跨标签边界的帧号另报 `action_crosses_boundary_frames`，不伪装成观测和 action 完全同时。speedup 仍需审核，不继承 normal 的“已确认”标志。

## JSON 与训练交付边界

输出目录布局：

```text
review/
  annotations/<episode-id>.json       当前版本
  history/<episode-id>/000001.json    各次保存历史
  media-validation/                  按来源 SHA 缓存的完整视频验收
  features/                          本地视觉/状态特征缓存
  models/                            按机器人 schema 区分的样例模型
  jev/                               计划、帧图、请求和回答缓存
```

每条 JSON 记录 `schema_version`、`revision`、`source`（本地路径、远端来源、来源录制、帧数、FPS、各文件 SHA256）、`status`、`origin`、`outcome`、`provenance` 和 `segments`。阶段示例：

```json
{
  "start_frame": 270,
  "end_frame": 348,
  "start_seconds": 9.0,
  "end_seconds": 11.6,
  "label": "grasp_fruit",
  "coarse": "摘取番茄",
  "text": "左手接近并抓紧一颗番茄，右手稳定托持番茄串",
  "reviewed": false
}
```

区间采用 `[start_frame,end_frame)`；首帧 0，末段结束为帧总数，连续覆盖、无重叠无空段。秒数由帧号推导。unknown 不允许审核通过。JEV 分数单独存 `model_probability` / `model_confidence`，与本地 `similarity_margin` 分开。

**当前交付到独立标注 JSON，尚未把标签写进训练用 `task_index`，也未裁分成新的训练 episode。** 粗指令、细指令、完整任务提示应怎样供训练读取，需要在标签效果确认后决定，不能直接覆盖原数据集的任务索引。
