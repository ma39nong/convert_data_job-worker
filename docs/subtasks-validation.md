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


## 2026-11-05 PI0.5示范质量与人工异常标记

按用户澄清把可接受掉果恢复、空手投放错误、左手整串拽走再交回分开，新增behavior.py整段2fps候选检查及独立人工事件/训练取舍。扫描不改变动作标签、reviewed、outcome或人工训练决定；模型输出强制pending，不能自证确认/排除训练。界面普通豆包预标注后接独立行为检查；可独立扫描以保留既有动作标签。预算为动作分析预算之外一次显式UI行为检查请求，不将其混报为原动作max_requests之内。

真实调用既有豆包接口复核两条既有缓存，不重新生成动作标签：Sharpa ep1当前更新到v9，候选64–72秒drop_then_resume、150–156.5秒empty_hand_place、231–248秒truss_takeover_return；三项都是未经人工确认的模型候选，尤其整串转交可能误判，留给用户看片。Wuji新ep3元数据v2，本次未检出候选，不等同于认证无异常。原始source、动作segments（包括确认位）与outcome逐字段未变，training_review未自动写入或改动；其他原有标注逐字段未变。材料在outputs/subtasks/behavior-validation/，包含输入/响应在原review/doubao缓存、annotations-before、两条-result.json与final-verification.json。

337passed/7skipped全工程，最终源码定向标注49passed、Node/diff通过。新增覆盖独立保存/历史与字段校验、禁止模型自确认或自排除、保留人工确认/误报记录、normal→speedup异常映射与压缩消失记录、误报不重引入、训练取舍不照搬，以及真实HTTP异步扫描保留动作与人工选择。

另用独立合成小视频及18767临时审核服务验证UI添加人工异常→保存→编辑帧区间→保存→误报处理/人工保留→保存重载，动作和source完全不变。合成数据在behavior-validation/ui-fixture与ui-review，未混入pilot，临时标签页与服务已清理，仅保留测试文件。真实结果页tab6保留，模型3候选均待确认、训练取舍未决定，浏览器warn/error空；截图review-behavior.png。仅正常更新8766并保留token，当前PID2513623；8765未改，未改原始数据、未真正过滤训练数据。用户原人工页未刷新或代保存。

## 2026-11-05 全部11条的结果回推与连续原帧复查（v6）

本节覆盖前述仅修指定片段的流程和旧辅助动作词表。当前策略为`tomato_outcome_grounded_style_source_frames_v6`，只保留七个主动作及unknown。normal重新分析整段七粗阶段、两次拿起及5fps局部采摘过程；每一个分界均经过上下文定位、连续原始帧判定，证据不足扩大范围后再查。speedup先按真实观测来源帧映射，再独立检查每个映射分界的画面；视觉冲突保留待核，不破坏来源对应。异常仍在独立人工记录中。

抓果结束于最后一次有效抓稳、最终拽脱开始之前；失败接近、没抓住、重抓和仍连在同一果实上的失败拉扯不自动拆成新循环。抓果→摘下分界额外提供后续三个结果时刻，要求模型看见同一单果脱离并被左手保留，再回溯起点。仅有拉扯、空手、落果或整串转交不足以认证正常摘下。每个源帧请求和逐图状态都有记录，但模型观察仍可能误判，不能称为人工逐帧真值。

人工已确认参考只用于其他录制的两类动作含义示例，不复制时间或果数，目标自身和同源normal/speedup排除。参考自身另做完整独立预测与原26个边界的单独复查，原人工JSON逐字段保留。独立模型在这条参考上的细动作逐帧一致率80.595%、粗阶段96.592%，预测31段而人工27段，仍有多拆循环问题；不作为训练就绪或未见数据准确率。上一轮v5为77.38%/91.33%，报告另存baseline-v5.json，未隐去校准不足。

用户最新截图对应Sharpa additional normal ep2：旧v3第9/10段1389–2024帧收敛为一段抓果，第11/12段2024–2346帧附近为同一次有效摘取投放。v6实际重新定位为抓果[1389,2031)、摘下放盘[2031,2346)，全条21→19段。模型原帧证据为2030抓稳且相连、2031开始最终拽脱；后续2134/2240帧见独立持果向果盘移动。此边界是模型候选而非人工确认；该条仍有4个其他分界未能证实。具体输入、输出和旧标注快照见`outputs/subtasks/review/quality-review/1adeebcf5b0e698c`及doubao源身份/策略缓存，观察对照图为`quality-review/inspection/ep2-final-grasp-extraction.jpg`。

