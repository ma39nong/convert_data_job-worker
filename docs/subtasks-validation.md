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
