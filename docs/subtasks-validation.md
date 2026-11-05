# 阶段标注试用验证记录

日期：2026-11-05（Asia/Shanghai）。本轮在唯一工程 checkout 的 `subtasks-data-convert` 开发，不建立 worktree。试用入口 `127.0.0.1:8766`，既有工作流 GUI `8765` 保持原样；两项原任务实查 stopped/disabled，未启动采集、上传或训练。

## 已验证

- 全工程回归 **307 passed / 7 skipped**；新增阶段标注测试 19 项。最后的下载附件与映射并发保护修正另跑阶段标注、GUI、OSS 定向回归 **48 passed**，结果在本机证据保留。
- 新测试覆盖连续帧区间、粗细归属、unknown 审核门控、来源只读、历史版本及冲突、输出路径隔离、录制配对、最近来源帧及被压缩短段、action 边界审计、错误来源时间戳拒绝、完整视频长度检查、参考模型过期拒绝、HTTP token/origin/range、缓存 SHA 和变更源保护、外部预测导入清除审核标志。
- JEV 使用模拟传输验证多相机真实 JPEG 编码、时序上下文、边界加密检查、请求上限、低 confidence 留空、错误模型/概率/用量拒绝、缓存复用及密钥不落审计。**模拟通过不代表真实供应商接口或识别准确率已通过。**
- Node JS 语法检查和 `git diff --check` 通过；wheel 打包检查包含新 Python 模块、三个静态文件、两个命令行入口。
- 浏览器实际验证三路 MP4、帧号定位、拆分、合并、标签修改、相邻边界联动、保存版本、JSON 附件下载、同源 speedup 映射；保存修改历史。页面 console 无 warn/error。JSON 下载后与保存标注逐字段比较一致。

## 真实输入范围

通过既有 SSH 别名 28，只读缓存：

| 机器人 | 远端 normal 批次 | 参考/另一录制 | 对应 speedup |
|---|---|---|---|
| Sharpa | `tomato_sharpa/normal/no-block-more/A/1102_588_616_normal_lerobot` | episode 0 / 1 | episode 0 |
| Wuji | `tomato_wuji/normal/pick_more_1031/A/1031_28_137_normal_lerobot` | episode 0 / 1 | episode 0 |

均相对于 `/mnt/data/train_platform/datasets`。6 份本地 episode 版本，每份必要文件全 SHA 校验；缓存前后远端清单与 SHA 一致。试验末再查询六份选定清单，远端与本地文件均保持原校验结果。未批量处理四个根的所有数据。

完整解码三路视频检查尺寸、帧数和 PTS；逐帧来源校验后的映射：

| 机器人 | normal 帧数 | speedup 帧数 | 映射标签段 | 观测/action 跨边界帧 |
|---|---:|---:|---:|---:|
| Sharpa | 7658 | 5321 | 34 | 33 |
| Wuji | 5330 | 3961 | 32 | 31 |

这两条没有完全消失的标签段。合成回归另外覆盖短段被跳过时明确报告的情况。speedup 映射结果全部保持未审核。

## 本地自动化试验的实际结论

参考标签由 Codex 查看视频抽帧初步制作，边界仅为近似秒数，不是人工真值；不确定部分保留 unknown，没有自动补造“全部摘完、放回裸杆、停止”。两份参考均含六类核心动作。使用未审核参考建模只为试验，模型和页面明确标为 `unreviewed_pilot`。

在**另一条未作为参考的录制**上：

| 机器人 | 总帧数 | 留为 unknown 帧数 | unknown 占比 |
|---|---:|---:|---:|
| Sharpa | 7654 | 7354 | 96.08% |
| Wuji | 3374 | 2628 | 77.89% |

这是拒绝猜测的覆盖统计，**不是错误率，也不是准确率**；被赋标签的部分仍未经人工审核。该结果说明一条参考 + 低层外观/关节近邻不足以达成批量自动标注目标，所以本地方案仅保留为辅助基线。不能把在参考自身上的拟合结果作为泛化效果。

## JEV 与尚未完成的效果验收