Sharpa heldout ep1 v12为27段，6个未证实边界。约189–224秒（5682–6744帧）的正常摘下候选在上下文、连续原帧、扩围及重查中均缺乏“同一单果独立脱离且仍被握持”的证据；其源帧proof为needs_review，不能据此视为成功投放。页面选中该段会显示拒绝证实的原因；原局部观察文字仍作为模型假设留档，不当成真值。这类未解决候选仍需人工修改，不把全部运行完成等同于全部标签正确。

全工程回归350passed/7skipped；最终源码的标注与逐帧证据定向60passed。Node语法检查及diff检查通过。新增覆盖逐帧before/after完整证据、扩大范围重查、必须有后续单果持有、失败不延长成功投放补尾、人工示例来源隔离、全量保存CAS与人工参考保护、speedup来源版本变化拒绝、异常和训练取舍保留、报告保留人工已排除决定并纳入映射候选。测试通过验证实现约束，不代表视觉模型准确率。


### 本轮最终实际结果
全部11/11条完成模型复查，共263个边界、95处重点复核、20条待审异常记录。待核包含证据不足及speedup映射/视觉分歧，不是已知错误率；异常数量包含保留历史、同源映射和人工参考的报告候选，不等于独立错误次数。
| 片段 | 细段 | 边界 | 待核 | 待审异常 | 版本 |
|---|---:|---:|---:|---:|---:|
| additional/sharpa/normal ep2 | 19 | 18 | 4 | 0 | v4 |
| additional/sharpa/speedup ep2 | 19 | 18 | 14 | 2 | v3 |
| additional/wuji/normal ep2 | 26 | 25 | 6 | 0 | v5 |
| additional/wuji/normal ep3 | 20 | 19 | 2 | 0 | v5 |
| additional/wuji/normal ep6 | 26 | 25 | 6 | 0 | v4 |
| pilot/sharpa/normal ep0 | 27 | 26 | 3 | 2 | v4 |
| pilot/sharpa/normal-heldout ep1 | 27 | 26 | 6 | 3 | v12 |
| pilot/sharpa/speedup ep0 | 27 | 26 | 26 | 2 | v4 |
| pilot/wuji/normal ep0 | 31 | 30 | 5 | 1 | v4 |
| pilot/wuji/normal-heldout ep1 | 21 | 20 | 3 | 0 | v2 |
| pilot/wuji/speedup ep0 | 31 | 30 | 20 | 10 | v2 |

独立最终核对脚本`quality-review/verify_v6.py`通过：11份缓存来源SHA不变，人工000000 v4逐字段完全保留，其他10份主动作均未自动确认，旧异常与训练取舍保留；3组normal→speedup按真实source_index重新计算后与保存标签一致，normal来源版本吻合。每个分界均保存连续原帧输入，已证实的抓果→摘下均有握稳/最终拽脱及后续单果持有记录。此项检查验证完整性和流程约束，不认证模型观察正确。

首轮Wuji additional ep2概览因输出未完成而拒绝发布，完整失败报告在本轮目录first-attempt-report.json，原响应在raw-incomplete。将全局输出限定为9个结构区间、局部仍独立重看逐果动作后重跑成功，62个逻辑请求（含缓存）产出26段；通过--resume仅接续这一条，其他已保存结果不重写。resumed_at与最初started_at均保留。最终报告为quality-review/report.json，逐条proposal/behavior/published、人工reference-boundaries、用户案例user-feedback-verification及final-verification均在1adeebcf5b0e698c目录。逻辑请求数不代表整个调试的实际账单。

8766已正常SIGINT收尾并保留HTTP token，最终PID3985766；8765及原pipeline未动。真实浏览器核实11/11、各版本/疑点/异常、人工参考80.6%/96.6%、speedup帧差和异常定位链接；Sharpa ep2定位2031帧时三路画面均readyState4，选中第10/19段[2031,2346)，七粗阶段、八个主标签选项（含unknown），0段自动人工确认。复查页warn/error为空，完整截图为quality-review/review-all-eleven.png；用户原页面未刷新、未代保存。

