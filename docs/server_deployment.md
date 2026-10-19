# 服务器部署准备

本文是后续部署步骤，不会自动连接服务器、安装服务或启动处理。本地实现与测试通过后，再在获准部署的服务器上执行；现有批处理、旧输出和原始包不受本次本地开发影响。

## 路径与运行账户

工程应完整复制到服务器本机目录，例如 `/opt/convert-data-job-worker`，包括 `src/`、`schemas/`、`configs/`、`deploy/` 和 `pyproject.toml`。不要搬运工作站的 `.venv/`、`outputs/` 或原始数据作为部署依赖。安装依赖及编译产物由服务器环境生成。

`/home/user/company-remote-2/real/harvest-tomato` 是工作站上的挂载入口；配置必须填服务器上该数据的本机真实目录，不能凭挂载路径推测。配置的 `data_root` 下应已有四个 `origin-data/{sharpa,wuji}/{A,B}` 输入目录，输出为四个 `data-lerobot/{sharpa,wuji}/{A,B}` 批次父目录。

运行账户需要输入的目录遍历/只读权限，以及工程 `outputs/`、输出批次父目录的写权限。检查本机可用空间、硬链接和输出指针原子替换行为。输入目录不需要写权限，接收器不依赖上传器脚本、回调、完成标记或临时目录布局。

## 安装和正式配置

在服务器工程目录中建立独立 Python 虚拟环境，项目要求 Python >=3.11；当前本地验证环境为 Python 3.12。激活后以 editable 方式安装，确保相对配置、schema 和工作区路径仍以工程目录解析：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[conversion]'
cp configs/ingest.server.example.yaml configs/ingest.server.yaml
```

编辑实际配置中的 `data_root`。服务器模板已设置：

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

验收通过后编辑 `deploy/ingest-worker.service.example` 中的账户、工程路径和实际配置路径，将其作为系统服务安装。模板本身不会安装或启用。启用后可用以下方式观察：

```bash
systemctl status ingest-worker
journalctl -u ingest-worker -f
.venv/bin/python -m convest.ingest.cli status --config configs/ingest.server.yaml
```

常驻进程会扫描已有包、处理就绪包并持续接收新包。`status` 同时列出队列、失败原因、各路由批次名称/完整路径、计时、实际来源清单和封存校验状态。活动批可预览；交付选择 `state: sealed` 且验证通过的范围目录，解析该目录后上传整套数据集，而不是只上传符号链接，也不是上传整个 A/B 父目录。

服务重启保留队列、跨批来源去重和 72 小时起点；不要删除这些状态以重新跑历史数据。历史内部版本暂不自动删除，部署前应确定容量监控和保留策略。完整处理规则见 [自动接收说明](automatic_ingestion.md)。
