# 正式服务长期使用 NVMe 临时工作区

## 方式和边界

原始 rosbag、真实队列、最终 LeRobot 数据、质量规则及生产代码和配置均沿用现状。用 systemd 原生 bind mount 将现有 work_root 的实际存储放到 NVMe；保留逻辑路径和所有权指纹，避免软链接被 resolve 后违反工作区安全约束。串行处理，不增加并发。

NVMe实际目录：`/data1/auto-clean-convert-trials/user/production-work`。已有试验文件不移动、不删除。

脚本安装一个 .mount unit 和服务的 `40-nvme-work.conf` drop-in。服务依赖挂载，启动前检查该目录确为挂载点且与预定 NVMe 目录为同一目录。服务下次启动/重启系统后依然使用此挂载；NVMe挂载失败会阻止服务启动，不静默退回原盘。挂载单元依赖 NVMe 原挂载，挂载点的父挂载依赖由 systemd 自动处理。

参考：[systemd.mount](https://www.freedesktop.org/software/systemd/man/latest/systemd.mount.html)。

## 在服务器终端执行

先让上一轮试验入口完整退出，包含恢复服务；未退出则共享协调锁阻止新切换。不要 sudo 整个脚本，sudo 仅用于安装系统单元和正常停启。

```bash
bash /data/data-fruits/real/harvest-tomato/code/operations/nvme-permanent/run.sh --check
bash /data/data-fruits/real/harvest-tomato/code/operations/nvme-permanent/run.sh --apply
```

`--check` 只读，不创建目录或改变维护/服务；未绑定时会显示 `bound_to_nvme: false`。

`--apply` 先等正在输入检查的包进入处理，再请求维护并仅向主进程发 SIGTERM，等当前包发布清理、服务成功退出。获取真实 worker.lock，确认没有活动队列项，且原盘临时目录只剩正确 owner.json，才安装挂载。已有残留工作、所有权不符、无关维护或冲突系统单元一律保留并报错。挂载后检查实际目录相同，再恢复自己的维护请求并启动服务。已发布包仍在原队列，不重做。

如果脚本输出等待当前包，保持终端开启。中途 Ctrl+C 等待安全边界；系统单元安装部分失败后可能保留维护和停止状态，报告明确记录，需根据日志排查，不能直接强制启动或删除工作文件。

报告位置 `code/operations/nvme-permanent/reports/<时间戳>/summary.json`，成功应 `status: passed, bound_to_nvme: true`，并有 active 服务状态。该状态只证明启动当时，后续进度应另读 progress/队列。

## 退回 /data

```bash
bash /data/data-fruits/real/harvest-tomato/code/operations/nvme-permanent/run.sh --rollback
```

同样先等当前包完整结束，检查正确且空闲的绑定，再卸载并只移除内容与本工具完全一致的两份单元文件，恢复原有 /data 目录启动服务。不删除 NVMe 数据，不编辑原服务文件。

## 时间与验证限制

已有真实NVMe排队测试 Wuji/B11 10:44、27 11:17、40 12:18，均正式发布。后两条均值11:47；此计时不含常规扫描、等待切换。原始输入检查和最终发布仍在 /data，性能仍随录制大小及共享存储负载变化，不保证固定耗时。

新增测试覆盖残留工作保留、冲突系统文件不覆盖、目录标记/所有者/软链接拒绝，以及安装和回退顺序。挂载单元已通过本地 systemd-analyze verify。单元测试模拟挂载操作；尚未在服务器实际执行 bind mount，真实挂载及服务恢复需以原生 --apply 报告为准。