## V8：首次提起标准及全11条边界复查

用户明确第1→2段从“开始提起的第一帧”进入，握稳后仍静止的时间留在第1段。V7临时“握稳即切”假设已撤销；手指姿态收敛只作辅助，不能直接充当提起时间。当前策略 `tomato_lift_motion_first_extraction_v8` 要求相邻原帧 `secured+still → secured+lift_onset`。左手从最后有效抓稳后的第一次主动摘脱进入摘下放盘，包含该次连续握持中的持续扭拉，不等最后一下断开。

本轮采用 `--boundaries-only`，保留已有动作循环并重查全部边界，不能视为独立分段准确率测试。run `08bd11b3431afe78` 的11条全部完成，共263个分界、最终97处重点待核、20条待审行为记录。待核包含来源映射与视觉分歧，不等于97处已知错误；行为记录包含历史与同源映射，不能当作20次确定异常。V7临时结果、V8定位流程修正前的停止报告和原始响应均保留；没有把未完成结果认证为成功。

| 片段 | 最终版本 | 细动作数 | 重点待核边界 |
| --- | --- | --- | --- |
| Sharpa additional normal 2 | v7 | 19 | 5 |
| Sharpa additional speedup 2 | v5 | 19 | 15 |
| Wuji additional normal 2 | v7 | 26 | 9 |
| Wuji additional normal 3 | v6 | 20 | 2 |
| Wuji additional normal 6 | v5 | 26 | 2 |
| Sharpa pilot normal 0（人工参考） | v4 原稿保留 | 27 | 3（另列模型意见） |
| Sharpa pilot normal-heldout 1 | v13 | 27 | 5 |
| Sharpa pilot speedup 0 | v5 | 27 | 26 |
| Wuji pilot normal 0 | v5 | 31 | 3 |
| Wuji pilot normal-heldout 1 | v3 | 21 | 1 |
| Wuji pilot speedup 0 | v3 | 31 | 26 |

Sharpa additional normal 2 的提起候选为88帧（约2.93秒），模型逐帧状态为87帧握稳静止、88帧开始提起。17→18曾由模型建议4017帧，但独立查看4016/4017/4050/4209原图后，发现前两帧左手仍较张开，未能证实稳定握持；因此**不能认定4017是正确分界**。已在normal与配对speedup中同步增加独立视觉反对意见，保留原始模型观察与候选位置，等待人工确定；没有用另一个猜测帧替换它。此次补充只改复核元数据，动作区间与人工行为决定逐字段不变，normal版本关联同步更新。

宽范围合法建议可以用于定位下一轮连续原帧窗口，但记录缺失时仍不算证实，最终连续帧要求不放宽。模型把已提供的后续结果帧误放入候选状态时，单独保留上下文并检查连接、持果一致；未提供帧、重复或矛盾状态仍拒绝。此流程防止退回旧候选附近反复检查，也不代表模型视觉判断必然正确。

`quality-review/verify_v8.py` 最终通过：11份来源指纹不变；人工000000整份v4 JSON完全保留；其他10份草稿未勾人工确认；原训练取舍、任务结果和人工行为记录保留；3组speedup按当前normal来源帧重算一致；每个边界都留有连续原帧输入。结论验证结构、来源与记录完整性，不认证逐帧视觉真值，也未导入PI0.5训练。

代码回归356 passed / 7 skipped，定向66 passed，Node、编译与diff检查通过。当前源码本地提交 `ba094c5`，未推送。真实浏览器核对报告11/11及全部版本，Sharpa2 v7第2段88帧、19细动作、7粗阶段、8标签选项及17→18独立疑点提示；未代勾确认或保存用户窗口。截图位于 `quality-review/review-all-eleven-v8.png`、`review-first-lift-v8.png`、`review-extraction-concern-v8.png`。最终记录、补充前备份和 `final-verification.json` 在上述run目录。仅8766审核服务更新，8765、18766、原数据、清洗阈值和暂停队列未改。

## 2026-11-06：目录队列与粗细阶段界面 V2

