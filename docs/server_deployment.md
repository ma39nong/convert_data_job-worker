# 服务器部署准备

本文是后续部署步骤，不会自动连接服务器、安装服务或启动处理。本地实现与测试通过后，再在获准部署的服务器上执行；现有批处理、旧输出和原始包不受本次本地开发影响。

## 路径与运行账户

工程目录为 `/data/data-fruits/real/harvest-tomato/code/auto-clean-convert`，包括源码、schema、配置、服务与安装脚本。部署包可附带 Linux x86_64 的独立 CPython 3.12.11（`runtime/python/`）和 Python 3.11/3.12 的离线 wheel（`deploy/wheels/`）。这些发行依赖不进入 Git；版本及每个文件的哈希记录在部署包的 `DEPLOYMENT.json`。不要搬运工作站现有 `.venv/`、原始数据或训练环境。

运行时、服务器原生生成的 `.venv/`、队列/暂存 `outputs/` 都在该工程目录内。训练工程的 Torch/JAX 环境不属于接收器依赖，不需要装到数据服务器来做转换。

`/home/user/company-remote-2/real/harvest-tomato` 是工作站上的挂载入口；配置必须填服务器上该数据的本机真实目录，挂载源已确认服务器本机路径为 `/data/data-fruits/real/harvest-tomato`，实际 `data_root` 为该目录下的 `data/`。配置的 `data_root` 下应已有四个 `origin-rosbag/{sharpa,wuji}/{A,B}` 输入目录，输出为四个 `data-lerobot/{sharpa,wuji}/{A,B}` 批次父目录。

运行账户需要输入的目录遍历/只读权限，以及工程 `outputs/`、输出批次父目录的写权限。检查本机可用空间、硬链接和输出指针原子替换行为。输入目录不需要写权限，接收器不依赖上传器脚本、回调、完成标记或临时目录布局。

## 安装和正式配置

在**服务器本机终端**运行下面的准备命令。它优先使用包内独立 Python，也支持 `PYTHON_BIN` 指定本机 Python 3.11/3.12；部署依赖版本见 `deploy/requirements-server.txt`。wheel 目录存在时安装不访问 PyPI，不复制本机虚拟环境。脚本会检查依赖、保存实际 `runtime.freeze.txt`、保留已有配置、只读检查四个输入根目录，并拒绝在正在运行的同名服务上重装：

```bash
cd /data/data-fruits/real/harvest-tomato/code/auto-clean-convert
bash deploy/setup_server.sh --prepare
```

`--prepare` 不创建接收队列，不处理原始包，也不启动服务。脚本拒绝从工作站的 SSHFS 挂载执行，以免在错误机器上创建环境。挂载只提供文件读写，安装/启动需要服务器终端或可执行命令的 SSH 登录。

服务器配置的 `data_root` 为 `/data/data-fruits/real/harvest-tomato/data`；实际配置不存在时从模板创建，已存在时不覆盖。其余默认设置：

```yaml
initial_history: {mode: all}
batch_hours: 72
poll_seconds: 10
stable_scans: 3
stable_seconds: 60
```

这表示四条路由中已有包和后续包均接收，先观察和体检，通过后才清洗转换；并不绕过录制状态和质量排除。正式第一次启动前审阅该范围。`once` 和 `run` 都会持久化首次历史选择并可能开始处理，不要把它们当作只读体检命令。只读配置检查使用：

```bash
.venv/bin/python -m convest.ingest.cli check-config --config configs/ingest.server.yaml
```

模板使用 `outputs/ingest-server-batches/` 下的新状态和暂存目录。旧单数据集布局配置与本版指纹不同，不能直接复用旧状态目录，也不自动迁移旧的 `clean-data-lerobot` 输出；旧服务和批处理不能与新服务争用同一输出。

## 验收与启用

先使用独立的验收配置、队列和输出处理一个真实包，核查裁剪效果、RGB、深度、触觉、独立来源审计和 LeRobot 校验。验收配置不能与正式队列共用路径，不能先在正式队列用 `include` 测试、再改成 `all` 期待自动扩大历史范围。分批到期、去重和恢复另有本地测试覆盖，不需要在服务器等待 72 小时才能开始验收。

验收通过后在服务器本机执行启动命令，需要安装系统服务的 sudo 权限。模板已按 `user` 账户和上述工程路径设置；该命令安装并启用独立的 `auto-clean-convert.service`，不会停止其他服务或批处理。启用后按下面的命令观察：

```bash
bash deploy/setup_server.sh --start
systemctl status auto-clean-convert.service
journalctl -u auto-clean-convert.service -f
.venv/bin/python -m convest.ingest.cli status --config configs/ingest.server.yaml
```

常驻进程会扫描已有包、处理就绪包并持续接收新包。`status` 同时列出队列、失败原因、各路由批次名称/完整路径、计时、实际来源清单和封存校验状态。活动批可预览；交付选择 `state: sealed` 且验证通过的范围目录，解析该目录后上传整套数据集，而不是只上传符号链接，也不是上传整个 A/B 父目录。

服务重启保留队列、跨批来源去重和 72 小时起点；不要删除这些状态以重新跑历史数据。历史内部版本暂不自动删除，部署前应确定容量监控和保留策略。完整处理规则见 [自动接收说明](automatic_ingestion.md)。
