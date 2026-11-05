# LeRobot 粗细阶段预标注与审核（试用版）

本模块在 `subtasks-data-convert` 分支开发，直接处理现有 LeRobot v2.1 的 Parquet + MP4。ROSbag 清洗、normal/speedup 生成与既有上传工作流保持原逻辑。源数据只读，标注和缓存另存；默认端口 8766，与工作流 GUI 的 8765 分开。

这是**预标注与审核工具**。当前本地检索基线对不同录制泛化不足，不能当成已经可靠的无人标注系统。支持豆包三相机视频预标注，并保留可选 JEV 接口；外部模型均需服务端凭据及用户发起预标注，未配置时不会外发图像。本机真实调用的验收范围见 [验证记录](subtasks-validation.md)。

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

当前只选以下七个主要动作及待确认。第3类抓果结束于抓稳且尚未开始拽脱；第4类从抓稳后的拽脱起动开始，包含拽脱、移向果盘与释放，不等到果实已经离杆才切换。掉果、重试、遮挡、停止、等待不再单列动作阶段；异常保留在独立训练行为审核与原始观察记录。

| 粗标签 | 细标签 ID | 内容 |
|---|---|---|
| 拿起番茄 | `grasp_truss` | 右手抓稳整串番茄 |
| 拿起番茄 | `lift_truss` | 右手提起番茄串并移至中间位置 |
| 摘取番茄 | `grasp_fruit` | 左手接近并抓紧一颗番茄，右手稳定托持番茄串 |
| 摘取番茄 | `pick_and_place` | 左手摘下并放入左侧果盘，右手稳定托持番茄串 |
| 放回番茄 | `return_truss` | 右手将番茄串放回右侧白盘并松手 |
| 旋转番茄杆 | `rotate_stem` | 右手手指移动并旋转番茄杆 |
| 放回番茄 | `return_stem` | 确认全部摘完后，右手放回番茄杆 |
| 待确认 | `unknown` | 无法确定或尚未标注 |

粗标签由细标签唯一推导，避免两层互相矛盾。连续相同粗标签在粗轨道合并显示，细轨道保留每次动作。动作可以循环任意次数，不套固定的 19 段时间模板。

## 自动预标注的两条路径

### JEV 多相机视觉识别

