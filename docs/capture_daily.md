# 采集端24小时批次、原始备份后释放与常驻服务

运行于 `Auto-convert-transfer-data` 分支的工作机，不修改服务器生产 `main` 服务、输入、队列或NVMe配置。本模式使用独立 `capture.daily.yaml`、状态/临时/本地批次目录，不复用旧按包试验队列。

## 工作顺序

1. 四路分别扫描新rosbag，观察稳定并确认已 finalized，完成完整输入体检。
2. 本地按既有规则裁剪、转换、独立来源对比、视频/数值校验和原始最终SHA-256核验。
3. 将合格数据安全加入该路的本地不可变LeRobot批次；来源和裁剪审计一同保留。此后释放清洗中间包和单包重复数据集，只保留批次数据与紧凑报告。
4. 优先上传原始rosbag。目标完整清单、大小和每个文件的SHA-256回读一致，并将远端完成凭据、本机SQLite及台账落盘后，才释放对应本机原始包。
5. 每路首包成功加入后累计24小时，完整校验并封存一套批次，排队上传。新数据进入下一批。即使没有下一包，轮询也会触发封存；重启或关机不会重置已保存的截止时间，恢复运行后补做已到期批次。

原始优先不会强行中断一个正在上传的批次；有封存批次等待时，上传调度避免被不断到来的新原始包无限推迟。24小时表示累计窗口，到期进入上传队列；网络/服务器存储拥塞可能延后完成时间。

Sharpa A、Sharpa B、Wuji A、Wuji B四路独立计时、独立数据集。沿用服务器批次机制，但采集端默认24小时，服务器现有72小时保持原状。按首包计时，不是每天固定零点；无数据的类别不生成空批次。

## 原始删除和恢复

只删除本次管线已完成本地质量验证、已安全加入本地批次、已在服务器完成独立回读核验并有持久凭据的那个原始目录。不会根据“文件复制结束”或“目录存在”就删除。

删除前持久记录意图、原始快照与隔离位置，核对目录inode和文件身份，再将原始目录原子移到同父路径的 `.capture-cleanup/<任务id>`，验证隔离内容后删除。删除过程中退出会保留意图，重启后先重新核验服务器备份，再只清理已登记的原始文件剩余部分。出现替换、变动、额外文件、软链接或冲突时停止并保留现场；原路径后来出现的新目录不参与旧隔离目录的删除。

同一路由的旧包号在释放后重新出现会标记冲突，不会删除新目录或覆盖服务器原始。不要覆盖复用原始episode目录名。

已上传的LeRobot本地批次暂时保留，不自动删除。原始备份失败/网络中断时原始仍在本机。磁盘不足时暂停转换并定期重试，不耗尽质量重试次数；该服务不控制录制器自身的存储写入。

## 台账和存底

本机 `outputs/capture-daily/state/`：

- `queue.sqlite3`：处理、上传、24小时批次、原始生命周期与追加事件日志，SQLite FULL同步。
- `recordings.csv`：可直接打开查看，UTF-8 BOM，包含四路包号、状态、身份、可读上传/删除时间、批次及远端位置。
- `recordings.json`：包含同样的记录和批次清单；原始纳秒时间也保留。
- `ledger_mirror.json`：最近成功同步台账的服务器位置及时间。
- `progress.json`、`upload_progress.json`、`status.json`：处理、上传和路由错误状态。

台账在发现/处理/上传/删除/封存等状态变化及轮询时更新。上传器将JSON/CSV存底到服务器 `data-lerobot/.capture-ledgers/<配置指纹>/<持久实例id>/`，重试台账同步不会重新转换或误报已经成功的原始上传。暂时无法同步时，SQLite和本机台账仍保留，之后补传。

原始逐包凭据：`origin-rosbag/.capture-transfer/<配置指纹>/<任务id>.json`。

24小时批次凭据：`data-lerobot/.capture-transfer/<配置指纹>/<任务id>.json`，包含精确来源包号清单、文件SHA-256和封存核验记录。两类凭据独立，原始删除不等待24小时批次上传。

## 配置与目标

`configs/capture.daily.yaml` 的四路源和两个服务器根目录均可修改，默认仍使用用户指定路径和 `zmx-testing`。`local_batch_root`、`work_root`、`state_root` 分离且位于工程outputs下。更换根目录、历史接入策略或其他指纹配置要使用新的状态目录；不要直接修改已初始化队列的身份。

- `delivery_mode: daily`
- `batch_hours: 24`
- `delete_raw_after_upload: true`：仅在上面的完整确认后删除；可显式设false保留。
- `cleanup_intermediates: true`：已安全提交本地批次后释放可重建工作副本。
- `initial_history: {mode: none}`：首次跳过已有包；以后新包持续接入。可在首次部署时明确改为all/include补处理所需历史。

目标批次类似 `data-lerobot/wuji/B/1003_1010_data_lerobot/`，内部episode编号连续从0开始，可能来源包号有缺号，真实纳入清单见台账与凭据，不以名称范围推断完整来源。

旧单包试验已上传的 `episode1003_data_lerobot` 等目录不移动或删除。原有历史样例不因启用新模式被回溯清理。正式换目标时需避免服务器原接收器再处理同一批原始。

## 常驻运行

```bash
cd /home/user/convert_data_job-worker
.venv/bin/python scripts/install_capture_service.py --config configs/capture.daily.yaml --apply
systemctl --user status capture-convert-transfer.service
journalctl --user -u capture-convert-transfer.service -f
.venv/bin/python -m convest.capture.cli status --config configs/capture.daily.yaml
```

服务由当前用户运行，启用default.target开机启动及用户linger，退出登录后仍可运行，不需要以root运行数据处理。异常自动重启，队列/计时从磁盘恢复。正常停止会等当前本地处理/上传完成，不设置强杀超时。

```bash
systemctl --user stop capture-convert-transfer.service
systemctl --user start capture-convert-transfer.service
systemctl --user disable --now capture-convert-transfer.service
```

持续运行需工作机保持开机且不休眠；关机/休眠期间不处理，恢复后接续。挂载由原有SSHFS连接管理；未挂载时上传等待，不能写到普通本地目录冒充服务器。修改程序分支或配置前先正常停止本机服务，确认后再启动；服务器服务不受这些命令控制。

## 验收

合成测试覆盖四路各两包合批、24小时边界、空闲封存、重启计时不变、原始备份确认后释放、删除中断恢复、同名新目录保护、目标损坏拒绝删除、封存失败拒绝上传、空间不足暂停和台账镜像恢复。

真实验收使用原始样例的独立本地副本，不删除用户给定的原始样例。为了当次验证封存上传，只有独立试验调用维护函数时的now_ns被推进到已记录24小时截止之后；不修改系统时钟或正式服务24小时窗口，不能据此宣称已经实跑满24小时。结果见 `capture_daily_validation.json`，日志在outputs。
