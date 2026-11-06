# subtask阶段标注

在唯一工程的 `subtasks-data-convert` 分支开发，默认地址 http://127.0.0.1:8766/ 。本工具处理 LeRobot Parquet 与三路 MP4，源数据只读。8765 工作流、原清洗/转换/上传和暂停队列不受影响。

## 使用顺序

1. 点击“导入28目录”。粘贴 `/mnt/data/train_platform/datasets/...` 或 `sftp://28/...` 路径，点击“读取目录”，打开包含 meta/data/videos 的 LeRobot 数据目录。片段编号留空导入全部，也可填 `0,2-5`。仅下载所选片段，已有缓存复用。
2. 上方按数据目录显示片段数、待处理/已处理、粗/细已保存数量。“开始逐个处理”使用服务端已配置的豆包，顺序处理未完成片段；单条失败不阻断后续。已人工确认或已保存细阶段的片段跳过。自动结果始终是待审核草稿。
3. 左侧选择片段，保留三路视频同步查看。空格播放，左右键逐帧；也可输入帧号定位。精确边界应暂停后逐帧检查，保存帧号为权威坐标。
4. 点击粗/细时间条中的分段，编辑对应标签与起止帧。“更新边界”会连同相邻段调整，保持连续覆盖。支持当前帧拆分、合并下一段、下一待审核段。
5. **粗阶段边界会联动细阶段的对应范围**，内部细动作分界尽量保留在原始帧位置。被新区间吞掉的细段会移除，受影响细段取消确认；粗标签发生不兼容的变更时，对应细动作留为待确认，不猜测动作。已保存粗阶段在重载时也会约束细阶段范围。
6. **“保存粗阶段”和“保存细阶段”分别保存**。粗边界联动后，细阶段显示未保存，需检查后再保存细阶段。保存草稿不等于人工确认。上方两个已保存按钮分别查看对应结果，“导出粗阶段 JSON”“导出细阶段 JSON”分别选择导出内容和保存位置。
7. 点“下一个片段”继续。切换片段或关闭页面前提示未保存修改。原模型疑点、异常说明放在折叠的“备注与疑点”，不必经过其他审核页面。

原先已经人工标好的片段会作为已保存细阶段继承，原文件不迁移、不重写。旧页面需刷新才能载入新版脚本；先保存窗口中自己的修改。

## 标签和边界标准

粗阶段为：拿起番茄、摘取番茄、放回番茄、调整番茄。初始按已有细动作生成；两层各有保存版本，编辑粗边界会约束细层范围。细动作只保留以下主要动作和待确认，不把掉果、重试、等待细分为新阶段。

| 粗阶段 | 细动作 ID | 内容 |
|---|---|---|
| 拿起番茄 | grasp_truss | 右手抓稳整串番茄 |
| 拿起番茄 | lift_truss | 右手提起番茄串并移至中间位置 |
| 摘取番茄 | grasp_fruit | 左手接近并抓紧一颗番茄，右手稳定托持番茄串 |
| 摘取番茄 | pick_and_place | 左手摘下番茄并放入左侧果盘，右手稳定托持番茄串 |
| 放回番茄 | return_truss | 右手将番茄串放回右侧白盘并松手 |
| 调整番茄 | rotate_stem | 右手调整番茄 |
| 放回番茄 | return_stem | 全部摘完后右手放回番茄杆 |
| 待标注 | unknown | 尚未确定 |

兼容历史标注，内部 `rotate_stem` ID 不改名。第一次稳定握住但仍静止属于抓稳，从开始提起的第一帧进入提起。左手接近失败、重抓仍属接近抓稳；由后续连续持果/摘脱过程回查最后有效抓稳，从其后第一次主动拽脱开始进入摘下放盘，包含扭拉摘脱，不能等果实已经断开才切换。调整动作按既有七段粗流程只出现一次。

掉果后继续采摘可以接受；空手继续投放、整串被左手拽走再塞回等异常保留为人工疑点，不能默认作为正常训练样本。本轮 UI 更新不重新认证既有边界，也不自动导入训练。

## 启动与配置

使用工程 `.venv`，新部署安装 `.[annotation]`，SSH alias `28` 及 rsync 应已可用。

```bash
cd /home/user/convert_data_job-worker
./scripts/start_subtasks.sh --root outputs/subtasks/pilot --output outputs/subtasks/review
```

可重复传入本地 `--root`。服务端通过 `ARK_API_KEY` 或既有 `--doubao-key-source /absolute/existing_config.py` 读取豆包凭据，不返回浏览器、不写标注。保持本机原配置启动参数即可。网页只使用豆包，JEV配置页与按钮已移除，旧 `/quality` 跳转主界面。历史CLI研究工具不属于当前网页流程。