主界面改为“导入28目录 → 豆包逐个处理 → 看片编辑 → 分别保存粗、细阶段”。移除JEV模型配置网页、请求计划、独立全部片段复查导航和训练行为操作面板；旧疑点及异常记录在折叠备注中保留。应用名为subtask阶段标注，显示“调整番茄”，内部rotate_stem ID兼容旧稿。

新增workspace目录状态、顺序任务队列、两层版本与历史、两种已保存筛选和分别下载。粗阶段调整会联动细阶段对应范围，保留内部绝对帧分界、清除受影响细段人工确认；粗标签不兼容时留unknown。粗、细仍分别保存；重载时从已保存粗边界恢复细层约束，未保存联动明确提示。用户已人工确认的旧参考fine作为已保存继承，不迁移原稿。

真实28 SSH浏览和清单读取通过；Sharpa normal目录3条、30fps，选取已缓存episode2导入返回新增0/复用1，原11条列表保持。新缓存目录独立于原始数据，持久化登记重启有单元覆盖。本轮未实际再次运行豆包识别，不能用UI验收认证标签准确率。

隔离18767合成数据配合mock模型验证队列处理、独立粗细保存、过滤与两种真实JSON下载。后续联动验收：粗边界11→12，细对应边界同步12，内部6/15帧保留；仅保存粗阶段时fine磁盘版本不变，再保存fine、重载后保持12。18767测试服务正常SIGINT停止，测试页关闭。没有在真实片段中代用户确认或代保存。

修复阶段切换重复提交旧输入框导致误报“边界要连续覆盖”问题。真实浏览器对11条的全部粗细阶段累计351次点击，未报错且区间数组前后相等；另验证下一待审核段、逐帧按钮。新增Node回归覆盖切换只提交一次、边界前后移动、跨内部切点、受影响确认取消、不兼容标签unknown及幂等。

完整Python回归373 passed / 7 skipped；Node回归、语法及git diff检查通过。原11份legacy annotations与UI修改前逐字段一致，用户本轮在workspace中自行保存的粗细版本保留。最终证据在outputs/subtasks/ui-v2-validation/，ui-v2-final-verification.json通过；ui-v2.png为真实页面。仅8766 PID132697服务更新；8765、18766、源数据、质量阈值和维护队列未改。

## 2026-11-06：交付路径、版本映射与整串任务prompt

用户明确本地下载不能代替训练服务器交付。查阅旧工具 `/home/user/apps/data_clean_tomato_eaafe4e/data_clean_tomato/`：默认exports，YAML计划在.bagcut_plans，LeRobot结果新目录内有stage_annotations.json，同时重写task_index/meta/tasks.jsonl。旧工具本机默认exports目录当前不存在，不能把默认位置当作已存在结果。实际旧浏览器粗阶段JSON在/home/user/Downloads/episode_000002_723f2e11248e806f2b2401b9_coarse.json。

新增两层独立导出按钮、28原数据集标注目录/28自选目录/本机自选目录/浏览器下载四种目标，界面显示源包、工作稿、最新交付的完整路径和版本。新delivery.py发布前验证来源SHA，不覆盖既有结果；独立文件带revision+内容指纹，保留交付收据。训练副本通过本机PyArrow准备新的Parquet/元数据，服务器标准库核对来源并复制原视频到新的单episode v2.1目录；不依赖28系统Python安装PyArrow，不修改源meta/Parquet/视频。

用户补充旧单果prompt后，再明确当前为整串任务。核对官方OpenPI config.py的prompt_from_task、README公开flow/action支持与pi05论文：总任务更新为逐颗摘完全部番茄、放左侧果盘、最后放回裸杆。**训练默认goal模式，全部帧使用整串总prompt，阶段标签另存**；stage模式为显式实验选项，需要部署时配套阶段指令切换，不能等同自动学得高层规划。meta/subtask_prompts.json保留原总prompt、推荐新总prompt、阶段中英文和模式。原数据包prompt未改。先前仅阶段文本的试验副本保留历史，最终收据指向goal版。

新增跨目录包扫描与多选导入，真实扫描Sharpa根显示26个包。配对用recording_id+source起止纳秒，网页纳秒用字符串避免JS精度丢失；缺失包可按同源搜索，不按episode号猜。normal已保存粗细层分别用既有project()映射到speedup；保持源帧映射、action跨界与压缩丢段信息、normal层版本，映射后全未确认。替换已有保存版本需明确勾选并保留历史；页面标明过期/待更新。

