# 拍摄时间裁剪修复与历史数据补缺

修复清洗按接收时间删除了仍对保留拍摄时刻有用的图像，导致内部对齐缺口的问题。默认使用 `image_time_basis=capture`，Wuji/Sharpa 共享同一实现。

## 新规则与保留项

- 静止区间仍由原v3双臂实测状态选择：100ms窗口、14关节范围≤1e-4rad、原生状态帧与边界保护，不增加旧2秒分级或手部否决。
- RGB、深度、触觉形变等 `sensor_msgs/msg/Image` 及 `CompressedImage`，按原始Header拍摄/采样时间是否位于静止区间决定删留；其他消息继续按bag记录时间。
- 保留消息使用原统一压缩映射，图像像素和力/力矩数值不变，不伪造采样时间或使用未来图像。
- 30Hz、150ms新鲜度、RGB/深度2点/250ms特殊容忍、整段连续规则、独立清洗审计和完整视频/来源校验保持。
- `trim_report.json`记录`image_time_basis=capture`和`selection_policy_version=image-capture-v1`。必要时可显式指定`image_time_basis: receive`使用旧选择规则。

## 为什么不重转已成功的历史数据

已成功的LeRobot及来源审计、内容身份保持有效，自动接收器首先根据既有发布索引/完成清单去重，不重新清洗或编码。既有配置字段、route/queue/batch所有权指纹不变，没有给配置对象注入新默认字段；新默认在执行层生效。24h/72h批次计时不重置。

新清洗job将`image-capture-v1`纳入job指纹，避免把旧默认接收策略的半成品当成新策略继续恢复。旧已完成发布/上传队列不依赖这个临时job指纹，不会失效。旧未完成job若直接`--resume`提示配置变更，应使用新的job目录；不得手动修改旧marker绕过。自动接收器在新的attempt目录重试。

可以在同一批次中保留旧合格包并追加新策略合格包；每包各自的原始裁剪报告留存。现有不可变Parquet/视频以硬链接继承，新包单独赋追加索引，旧包不重新编码、也不重新排序。

## 服务器只补 Wuji 内部缺口排除包

只读查看候选：

```bash
.venv/bin/python -m convest.ingest.repair --config configs/ingest.server.yaml
```

安装本版后在服务器原生终端执行：

```bash
.venv/bin/python -u -m convest.ingest.repair --config configs/ingest.server.yaml --apply
```

运行要求已有维护暂停。工具只选`wuji-A/B`、state=excluded、`continuous_export_range/full-all`且有内部无效区间的包，不选Sharpa、成功包、新录制包或其他排除原因。当前已知15条是A:23/31/50/60/66/71/87/91/122，B:49/58/59/80/109/128；实际以运行时读取的队列为准。

原生工具取得协调锁，正常SIGTERM等待接收器退出，再取得worker.lock。备份队列、保存候选；先核对原包快照和内容身份与排除时一致，再完整输入校验→新规则清洗→转换→完整来源/视频校验→正常不可变发布。真正缺失仍排除，原包变化则保留原记录并报告需要正常稳定观察。旧成功记录和所有训练artifact的inode、大小、mtime、SHA清单必须保持不变。提交后队列未更新的中断可从发布索引恢复，不重转。

最后只启动处于维护暂停的接收器，**不清除暂停、不恢复常规处理**。所有原始包保留。报告`code/operations/capture-time-backfill/reports/<ns>/summary.json`列出每条结果，`all_repaired`才表示全部候选修复成功；`completed`仅表示这次有界补缺执行结束。质量不通过的包不会强行发布。

服务器只有SSHFS访问时，不得直接在挂载客户端改SQLite或绕过原生锁；必须在服务器终端执行。异常时保持维护和停止状态，检查报告后恢复，不强杀、不删状态库。

## 原生源代码升级入口

开发端提交后运行`python scripts/build_capture_time_release.py --output <新目录>`，构建固定Git提交的source-only归档、SHA清单和`apply.sh`。不含.venv、Git、outputs或原生ingest.server.yaml。复制到服务器后，`bash apply.sh --prepare`验证；`bash apply.sh --apply`依次升级、定向补缺，并保留已有维护暂停。源文件替换使用既有graceful upgrader，备份被替换的源代码；数据/状态/挂载配置不替换。

## 已验证

真实Wuji A23/A122/B80由1/3/4个内部无效点变为0，分别1298/848/999帧；真实Sharpa A36为807帧、0个无效点，10路触觉形变和10路力/力矩通过。上述实测静止区间mapping完全不变，完整视频和独立来源比对均通过；两种机器人保留的图像消息像素/触觉内容逐条与原始副本核对通过。

新增测试覆盖拍摄/接收跨边界、像素不变、半开区间、缺失Header拒绝、显式旧模式、默认新策略、真原始深度缺失仍排除；补缺测试覆盖旧成功文件不变、只修排除包、幂等、保持维护、取消、原包变化拒绝、真实缺失仍排除、发布已提交时不重转以及旧job恢复隔离。