仅绑定本机，写接口使用会话 token 和来源检查。输出目录有单进程锁；同阶段多窗口保存通过 revision/source/base revision 检测冲突。

## 输出与队列

- 原有 `annotations/` 与历史模型证据保留。自动处理仍发布未审核草稿，人工粗/细结果另存。
- `workspace/queue.json` 持久化队列状态；重启后未完成运行标为中断，可重试。
- `workspace/stages/<episode-id>/coarse.json`、`fine.json` 分别保存两层；`workspace/history/<episode-id>/<stage>/` 保留历史。
- JSON含来源身份、版本、帧与秒、标签、人工确认和备注。保存一层不会覆盖另一层的磁盘版本；联动未保存的细范围在页面明确提示。
- 新导入缓存放在输出目录旁的 `<output-name>-imports/`，例如 `outputs/subtasks/review-imports/`，不混入原始数据根。重启会重新登记这些缓存。
- 服务器浏览只允许 alias28 的 `/mnt/data/train_platform/datasets` 范围，校验真实路径；新下载使用原有清单与哈希校验后加入列表。
- 自动队列先处理 normal，再处理 speedup；对应版本按同源帧映射，单独保留视觉分歧，不把加速时间直接当原视频时间。人工参考、已保存细阶段与并发人工修改受到保护。

识别模型 confidence 不是准确率；细边界仍需人工看连续原帧。验证范围见 [验证记录](subtasks-validation.md)。


## 导出位置、normal/speedup 与训练任务 prompt

“保存阶段”保存本机工作稿；“导出阶段”发布交付文件，界面下方列出工作稿、原始包与最近发布的完整路径和版本。

- **28原数据集（默认）**：JSON在 `<dataset>/annotations/subtasks/{coarse,fine}/episode_XXXXXX_r版本_指纹.json`，两层分开，文件不可覆盖。新版本另存，发布前逐文件核对源SHA。
- **28自选目录**：在 `/mnt/data/train_platform/datasets` 树下选择其他绝对路径；按源数据包身份、层级再分目录，避免不同包同号片段冲突。
- **本机自选目录**：输入绝对路径，不得放进输入数据根。实际文件路径返回界面。
- **浏览器下载**：仅下载JSON到浏览器设置的目录。本机已确认之前的下载在 `/home/user/Downloads/`；这不是上传服务器。

导入弹窗可以扫描当前目录下所有LeRobot包，展示normal/speedup、条数、完整路径，多选后逐包导入；单包失败保留明确错误并继续。查找同源包只扫描元数据，按recording ID及起止纳秒共同匹配（纳秒以字符串返回页面，避免JS整数精度丢失），不会仅按episode号猜配对。当前已导入的配对可直接互相跳转，不必重复导入。缺少对应版本才需导入它，随后可将已保存normal粗、细层分别映射到speedup。映射用观测源帧，不按视频时长比例；映射后取消人工确认，保留来源版本、映射SHA、action跨边界记录，normal更新时显示过期。替换speedup已有保存版本需勾选并留历史。

JSON本身不会让训练代码自动读取subtask。因此导出内容还可选择“LeRobot训练副本”，在新目录生成单episode的v2.1数据集：重排episode/index从0开始、保留完整state/action/视频帧、写入task_index、meta/tasks.jsonl、阶段序号、统计与stage_annotations.json，不修改原包。默认28位置 `<dataset>/subtask_training/{coarse,fine}/episode_XXXXXX_r版本_指纹/`。它是单片段结构导出，不是已经训练或整批PI0.5验收；多片段汇总、机器人适配、归一化与部署策略属于后续训练配置。

原prompt“Pick up a tomato truss ... pick a cherry tomato ...”只描述一次单果摘放。整串任务的推荐总指令：

> Hold and reposition the tomato truss with the right hand. Pick all the cherry tomatoes one by one with the left hand and place them in the left basket. Finally, return the bare stem to the right plate.

训练副本默认选 **整串任务指令**：全部帧使用同一个总目标，粗/细标签和阶段序号独立保留。`meta/subtask_prompts.json`记录旧总prompt、新总prompt、阶段中英文及指令模式。不会改原包的task文本。normal与其speedup使用相同任务目标。

可另选 **阶段指令实验**：按当前段写英文任务文本，部署时需提供匹配阶段指令或配套调度/高层模型，不能以为给每帧换task_index就自动训练出阶段规划。官方OpenPI的prompt_from_task会把数据集task作为prompt；默认公开流程不能直接等同论文完整的高层预测训练。参考[官方配置](https://github.com/Physical-Intelligence/openpi/blob/main/src/openpi/training/config.py)、[官方README](https://github.com/Physical-Intelligence/openpi)、[论文](https://www.physicalintelligence.company/download/pi05.pdf)。
