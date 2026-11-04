# GUI A+B 合计100条封包及训练短名称验收（2026-11-04）

用户确认：同一任务 A+B 合计100条；不足继续累计；日期取达到满批、准备封包上传当天的北京时间MMDD；新训练目录只以normal/speedup结尾。

## 实现与兼容

- GUI在原capture配置身份加载后注入batch_group_size=100，保留既有采集/训练队列和批次容器身份；旧CLI24h/服务器72h不变。
- 共同锁覆盖每次入批前后边界检查。只计成功提交的来源录制，A/B来源编号可重复但独立存储；质量排除/失败/重复扫描不计新增。
- 满100条先持久化共同成员、实际来源清单、不可变generation和准备日期，再各自完整SHA及视频解码封存。所有非空成员验证完成后才写group_ready，完整上传与训练发现均受此门控。缺一个组时仅封非空组，不创建空批次。
- 断电/中断后优先恢复未完成共同封包，冻结日期和成员；第101条进入下一轮。既有未封批次转为计数累计，已封批次不改。历史未封来源超额时保留并报错，不静默拆分或吞掉计数。
- 新训练payload明确固定variant-short-v1后生成MMDD_N_M_normal/speedup；旧local_ready/upload_retry/uploading payload无此标记继续旧目标，避免重传改名。202完整包仍为MMDD_N_M_data_lerobot。
- 界面去掉24h倒计时，显示本轮A+B N/100及缺口，路径预览与OSS模板使用短名称；现有历史记录不伪造新批次标签。

## 验证

```bash
.venv/bin/python -m pytest tests/test_capture_group_batches.py tests/test_ingest_batches.py tests/test_capture_daily.py tests/test_gui.py tests/test_gui_oss.py tests/test_training_pipeline.py -q
node --test tests/test_gui_record_display.cjs
```

完整相关Python回归78项通过，Node5项通过；进一步加强共同成员/日期一致性保护后，20项相关定向回归通过。实际100条测试没有把阈值缩小：101份本地合成ROSbag执行真实稳定性、质量、清洗转换及不可变入批。A60+B39时保持99条，即使推进等待时间也不封；重开队列后补B第40条，A60+B40共同封包且同日期，各组完整校验通过；A第61条进入新轮，计数1。

另以小阈值验证双组封包中断恢复、另一组未通过时禁止训练发现和完整上传、旧24h未封批接续/已封批保留、封包日期冻结、质量排除不计数、单组输入。GUI真实原生转换→完整包→normal/speedup→本地模拟OSS/CPFS协议→配对凭据流程验证短名称、SHA及上传失败恢复；本机完整LeRobot释放条件在新旧命名下均通过。历史跨午夜测试固定其测试时钟，避免运行日超过旧测试的72h边界导致误判。

浏览器核对100条提示、normal/speedup无lerobot后缀、现有3条未封来源显示3/100，无console warn/error。截图和本机验收概要位于outputs/gui/count-batches-20261104。仅重启HTTP加载新runtime策略，保留token、设置、停止/禁用状态；未启动现有退休测试任务，未写远端目录或上传业务/测试数据。本次未验证连续生产100条的真实网络吞吐。
