# 采集电脑本地处理与上传

本功能仅位于 `Auto-convert-transfer-data` 分支。处理和上传发生在采集电脑，服务器仅接收 `zmx-testing` 下的数据；未更改服务器 `main` 服务、生产输入/输出、队列或 NVMe 挂载。

## 路径和运行方式

复制 `configs/capture.example.yaml` 为自己的配置，修改四条 `routes[].source`、`remote_raw_root`、`remote_dataset_root` 和 `require_mount`。路径支持绝对路径，项目内部路径以工程根目录解析。state/work 必须分别位于工程 `outputs/` 下且不重叠；输入、state、work及两个上传目标之间均不允许重叠。配置与队列所有权指纹绑定，更换这些设置必须使用新的 state/work，不能把旧上传记录冒充新目标的完成状态。

默认 `require_mount` 指向 SSHFS 挂载点 `/home/user/company-remote-2`；未挂载时上传进入重试，不能回落到普通本地目录。两个远端目标根目录需先存在；路由子目录由上传器按需创建。当前传输利用现有挂载，不需要服务器新进程或服务。网络连接、认证和挂载恢复由现有挂载配置负责，本工具恢复后会重试。

```bash
# 以下均从工程目录执行，不使用 sudo。
.venv/bin/python -m convest.capture.cli check-config --config configs/capture.testing.yaml
.venv/bin/python -u -m convest.capture.cli run --config configs/capture.testing.yaml --until-idle
.venv/bin/python -m convest.capture.cli status --config configs/capture.testing.yaml
```

`check-config` 只读校验配置，不创建状态或传输数据。`status` 只读查看状态。`once` 做一次本地扫描，独立执行时不会自动上传；`upload-once` 尝试一项到期上传。`run` 启动处理循环及独立上传子进程，两者各自持锁，通常每次一个转换、一个上传，可以互相重叠。离线上传重试不会占用本地处理锁。

`--until-idle` 供有限样例试验使用，等当前已发现项目完成本地处理和上传后退出，不作为持续采集模式。若本地出现 failed/conflict，返回非零；缺失录制状态的包继续等待；上传重试没有任意丢弃次数，遇到断网会持续等待恢复。`run` 不带该参数时持续发现新包。Ctrl+C/SIGTERM 等当前本地任务/传输到安全边界后结束，不强杀、不回滚已发布的数据。

## 接入与质量

每路 `initial_history` 必须显式选择 `none`（初次扫描已有包跳过）、`all`（补处理全部已有包）或 `include` 加指定 `episodes`。策略与首次跳过清单持久保存；以后新增的 `episode数字` 目录仍自动发现，重启不会把历史重新当新包。试验配置只接入已存在的 Sharpa A36/B55、Wuji A995/B1003 四条，其余已有包跳过。

输入必须已 finalized，并满足采集状态、metadata、SQLite、消息数量和必需流检查。默认跨独立扫描观察至少3次且稳定60秒，不能以目录出现或下一包出现代替检查。原始只读；用既有双臂裁剪、30Hz对齐和 LeRobot v2.1 转换，保持来源对比、完整视频解码、无损深度/触觉和最终原始SHA-256校验。

本地成功状态为 `converted`，表示已生成合格数据并进入上传队列，不表示上传成功。质量排除为 `excluded`，保留报告，不将其转换输出或原始包加入这条合格配对上传链路。检查未通过为 `waiting_input`，到期再检查；本地执行异常按次数重试，达到上限为 `failed`。已转换原始发生变化为 `conflict`，保留既有输出，不覆盖服务器上同名数据。

本地 job 完成凭据与上传队列可在进程重启后协调恢复。已转换的数据不会因网络失败重做清洗转换。上传队列持久保存在本机 SQLite，包含原始和数据集各自完成标记。远端同名内容冲突或原始变化为上传 `conflict`，不覆盖、不盲目重试，其余上传继续；有限试验遇到这种冲突返回非零。

## 上传布局与提交语义

第一版每条原始录制对应独立 LeRobot 数据集，内部 episode 编号按原转换器从0开始，不混合机器人或A/B。

```text
origin-rosbag/sharpa/A/episode36/            # 原始文件，未增加或改写内容
data-lerobot/sharpa/A/episode36_data_lerobot/ # 完整独立 LeRobot，含 meta/data/videos/conversion
```

另外三路同理。每路目录下 `.uploading/<任务id>` 用于未完成传输，最终 `episode...` 目录只在完整清单及回读SHA-256通过后，以同一文件系统内目录rename发布。每个文件写入并刷盘后，最终清单核验对全部目标文件完整读取一次SHA-256，不重复读取同一目标文件。失败重试会校验已传前缀并续传部分文件；损坏的暂存文件可重新写入，已存在的正式目标不会覆盖或删除，内容冲突保留并报错。

原始与数据集的两个目录无法跨目录同时原子提交：可能先看到原始目录而数据集尚未到达。二者都成功后，在下面写配对完成凭据：

```text
data-lerobot/.capture-transfer/<配置指纹>/<任务id>.json
```

只有这个凭据和本地上传 `completed` 才代表整对数据完成。凭据记录来源identity、两个目标及所有文件的大小和SHA-256。需要消费成对数据的下游应据此接入，不能仅以目录出现判断整对完成。该凭据不能证明后续人为更改后的数据仍完好，需要时可再按清单核验。

上传原始文件和已验证的数据集清单均固定。源变动、错误文件或读回哈希不符会阻止提交。网络错误按退避重试；不会因此重复转换。对已存在且内容完全相同的目录可核验后恢复本地成功状态，应对rename成功后本地进程中断的情况。

## 存储与排查

本机原始包、成功的 LeRobot、清洗中间包、失败尝试和报告当前均保留，不自动删除。持续运行需监控本地空间；空间不足会报错，不降低质量门槛。后续可以在明确保留策略后增加清理。

`outputs/capture-testing/state/` 保存队列、`progress.json`、`upload_progress.json`、`status.json`；`work/<路由>/<episode>/<内容identity>/` 保存本地处理尝试和 `local_complete.json`。处理与上传进度分开，上传慢时应查看 upload_progress 和 uploads，不能把本地 converted 当上传完成。上传进度中的 `operation: flushing` 表示等待刷盘，`hashing` 表示回读校验；字节写满后还需要等待核验完成。

本次真实试验日志在 `outputs/capture_real_trial.log`，进程信息在 `outputs/capture_trial_process.json`；验证记录在 `docs/capture_validation.json`（试验完成后生成）。合成测试日志 `outputs/capture_tests.log`，完整回归日志 `outputs/capture_regression.log`。

## 只读验收

四条上传完成后执行：

```bash
.venv/bin/python scripts/validate_capture_trial.py --config configs/capture.testing.yaml \
  --report outputs/capture_acceptance.json
```

它检查本地完成状态、原始快照未变、四路配对凭据、上传清单、目标文件大小和远端携带的本地完整质量报告。上传器已经在提交前实际回读每个远端文件并核对SHA-256。若要在验收时再次独立读取所有远端字节校验，可加 `--rehash`；这会额外从服务器读取十余GB。验收报告明确记录是否进行了额外重复哈希，不能把默认验收当成再次全量读取。