实际Sharpa additional ep2：normal coarse r3/fine r2保留不变；映射生成speedup coarse/fine r1未审核，并分别发布两版本两层JSON至各自 `<dataset>/annotations/subtasks/{coarse,fine}/`。人工参考normal0另生成goal模式训练副本：
`28:/mnt/data/train_platform/datasets/tomato_sharpa/normal/no-block-more/A/1102_588_616_normal_lerobot/subtask_training/fine/episode_000000_r0_439652cb025a2d30/`。
独立回读11个输出文件SHA，7658帧、1个task、新整串prompt；原state/action/timestamp等逐列相等，原视频SHA一致。原11份legacy标注及本轮前已有用户层保存逐字段不变；未替用户勾确认、未训练。此为单episode格式/传输验收，不代表PI0.5机器人适配、批次训练或部署成功。

全工程380 passed / 7 skipped，Node阶段联动/导航回归和语法、compile/diff检查通过。新测试覆盖不同层/路径、幂等不覆盖、源不变、训练副本逐帧任务与状态、goal/stage模式、原prompt保留、拒绝未审稿、输出篡改检测、同源映射与历史保护、错配拒绝、批量导入单包失败继续。证据在outputs/subtasks/delivery-validation/，final-verification.json passed，delivery-overview.png与training-prompt.png为真实浏览器。8766正常收尾并沿用HTTP token，当前PID291031；8765/18766、原清洗阈值与维护队列不变。

## 2026-11-06：清空测试工作列表并准备1102/1103/1104正式批次

按用户截图选择Sharpa/no-block-more/A三个日期包及对应speedup，实际6包52条：1102各3、1103各7、1104各16。26组recording_id+源起止纳秒配对完整。独立新输出为outputs/subtasks/batches/sharpa-A-1102-1104，旧outputs/subtasks/review原地归档保留，不删除旧视频缓存、15份原标注/层保存或服务器既有交付。新输出只继承人工000000完整原稿及独立参考快照，其他旧测试草稿和工作层不复制。

导入全部完成：新增47条、复用5条、失败0；新增媒体/数据约2.406 GB，每包使用原有下载前后远端SHA、下载后本地SHA一致验收。来源一致的1102缓存复用，续启脚本resume.sh保存于新输出。8766正常SIGINT收尾并沿用HTTP token，当前PID397857；仅切换审核输出与数据根，8765/18766和维护队列不变。

verify_batch.py独立验证6目录/52条/26normal+26speedup，每条唯一对应；26组用真实source_index/action_source_index和source_timestamp_ns重新调用project通过，包含长度/时序/源观测对齐检查。人工000000原稿逐字段一致、全部15份旧标注/工作层SHA不变；新批次只1份人工参考头，51条待处理，无模型草稿从旧测试偷带入。未发模型请求、未训练。reference-verification.json验证其他录制得到10张人工转场例图，目标/同录制排除；不是识别准确率测试。

界面缩短数据包名与统计，默认仅显示所选包，片段按编号排序并优先打开待处理项。当前1102 normal只显示3条，000000标“人工参考·已完成”，不是把全部52条默认堆在左侧。完整来源路径仍可查看；批次栏提示人工参考已保留。24项定向单元回归、Node导航/联动和语法、compile/diff通过；不为纯展示改动重跑无关全工程。证据在outputs/subtasks/batch-1102-1104-preparation/，final-verification.json passed。保留人工参考能维持标法依据，不是新批次无误的保证，仍需逐帧人工复核。

## 2026-11-06 speedup依赖状态误报失败

实际用户先启动1102 speedup：ep1/2当时没有normal标签，旧代码报“请先处理同源normal，再处理speedup”并记为failed；ep0因人工normal参考存在而成功。随后normal ep2正在模型处理。该两条是依赖未就绪，不是视频损坏、缺人工参考或模型接口失败。