JEV 视觉接口按 [TypeSafe 官方图像文档](https://docs.typesafe.ai/images)和 [Choice 文档](https://docs.typesafe.ai/primitives/choice)实现。固定 `jev-1.14.0`，每次锚点带前后时刻的多相机抽帧，先 2 秒粗扫再约 0.25 秒细查；带 unknown、请求缓存与次数上限，始终输出 draft。

本机没有配置 `TYPESAFE_API_KEY`，正式服务也没有开启 `--enable-jev`。**真实 JEV 调用为 0，未向外部模型发送用户图像，尚未测得 JEV 的真实边界误差、动作准确率、成本或审核省时比例。** 已生成四条 normal 的离线请求计划，粗扫描分别为 129、129、90、58 次，细查会额外消耗预算。

下一轮应配置凭据后，选择少量实际完整 episode 跑 JEV，并让人工确认标签与边界，记录段级漏标、分类错误、边界偏差和实际审核耗时。识别效果达标前，不应扩大为全量自动审核通过。

当前仅交付独立阶段 JSON；未重写原 LeRobot `task_index`，未裁分训练 episode，未运行训练。与训练数据读取器的最终接口需要在标签方案确认后继续实现。

## 本机证据

均位于工程 `outputs/subtasks/`（不提交 Git）：

- `pilot/verification.json`：末轮来源核对、原工作流状态、6 份标注、测试与限制。
- `pilot/pilot-report.json`：参考、未见录制、本地覆盖结果、speedup 映射审计。
- `pilot/jev-offline-plans.json`：未外发的请求计划摘要。
- `pilot/ui-review.png`：真实三相机及粗细阶段审核页面。
- `pilot/downloaded-sharpa-subtasks.json`：浏览器实际下载且核对一致的 JSON。
- `review/annotations/`、`review/history/`：当前初标及修改历史；全部为 draft，未人工确认。
- `review/media-validation/`：来源 SHA 对应的完整视频验收。
- `package/`：wheel 及构建验收材料。

开发中两次界面验证版本分别保存在 `dev-first-browser-check`、`dev-before-source-provenance`，不混作最终标注。远端、原始缓存和旧 GUI 配置未改写；本轮未推送远端仓库。

## 2026-11-05 JEV 账号连接准备
新增独立 /models 配置页，账号官网入口、密码框及连接状态；POST 接口复用同源/token 防护。只读取官方模型列表验证凭据，成功后内存启用 JEV，失败不替换原配置；密钥不落盘、不进入响应/标注/审计。图像推理显式客户端注入内存密钥，原环境变量 CLI 方式兼容。审核服务更新通过仅进程环境交接现有 HTTP token，原页面无需刷新即可继续保存。
22 项 annotation 测试通过，含连接防伪造、旧会话延续、凭据不泄漏、验证失败保留配置及模型列表唯一请求。尚无用户账号/key，真实图像调用仍为 0，未验证供应商实际图像响应或精度。

## 2026-11-05 豆包真实视频接入与人工对照

新增豆包后端，复用用户提供工程的官方 Ark Responses 接口及现有凭据，未导入/执行原工程，也未复制密钥到配置或 Git。固定 doubao-seed-2-1-pro-260628，三相机同步 2×2 视频、约5fps抽样，40秒核心分块、前后2秒上下文、最多两路并发。没有使用原工程的人手筛选/短动作合并条件或 SAM3 依赖。审核接口按原始帧输出固定粗细标签 JSON，并保持原有修边界、拆并、审核、下载和 normal→speedup 映射流程。

首次真实短块发现模型将短暂托持/左手回位误拆成 idle，故初试在两个完成请求后通过 STOP 停止后续请求；原始响应仍保留在 outputs/subtasks/doubao-validation。修正连续阶段定义后完整运行人工对照，证据在 outputs/subtasks/doubao-validation-v2：

- 参考为用户已确认的 Sharpa normal ep0 v4，27段，7658帧；预测单独保存为 prediction.json，没有替换人工标注。此条没有发送同一录制的标注图片或人工边界作为答案，仅用固定类别/划分规则，因此仍应称为“同条录制上的标法校准”，不能称为未见数据准确率。
- 模型输出30段，完整覆盖255.27秒；细动作逐帧一致率84.02%，粗阶段88.31%，unknown 0。unknown为0不等于全部正确。
- 26个人工内部边界中17个在1秒容差内一对一匹配，召回65.38%；29个预测内部边界中17个匹配，精确率58.62%。匹配部分平均偏差0.384秒，不代表全部边界平均误差。
- 明显偏差是旋转后再次抓串：人工131.67–149.00秒为grasp_truss，模型将其中大部分仍判成rotate_stem；首次lift→grasp交界也偏早。应重点审核这些边界，不能只检查unknown区域。
- 完整校准7次真实请求，约717.8秒，input_tokens545308/output_tokens43895，记录供应商返回用量，未据此估算账单。report.json保存逐类IoU、边界统计、调用记录；human-reference-before.json为人工版本快照，结束时验证服务端人工JSON逐字段相同。

全工程317 passed/7 skipped；标注、GUI、OSS定向58 passed。新增测试覆盖同步三相机源帧、上下文拼接/完整覆盖、固定标签/时间/分数强校验、无执行地读取凭据、审计与缓存不泄漏密钥、STOP和请求上限、推理期间人工保存与revision冲突，以及参考画面排除当前/同源normal-speedup和未审核录制。Node语法和diff检查通过。远程任务不持有全局保存锁，本地基线训练/预测原有锁行为保留。

另通过实际审核服务 /api/predict 对不同录制 Sharpa normal-heldout ep1 运行完整流程：使用7类人工代表画面（来源normal ep0 v4），7次真实请求，输出33段连续覆盖7654帧/255.13秒，保存为v5 draft，origin=doubao_multicamera_temporal_v1，全段reviewed=false、outcome仍unknown，unknown帧0；该录制没有人工真值，不能报告准确率。input_tokens608344/output_tokens45775。旧试验v4保留历史。请求和视频缓存在review/doubao/，独立交付JSON heldout-subtasks.json、用量与来源报告heldout-report.json、final-verification.json在doubao-validation-v2/。人工normal ep0 v4逐字段未变，原speedup映射/其余录制未替换。

审核HTTP在空闲时正常SIGINT更新，延续旧token并验证相同，最终PID1733531；已有页面未刷新/切换/代保存。另开新版页面直接定位ep1，核实豆包可选、33段/0已确认、粗细轨道、片段定位与confidence显示，浏览器warn/error为空。截图review-ui.png、review-controls.png在同验证目录；原人工页面与新结果页面均保留。此次只是两条试用录制（含人工校准），未全量处理远端四个场景根。

## 2026-11-05 用户纠正：单次旋转模板与真实脱离边界

用户指出ep1 v5第2段过晚，7/8边界应按“仍相连→真正脱离”定义，且整条粗流程必须与000000相同、只旋转一次。这条新指令取代此前“不约束整体顺序”的方案。新增template.py，七阶段/细动作语法强校验，整段约1fps定粗流程，旧密集草稿或摘取范围内5fps局部推理补足逐果循环，再对边界做约±2秒事件窗口及连续原帧复查；不固定果数，不复制参考时间。

整段概览曾漏掉局部循环，故不把它单独用于细动作计数。多图中思考模式曾耗尽12000推理token而无完整答案，low模式也较慢，试验随后用各自STOP结束后续调用并保留材料；最终使用官方支持的thinking=disabled（不带reasoning）输出边界JSON，参数计入缓存key，incomplete不进入成功缓存。模型返回frame=null/confidence=null表示弃权，归为未确认，不猜测或整条报错丢弃其他有效结果。失败/试验用量不藏在最终一次cache-hit计数中，原始记录留在各validation根，不估算账单。

最终独立预测在outputs/subtasks/doubao-template-validation-final/，通过带token/source/revision/模板校验的apply-proposal写入ep1 v6，旧v5完整保留：

- 29个细动作，七个粗阶段严格为拿起0–300、摘取300–3000、放回3000–3264、旋转3264–3726、拿起3726–4110、摘取4110–7494、放回7494–7654（均原始帧、左闭右开），旋转仅1段。两个摘取阶段分别保留6和5次模型候选循环，不照抄000000的27段。
- 原第2段结束360帧/12秒→300帧/10秒；原第7/8段分界1320→1319帧/43.967秒。两者是按新规则复查的候选，不声称已经得到人工逐帧真值。左手实测起动辅助标记297帧，视觉候选300帧，两者仍有3帧差异，保持待审核。
- 共18个逻辑请求（含精确缓存复用）；完整provenance记录原始用量、两次边界证据、候选来源v5/SHA与用户搜索种子。4个边界视觉未确认，0基边界id为8/10/13/15，对应细段9–10、11–12、14–15、16–17；页面显示提示。其余边界也全未人工审核，outcome仍unknown。
- human-before.json与服务端000000 v4逐字段相同；未改人工27段、其他episode、既有speedup映射和原始数据。updated-subtasks.json/final-verification.json及review-v6.png留在最终验证目录。

全工程323passed/7skipped，末轮标注35passed，Node/diff通过。新增覆盖重复旋转拒绝、局部细动作与七阶段语法、只能选择提供的源帧、null弃权、真实关节起动辅助、思考参数/不缓存incomplete，以及独立结果导入的source/CAS/强制未审核。只更新8766，正常SIGINT并延续原token，最终PID1923528。原页确认无未保存修改后加载v6，核实29段/七阶段一次旋转、10秒定位、未确认提示，浏览器warn/error为空。未修改8765工作流或数据质量规则。


## 2026-11-05 再次纠正：仍相连、长尝试及意外掉果

用户发现v6仍把握紧未脱离误当放盘，并将失败/掉落硬拼成成功循环。v3移除强制逐果成对与末尾延长放盘的逻辑；两段采摘都重看5fps、30秒核心/4秒上下文的1280×960三相机视频，输出连接/持果状态和事件。仍相连的持续尝试不按时长拆成功；新增fruit_drop、harvest_retry、harvest_uncertain细标签，粗轨道仍是七阶段/一次旋转。整段概览中放回时附带的落果保留事件说明，不伪造额外粗采摘阶段。

掉果需要另查画面：区分左手滑落、果串上的其他果实脱落、正常向托盘释放；复核否决的判断留档。JSON不自动宣称整条成功。豆包原帧回答仍会给出窗口外帧或遮挡下的猜测，因此保留可见的未确认区间，没有把结构通过当作准确率。用户六个截图仍相连帧只约束同source，模型不得在此前判已摘下。

独立材料在outputs/subtasks/doubao-events-validation。实际从28只读拉取并全SHA校验：Sharpa normal ep2（151.4秒）；Wuji normal ep2/3/6（166.43/98.67/164.03秒）；另取Sharpa配对speedup ep2（3197帧，106.57秒）。来源都在原有两个训练批次，未修改服务器或完整元数据；新缓存位于原工程pilot/additional，未创建额外checkout。

最终发布：原Sharpa ep1 v6→v7，32细段/七粗阶段/一次旋转，9个未确认边界、7个状态待确认段。六个用户帧945/1319/4464/5068/5694/6480均为grasp_fruit；其余帧未冒充人工标注。长抓取59.7–94.6秒保留为连续尝试，约94.6秒后的异常掉落与恢复单列，drop_checks记录掉落来源。仍需人工审核，不保证精确逐帧。

四条新录制均保存v1草稿：Sharpa ep2 24细段（9未确认边界）；Wuji ep2 27段（9）；ep3 19段（4）；ep6 26段（7）。配对speedup通过source_recording_id/源区间/观测时间戳与source_index逐帧映射验证，生成24段v1草稿，不做独立视觉准确率结论。原人工ep0 v4及其余5份旧标注中的非目标4份完全保留，逐字段比较通过；所有新增/替换段reviewed=false、outcome=unknown。最终JSON与对照为final-verification.json及各published-subtasks.json。

整个调试经历了整段响应输出HARVEST占位、视频长思考耗尽、模型批次id重置、短尾未覆盖、窗口外帧及误把正常释放算掉落等失败，失败材料及原响应都留档；成功缓存复用。最终各任务的逻辑请求数包含缓存，不代表全部调试实际调用数或账单。并未把低未确认数量当作性能指标，也没有未见录制的人工真值精度。

最终全工程333passed/7skipped，精确最终源码的标注测试45passed，Node/diff检查通过。新增覆盖长尝试/掉果/恢复语法、附着状态证据、自由落体与受控投放区别、果串落果、用户仍相连约束、无效原帧逐项弃权、短尾不延长成功、异常标签normal→speedup映射。仅8766正常SIGINT更新并保留HTTP token，最终PID2373777；8765未改。页面实查v7、32段/0已确认、七阶段、异常落果定位和复核说明；新Sharpa ep2 v1/24段可审核，浏览器warn/error为空，截图review-v7.png。原人工页面未刷新或代保存。
