# 本地24小时LeRobot的normal/speedup与训练服务器上传

本功能接在已有采集工作机的24小时封存批次之后。完整LeRobot上传data服务器、原始rosbag备份/核验后释放流程继续独立执行；新服务不修改其代码、队列、时钟或清洗规则，不依赖data服务器上传先结束。

## 数据定义

| 内容 | normal | speedup |
|---|---|---|
| Sharpa state/action | 原生116/58，名称/顺序/数值不变 | 原生116/58，按各自原生通道重采样 |
| Wuji state/action | 原生108/54，名称/顺序/数值不变 | 原生108/54，按各自原生通道重采样 |
| 轨迹数 | 全部保留 | 全部保留 |
| 帧数 | 全部保留 | 按平滑1–3倍率曲线减少 |
| RGB | 原分辨率、原视频文件字节 | 最近源帧，原分辨率、30Hz，H264 CRF18、最大GOP 30帧重编码 |
| 深度/触觉/额外audit数组 | 不带入派生版，完整版本仍保留 | 不带入 |
| engaged | 记录中的双臂标志原样保留 | 最近观测源帧，不合成手部标志 |
| 统计 | 对派生字段重算 | 全部数值及视频统计重算 |

normal不从旧Wuji数据集复制schema，不重命名Sharpa关节，也不删除thumb_MCP_AA/pinky_CMC等维度。维度、名称、顺序和每行值会与本批次完整数据逐项比较。采用用户提供的AdaTempo原型，并将读取/校验适配为原生手型；来源哈希及修改说明在normal_speedup_provenance.json。

AdaTempo按同一批次、同一机器人/组的关节命令估计相位和节奏，生成虚拟时间映射。连续state在当前虚拟时刻插值，action为下一虚拟时刻的记录命令插值；图像、标志及int64 source_timestamp_ns取最近观测源帧。它是派生训练轨迹的时间重采样，不是上游rosbag裁剪，不受上游ZOH禁止插值的约束。记录velocity仅作为观测插值，不乘加速倍率，也不等于机器人新实测速度。

轨迹首尾附近倍率保护为1；最后一个源时刻用于下一步action，所以即使倍率为1，输出观测行通常也是原长度减1。支持上游最短2帧episode（输出1帧），不丢弃整条轨迹。每条轨迹保存source_index、action_source_index和倍率曲线；全表数值映射、metadata、stats及全部视频帧数/PTS校验通过才发布。RGB二次编码有损，未进行训练或机器人实测效果验证。

## 部署与路径

使用configs/training.example.yaml复制成configs/training.local.yaml。后者被Git忽略。

| 字段 | 含义 |
|---|---|
| capture_project | 现有24小时采集工程的路径；缺省为当前checkout |
| capture_config | 采集配置路径；相对路径按capture_project解析 |
| state_root/output_root | 新服务自身目录，位于当前工程outputs下，与所有上游目录分开 |
| workers | 视频并发1–4，默认1；本地后处理一次只加工一个批次 |
| cmax | AdaTempo最大倍率1–3，默认3 |
| min_free_gib | 除预估两版视频空间之外的最低余量，默认8GiB |
| training_target.transport | ssh或mount |
| training_target.dataset_root | 目标根，末两级必须为workspace/data；程序追加robot/group/batch/version |

SSH示例（服务器实际目录）：

```yaml
training_target:
  transport: ssh
  host: 47.116.73.163
  port: 28
  user: mengxiang
  dataset_root: /home/mengxiang/workspace/data
  python: python3
  rsync: rsync
```

读取已有~/.ssh/config、密钥和known_hosts，使用BatchMode和严格主机检查；不保存密码、不复制私钥。host也可填SSH别名，未配置user/port时使用SSH配置。服务器需要可调用的Python3标准库和rsync；命令不在非交互PATH时，分别填写python/rsync的远端绝对可执行路径。

**个人目录与登录账号单独配置。** mengxiang可替换为tairan、junyu、zhiwei等实际目录名，根结构始终是workspace/data，下级按sharpa或wuji区分。服务器该根目录应先存在且可写，不在拼错的路径创建替代根。若已有/home个人目录是指向/mnt/data的别名，固定解析后的真实根再操作其内部目录。

mount示例：将服务器个人目录挂到本机/home/user/training-server后：

```yaml
training_target:
  transport: mount
  require_mount: /home/user/training-server
  dataset_root: /home/user/training-server/workspace/data
```

mount模式填本地挂载后的路径，不能直接填未挂载的服务器/home目录；缺挂载即等待，拒绝本地fallback。data服务器和训练服务器是两个目标，分别配置。

目录结构：

```text
workspace/data/{sharpa,wuji}/{A,B}/
  36_68_data_lerobot--batch-000001--<唯一版本标识>/
    normal/                 # LeRobot数据集根，训练时选择这里
    speedup/                # LeRobot数据集根，或选择这里
    source.json             # 完整输入清单/封存版本
    normal.complete.json
    speedup.complete.json
    pipeline.json
```

