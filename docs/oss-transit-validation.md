# GUI 训练数据 OSS 中转验收（2026-10-29）

## 交付与环境

工作机 → 训练 SSH 目标上的 OSS 挂载 → CPFS datasets，按相同相对目录交付 normal、speedup 和配对审计。最终全文件校验、刷盘发布、持久化回读凭据后，自动精确删除该批中转对象。202 备份、转换规则、24h 封存、最终批次名与本机释放条件保持原规则。

现场使用既有端口 28 的 SSH 认证；`/mnt/public_data` 为 `fuse.ossfs2`，`/mnt/data` 为 NFS 上的 CPFS。公共根本身含其他项目，只允许操作自身持久化意图所列批次。没有安装 OSS 客户端、写云密钥或执行指南安装脚本，也没有要求人工 DataFlow Import。

初次受控上传发现挂载不支持 rsync 的 `ftruncate`（errno 95）；已改为有界内存的 SSH 顺序文件流。中转写完关闭并回读校验，重试跳过已完整文件，半文件从头传；采用写入超时和 SSH 存活检查。最终 CPFS 仍使用既有精确清单、全 SHA256、fsync、原子发布协议。锁、归属、清理意图与凭据存放 CPFS，不依赖 OSS 支持 flock/fsync/rename。

## 自动化结果

命令：

```bash
.venv/bin/python -m pytest tests/test_gui.py tests/test_gui_oss.py tests/test_capture_daily.py tests/test_training_pipeline.py -q
node --test tests/test_gui_record_display.cjs
node --check src/convest/gui/static/app.js
```

Python 54 项通过；Node 2 项通过。覆盖真实小型 ROSbag 原生转换、A/B 分流、MMDD 命名、normal/speedup 经中转发布与配对材料、首次 speedup 失败后重试、本机释放原条件、队列指纹兼容、已有目标复用，以及以下故障注入：

- 中转内容损坏、未知文件/空目录、软链接及非本次目录：拒绝发布或清理，已有数据保留。
- 最终路径同名内容冲突：保留双方数据。
- 最终凭据写入失败：保留中转，恢复后验证最终副本再清理。
- 第一次中转删除后断线：恢复清理余下匹配对象，无需再次上传。
- 文件流中断：已完整对象可复用，未完成对象重传。
- OSS 或 CPFS 挂载缺失、路径逃逸/根重叠：报错，不回退直传。
- 交付策略升级：采集和训练指纹、运行版本保持一致。

## 真实挂载验证

使用独立隐藏场景 `.workflow-oss-acceptance-20261029-*`，在 tomato_sharpa 的 normal/speedup 各发送约 1 MiB 合成二进制和一个 JSON（非业务录制）。两版均完成顺序上传、中转回读、CPFS 全 SHA256、最终持久凭据、批次对象自动清理。对每版重复提交验证，复用已有最终副本，不重传、不重建中转。首次兼容性修复和最终传输超时处理后分别完成受控验证。

验收过程和清单记录在本机 `outputs/gui/oss-transit-20261029/`。验证后只移除本次唯一隐藏测试场景的最终测试文件、测试审计和中转空父目录；没有清空共享根或操作业务批次。两个 GUI 任务保持原停止/禁用状态，仅重启 HTTP 服务加载交付策略，保留登录校验 token 和全部任务设置。界面已确认中转说明显示正常，无浏览器 warn/error。

## 范围与边界

本次没有启动采集任务或改变任何历史独立作业。此交付策略接入 GUI；旧命令行服务保持原上传方式。完成了小样本真实挂载和自动故障测试，未声称已完成真实 TB 级吞吐、机器断电或对象存储挂载内部的云端持久化压力验收。最终安全判断基于挂载关闭/回读以及 CPFS 精确清单和全 SHA256；OSS 云侧凭据与生命周期策略仍由现有挂载管理方维护。