新增WaitingForNormal及源录制/起止纳秒依赖预检：缺normal或normal标签含unknown时，进入waiting_normal，不组装参考图、不发独立speedup请求；normal就绪后目录状态恢复pending，可继续处理。兼容旧failed字符串，但保留旧队列记录供追溯；HTTP等真正失败仍显示失败。前端同步兼容正在运行的旧服务，显示等待normal和跳转入口，统计不算失败；对应版本跳转会切换左侧数据包，避免主视图与列表不同包。

当前PID397857的normal ep2仍运行，**没有重启、中断、重复提交或更改正在写的队列文件**。前端兼容修复已实时验收；新的Python依赖预检需当前任务结束后正常重启才能加载，不能误报已热更新Python类。当前旧API仍可保留两条failed历史，网页按确切依赖原因显示waiting_normal。此分离只对指定旧依赖错误生效，不隐藏其他错误。

26项workspace/delivery测试通过，Node导航/边界/依赖兼容测试、compile/语法/diff通过。真实浏览器1102 speedup显示ep0草稿、ep1/2等待normal，选ep1显示“等待同源normal处理完成，再处理speedup”和对应normal入口。仅只读导航，无模型请求/人工确认/保存，截图outputs/subtasks/waiting-normal-validation/waiting-normal.png。新批次数据及参考保持。

## 2026-11-06：审核按钮与多步撤销

编辑卡右上三个按钮为撤销、当前段确认、当前层整条确认；下方保留拆分、合并、下一待审核段。Node回归覆盖连续撤销/重做、粗细联动恢复、未知标签阻止整条确认、合并/拆分与服务器版本保护。

18767隔离合成片段真实浏览器验证：边界修改与连续撤销、拆分合并、标签恢复、逐段与整条确认/撤销、保存fine v1后撤销再保存v2、键盘重做与撤销、刷新后持久化一致。只保存测试fine层，原测试模型稿和正式人工参考SHA未变；未对正式片段进行确认或保存。测试服务正常停止。正式8766静态页面直接使用新控件，无需重启运行中的豆包队列。截图与浏览器证明位于outputs/subtasks/review-controls-validation。

## 2026-11-06：双语标注与英文JSON

新增共享英文词表与UI镜像一致性测试，验证粗阶段四种编号、七段最后放回裸杆的独立英文；未知/异常序列不推断最后裸杆。工作层仍保留中文，导出白名单保留原帧范围/标签/确认/版本/身份SHA，删去中文备注、模型证据及投影事件说明。JSON下载、发布和训练副本stage_annotations统一英文。

29项vocabulary/delivery/workspace定向回归通过；全部subtasks测试97passed，三组Node回归通过。隔离18767真实HTTP粗/细下载均无中文且末段bare stem正确；浏览器双语选项、数字时间条和新总prompt验证，浏览器下载按钮显示交付浏览器提示（IAB下载事件未返回文件路径，未据此宣称落盘文件验收）。18767正常停止。正式8766只读查看人工参考，没有确认/保存/改边界，截图outputs/subtasks/bilingual-validation/bilingual-ui.png。

使用本机PaliGemma分词器逐一验证257种状态bin（58维统一值与逐项可加性），无标点总目标上界256，五种粗动作英文上界254/255/252/252/253，未改训练仓库或启动训练。报告token-budget.json。正在运行的1103normal批次保持，后端切换由一次性等待脚本完成，最终实际状态以deployment.json为准。

## 2026-11-06：配对剔除到C和恢复

新增quarantine原子配对登记、恢复和历史版本，HTTP鉴权、过期版本冲突、原因校验、缺失/歧义配对拒绝、当前处理对拒绝、C路径仅28 A/B包。catalog分离excluded计数；process锁内检查后跳过、映射及新建训练副本拦截。源文件和原标注不写。整包只读预览要求完整导入/配对、保留粗层审核完成，列出保留和剔除新旧编号。没有实现实体归档执行器，明确显示preview_only。

全部subtasks回归101passed、4项新Python测试通过、四组Node回归通过；语法/compile/diff检查通过。18767隔离真实浏览器完成填写原因→剔除→已剔除筛选→本包预览→恢复→普通列表恢复的完整往返，normal/speedup同步，原数据身份不变。正式8766仅打开窗口预览，确认按钮因旧后端仍在批次处理中禁用，没有提交真实剔除。证据outputs/subtasks/quarantine-validation，正式界面截图quarantine-button.png和quarantine-dialog.png。