批次父目录不是LeRobot根。版本标识包含来源配置、generation及代码配方，避免不同生产者、同号范围或算法版本互相覆盖。结果与源都保留，后处理不删除任何原始或完整LeRobot，不删除已上传派生版、失败暂存或历史版本。空间回收另定策略。

## 运行与恢复

```bash
.venv/bin/python -m convest.training.cli check-config --config configs/training.local.yaml
.venv/bin/python -m convest.training.cli once --config configs/training.local.yaml
.venv/bin/python -m convest.training.cli upload-once --config configs/training.local.yaml
.venv/bin/python -m convest.training.cli run --config configs/training.local.yaml
```

check-config只读加载配置；once会发现封存批次并实际加工最多一批；upload-once实际上传最多一对；run持续加工并启动独立上传子进程。process/upload可分别持续运行，角色锁防重复。status不打开写数据库。

推荐用scripts/install_training_service.py预览并--apply启动用户服务capture-normal-speedup.service。与原capture-convert-transfer.service并存，处理和上传均串行。新服务Nice10、CPUQuota200%、MemoryHigh4GiB/MemoryMax8GiB及单视频worker限制资源；大量/长轨迹DTW仍可能占较多内存，超限失败暂存保留，不能为通过而绕开校验。

服务SIGTERM等待当前加工和上传完成。退出/断电重启后，已完整校验的normal可复用，半成品speedup移到.failed保留后重做；整对结果本地提交后，上传失败不重做加工。上传走.uploading，逐文件远端SHA256独立核对并刷盘后，才将normal/speedup整对原子发布。完成凭据在训练目标根.training-transfer下；目标已有不同内容则conflict，不覆盖。SSH传输大清单走stdin，避免24小时大批次触发命令行长度限制。

state_root含queue.sqlite3、discovery.json、progress.json及upload_progress.json。新配置指纹绑定状态/输出，改目标身份须新建独立state/output，保留旧队列；代码配方更新会产生新版本。改动本服务代码前先正常停止服务并等待当前包，避免试验中混用代码版本。

训练使用固定normal或speedup目录，并由原生Sharpa/Wuji适配器读取对应维度；对所选版本重新计算norm_stats。上传完成不自动启动训练。

## 验收

tests/test_training_pipeline.py覆盖四路原始数据备份及整批上传兼容、封存阻断、原生维度/名称/数值/RGB字节、AdaTempo全量映射与视频、失败恢复/不重算、目标冲突、远端哈希与路径保护、锁及所有权。scripts/validate_training_trial.py可复制已完成且来源校验通过的真实单包job，建立独立24小时试验批次，受控推进试验的now_ns后封存，使用正常CLI加工/可选SSH上传。该验收不改系统时钟或上游状态，不等于自然运行满24小时，也不启动训练。

## 已有服务器历史批次的一次性处理

`scripts/process_historical_training.py`用于用户明确选定的历史批次，不接入或改写常规24小时队列，也不把服务器仍为active的批次伪标记为sealed。它固定当前ready generation，并要求该generation已有完整输出校验、帧数/轨迹数及来源artifact清单。上游目录和批次状态均只读。

此入口针对**训练服务器已保存同批次完整副本**的情况：在训练服务器只做原始完整副本的全文件SHA256核验，同时核对data服务器保留的Parquet列数据块及footer与已验证副本一致。深度、触觉等弃用大列无需搬到工作机；normal字段筛选、所有保留数值逐行比对、RGB哈希/完整解码、AdaTempo及全部输出验证仍在工作机执行。训练服务器不承担转换或编码计算。缺少或不匹配的完整副本会报错，不降低校验要求继续处理。

```bash
.venv/bin/python scripts/process_historical_training.py \
  --filesystem-root /path/to/data-server-sftp-mount \
  --source /server/path/to/data/data-lerobot/sharpa \
  --groups A B \
  --config configs/training.local.yaml \
  --upload-workers 4
```

`filesystem-root`是服务器文件系统根在本机的挂载位置，`source`是服务器上的绝对Sharpa根路径；两者拼接后可读取A/B目录及其原生generation。当前一次最多选择每组的一个历史批次，多批次时拒绝隐式混合。

结果位于`outputs/historical-sharpa/`，progress.json记录阶段，report.json保存最终结果。该入口可恢复已验证normal/speedup，重复上传保持完成清单字节不变；不会因重跑改写完成时间而产生目标冲突。输出使用与常规任务相同的机器人/A或B/批次版本/normal与speedup层级，独立版本标识避免覆盖完整数据。

上传前可在训练服务器暂存区复制哈希完全相同的normal视频，不硬链接或修改原完整数据。其余文件用1–8条互不重叠的rsync文件列表并行传输；全部结束后仍由同一个远端完整清单/SHA256校验及原子发布入口提交。上传失败只重试传输，不重复编码；不自动删除原数据、完整副本或本机已验证派生结果。

视频帧率与 GOP 分开设置：speedup 输出仍为 30 fps，显式限制最大关键帧间隔为 30 帧（约 1 秒），避免采用 libx264 默认的 250 帧；场景切换时允许提前插入关键帧。此设置应用于后续新编码的视频，已有成品不会自动重新编码。