采用 TypeSafe 官方 [图像输入协议](https://docs.typesafe.ai/images)和 [Choice 协议](https://docs.typesafe.ai/primitives/choice)，固定 `jev-1.14.0`；图像接口目前被官方标为试验能力，效果必须用实际视频验证。

推荐打开独立的 [本机视觉模型配置页](http://127.0.0.1:8766/models)，从其中的官方控制台入口注册/登录并创建 TypeSafe API key，然后在本机密码框填写，点击“验证连接并启用 JEV”。账号条款、充值由用户自行完成。连接验证仅请求官方 `GET /v1/models`，不会发送图像或运行推理；通过后在服务内存中启用 JEV，密钥不落盘、不回传到页面、不写日志或标注文件，服务重启后需重新填写。验证失败保留原有配置。

连接完成后，先保存原标注页未保存的修改，再刷新标注页选择 JEV。连接验证通过不等于图像接口或识别效果已经验证，应在另一条录制上小样本试验，保留已完成人工标签。

也可以在启动服务的本机终端设置 `TYPESAFE_API_KEY`：

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

### 豆包三相机视频预标注

可复用已有火山方舟接口。在本机终端设置 `ARK_API_KEY`，或显式指定已有工程中的凭据文件：

```bash
./scripts/start_subtasks.sh --root outputs/subtasks/pilot \
  --output outputs/subtasks/review \
  --doubao-key-source /home/user/pipeline_text_label-20261016-172802/pipeline_text_label/api/tools/ark_video_understanding_current.py
```

仅解析指定文件中的 `DEFAULT_ARK_API_KEY` 字符串，不导入或执行原工程。密钥只保存在服务内存，不复制到本工程配置、缓存、响应或标注中。默认固定 `doubao-seed-2-1-pro-260628`；可用 `--doubao-model` 显式指定其他已开通版本。只有选中豆包并点击“自动预标注”才发送图像/视频；原始数据不修改。已有标签替换仍需页面确认，旧版本保留。

- 每个复合视频由同一原始帧的三路画面组成：左上 cam0、右上 cam1、左下 cam2，右下留黑，不镜像，不改变时间速度。保持宽高比例，抽样约 5fps，原始帧/抽样索引和视频 SHA 留档。
- 按用户最新规则，整段约1fps确定000000的七个粗阶段：拿起→摘取→放回→旋转→拿起→摘取→放回，旋转只允许一个区间。抓串/提起各分两段，两个摘取阶段允许失败、掉果和重试，果数和时间不照搬参考。不符合模板时拒绝发布，不用重复旋转的分块结果拼凑。当前整段模式支持8分钟以内录制。
- 摘取阶段必须重新看局部5fps视频：30秒核心、前后4秒上下文、1280×960三相机合成。旧草稿不再决定循环或补齐放盘。模型同时输出连接状态（attached/detached/occluded）、持果状态（held/empty/falling/occluded）和观察说明，保留JSON审计。长时间仍相连的拽拉保持grasp_fruit；新增fruit_drop（掉果）、harvest_retry（失败后恢复）、harvest_uncertain（采摘状态待确认），都归入粗阶段“摘取番茄”。不强制成功成对，不按时长切断尝试。最多两路并发，默认上限32请求，预算不足或失败不发布半条。
- 分界先在约±2秒窗口按5fps复查，再回看候选附近连续13个原始帧；首次左手准备接近的窗口可扩大到约±0.75秒。提起→抓果取左手开始准备接近的时刻，抓果→放盘取真正脱离枝杆的首帧，仍相连的抓握/拧动都属于抓果。左Fr3七关节存在稳定前缀时，起动标记辅助定位接近动作；没有修改清洗或数据质量阈值。抓果→放盘须逐张分类全部供给帧，并出现仍相连→脱离且握住的相邻证据；自由下落不算受控投放。证据缺失时，候选放盘段改为harvest_uncertain并标记unresolved，不宣称精确。
- 按用户人工标签保持“右手抓稳→提起”和“左手抓果→最终摘下放盘”的区别，重复摘果逐次保留。短暂托持和左手回位归入相应连续动作，不因不足 1–2 秒强制合并。模型仍可能错分或漏分，需审核。
- 使用已完整审核标注的标签类别和粒度约定；在其他录制上额外选取每类一张人工标注的三相机代表画面。仅使用关节 schema 一致的参考；排除当前 episode 及同一 source_recording_id 的 normal/speedup，避免把被评估的同源画面作为答案。来源 id、revision、标注 SHA、代表画面 SHA 进入 provenance；不直接复制参考的时间戳、次数或整条动作顺序。
- 模型返回连续片段结束秒数，本地映射回原始帧；首尾完整覆盖、单调边界和固定标签均强校验。采摘状态不明或证据被否决时留harvest_uncertain，其他低分标签仍保留unknown；全部未审核。confidence 是模型自述分数，不是经过校准的正确率。任务成功与否不自动推断。
- 整段/局部视频使用官方 Responses API 的内联 Base64，边界复查使用带原始帧号的多图序列，不创建远端 Files 对象，`store=false`；实际服务端数据处理以供应商政策为准。输入视频及原始输出留在本机 `doubao/`；请求审计只记视频 hash、提示词、参考身份，不记录 key 或 Base64。精确请求可复用缓存，`doubao/STOP` 阻止后续新请求，已在处理的请求最多等待 420 秒。一个批次至多两条请求已提交，失败后不启动后续批次。
- 远程推理期间不占用全局保存锁，其他 episode 可以保存；当前 episode 若被另一窗口更新，最终发布会通过 revision 拒绝覆盖，独立预测仍保留在缓存。

“从已确认片段学习”仍用于本地近邻模型。豆包会直接读取已保存且整条已审核的参考，不需要点击该按钮，也没有对豆包权重做微调。参考新改动应先保存，下一次豆包请求的参考指纹会随之变化。

2026-11-05 的ep1 v6仍存在握紧/实际脱离混淆，以及将掉果和长时间尝试误认成功循环的问题。v3策略改为上述状态与异常事件分析，不再把v6当作合格边界样本。与此前自由分块版本的84%校准统计是不同策略，不将旧指标当成新版准确率。用户给出的画面帧号只作当前来源的搜索种子，存于`doubao/corrections/<episode-id>.json`并绑定完整source指纹；种子不被强制当成最终答案。

边界图片请求按[官方深度思考参数](https://docs.volcengine.com/docs/ark/responses-api-deep-thinking?lang=zh)关闭长思考并限制输出，以避免把额度耗尽在推理文本；整段理解保留思考。参数纳入缓存身份，供应商返回incomplete时只留原始失败材料，不发布标签或写成功缓存。未确认边界在页面提示；手工编辑后不继续按旧段号显示生成时的警告。`/api/apply-proposal`仅用于提交已完成的独立模板预测，同源token校验、source/版本/模板校验后保存，自动清除reviewed并保留历史。


2026-11-05 v3补充：用户截图提供的仍相连帧绑定完整source身份，存入corrections的known_attached_frames，只约束这条录制。相应尝试的脱离不得早于这些帧；与其冲突的掉果推断被否决，接近/抓持前缀归入grasp_fruit，观察之后不自动推断。frame945/1319/4464/5068/5694/6480均作当前来源约束，没有复制到新录制。

模型边界id在每个请求内从0编号，解析后才恢复到全局段号。返回未提供的帧、无效id或证据缺失时逐项弃权；不插值、不猜归属，也不因一项错误丢掉其他有效结果。所有声称fruit_drop的区间另作同步画面复核：要求看到先有支撑、后无支撑下落，区分origin=left_hand和truss；正常释放到目标果盘不算掉果。原始判断与否决理由都留在drop_checks，页面同时展示，依然是模型草稿。复核预算计入max_requests，超过预算拒绝发布。图像状态与掉果复核关闭长思考，整段概览保留思考；请求参数都进入缓存身份。


### PI0.5训练行为审核

动作阶段与示范质量分开记录。掉果后直接继续采摘是可接受的恢复，不自动判整条失败；掉果后仍空手走向果盘执行投放、左手拽走整串再交回右手是需要人工确认的异常。整串转交不能硬套普通单果采摘/旋转，遮挡下不确定的区间保留候选。

“训练行为审核”面板支持：检查训练异常候选，定位候选帧，修改区间/类型/说明，确认事实或排除误报，添加人工记录。新模型候选一律pending，不自动决定训练取舍。豆包“自动预标注”完成后，界面再单独运行一次行为检查（一个额外视频请求）；行为检查失败时动作草稿已保留并明确提示。也可独立检查既有标注，不覆盖动作标签。2fps整段候选检查不提供逐帧准确率，空候选不能认证数据正常。

JSON新增可选review_events（id、start_frame/end_frame、kind、status、origin、note）与training_review（decision=pending/keep/exclude、note）。kind为drop_then_resume、empty_hand_place、truss_takeover_return、uncertain_behavior。status为pending/confirmed/dismissed。动作标签全审核通过与训练取舍无直接等同关系；只有人工改变训练取舍才保存keep/exclude。当前只是记录决定，不删除原始数据、不自动剔除、也不改训练文件。

normal→speedup同时映射未排除的异常区间，依据观测source_index映射，映射后重新pending；已排除误报不重引入。完全被压缩掉的异常id留在collapsed_review_events供检查，不静默消失；normal的训练取舍不自动覆盖speedup的人工决定。重复扫描以稳定事件id合并，不覆盖用户已经确认/排除或修改的记录；保存依旧受source身份与revision保护。


## 2026-11-05 主动作收敛（当前 v4，覆盖前述 v3 的边界与额外动作规则）

`tomato_main_actions_extraction_onset_v4` 只输出主动作；历史 JSON 的旧标签仍可读取，但新下拉框与模型发布结果不再提供六个辅助标签。未知或失败操作不自动变成成功投放，候选动作仍未确认；独立行为审核、人工训练取舍继续保留，尚未接训练过滤导出。

抓果→摘下放盘改为抓稳后的主动拽脱起点：上一帧须抓稳且相连，下一帧开始拽脱，两帧都可以仍相连。已离杆并移动的画面不能证明拽脱起点。原帧请求分类 phase/attachment/retention；证据不足只标边界待核，不新增阶段。旧 known_attached_frames 仅约束历史实际离杆判定，不再限制当前拽脱动作起点。

当前未确认草稿做一次可回溯收敛，相邻同动作合并，原标签保存在历史及审计副本；人工已确认模板不变。旧离杆边界未复查处继续提示待核。Sharpa additional normal ep2 首次抓果/摘下边界从534帧前移到模型建议493帧（16.43秒），未代替人工确认；配对 speedup 按真实观测 source_index 重映射。两次真实图像请求、原帧及前后版本审计见 outputs/subtasks/simple-actions-validation。

## 全部片段的质量复查（v5 基础流程，后续由 v6 扩展）

`tomato_all_boundaries_source_frames_v5` 覆盖前述 v4 的单点复查方式，主动作与拽脱定义不变。自动预标注先重新看完整视频与5fps局部采摘过程，检查粗阶段、单次旋转及每次采摘；随后**所有**细动作边界都经过上下文与连续原始帧复查。上下文用约±2秒范围定位，再逐帧看±12帧；证据不足会扩大到约±6秒再重新看连续原帧。每帧需输出before/after/occluded，接受候选须有相邻before→after证据；抓果→摘下另须握稳/开始拽脱及连接、持果状态。模型置信度不能替代这些证据，证据不足保留unresolved，不新增异常动作阶段。默认预算128请求，最多256；未完成全部流程不会发布半条结果。

`python -m convest.subtasks.batch_review --credential-source /path/to/existing/source.py` 对8766已加载的全部片段复查，默认两条独立视频并行，可用`--workers 1/2/3`。normal重新分析完整动作及边界、独立检查训练异常；speedup严格按其normal观测source_index映射，再独立看每个映射边界的连续原帧。不把映射验证算成独立视觉验证；视觉建议与来源映射不一致时留待人工复核，不静默破坏对应关系。任意已确认动作的录制只输出独立复查意见，不自动改写；其他标注通过source身份及revision CAS发布，保留人工异常和训练取舍。跨窗口的新修改会拒绝覆盖。

`/quality`展示本轮全部片段处理状态、边界数、仍有疑点的边界、异常候选数量及版本；展开每条可查看各边界帧号并定位审核。`outputs/subtasks/review/quality-review/<run-id>/`保存开始前全部标注、逐条proposal/behavior/published及报告；原始请求/响应及逐帧输入在doubao缓存，随source和策略区分。中断后成功请求可复用；失败不是审核完成。整个流程是模型复查，不能宣称等于人工逐帧真值或100%准确。

首次左手准备接近的运动辅助搜索覆盖整个前置提起区间，不能只围绕可能已经选晚的候选±3秒；视觉建议明显偏离该起动线索时保留冲突，不能用“后来接触到果实”充当首次接近。运动数据只作线索，不能证明握持或脱离，也不修改清洗阈值。

局部视频若模型漏标尾部或结构无效，允许一次带明确时长/错误说明的重新观察，仍须完整覆盖；不直接延长成功投放去填空白。HTTP/连接失败不自动重试。`batch_review --resume`仅接续最近一轮未完成项，复用成功请求，已发布版本或人工参考发生新修改会保留并报告冲突。人工参考既有每个分界也单独复查，保存于`reference-boundaries.json`；整段独立模型与其逐帧标签一致率只作该条参考校准，不作为全量准确率。

## 人工样例与投放结果回推（当前 v6）

`tomato_outcome_grounded_style_source_frames_v6` 在逐帧复查基础上增加结果约束：先看同一目标果实确实脱离且仍被左手持有、随后去投放，再向前追溯有效握持及最终摘脱起点。没抓到、失败拉扯、重新靠近同一果实不自动生成新的成功循环。第3阶段可包含这些准备/失败尝试；第4阶段包含最终摘脱与投放。旋转结束后的手指安排、抓取准备即进入抓串，不等最终握稳或开始提起。

完整人工参考提供两类过渡画面：抓果→摘下、旋转→抓串准备。只学动作含义，不复制时间、果数、位置；目标自身及同源normal/speedup禁止充当自己的示例。全局视频只确定七粗阶段与两次拿起的抓/提分界，局部5fps视频独立识别果实循环。

每个抓果→摘下候选除连续原帧外，再看该次摘取区间的后续三处结果画面，并逐图记录attachment/retention/single_fruit；至少一处需见同一单果独立脱离且被左手保留。仅有手部拉扯、全部仍相连、空手、自由下落、整个果串或遮挡均不能认证普通摘下。缺少证据保留重点复核标记，模型不能自作人工确认。

v5校准在既有人工作业000000上仅约77.4%细动作逐帧一致、91.3%粗阶段一致，不能当成训练就绪或推广到其他视频的准确率；原始失败/中断报告另存quality-review/baseline-v5.json。v6对当前全部片段统一重跑，保留前版历史。Screenshot反馈“旧9/10为未抓到后的重复接近，旧11/12为一次有效摘取投放”绑定来源存user-feedback，用作语义回归，不直接把截图帧号钉为答案。
