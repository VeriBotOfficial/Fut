# test.4 正常完赛被判弃赛与个人统计调查

范围：只修改 efootxi 测试版。用户于 2026-10-17 确认 test.3 在 AI 组别正常踢到终场，仍显示放弃落败、个人统计为 0。本轮没有该手机的原始结算请求，不把静态分析或主机测试写成手机实测。

## 本轮新增证据与根因

test.3 已经把回复里的结束字符串改为原生支持的 `NORMAL_END / NOERR`，但**生成回复之前**，`match_protocol.outcome` 仍采用：

```python
gave_up = intentional_give_up == 'YES' or abnormalend_reason == 'NORMAL_GIVEUP'
```

这不是同一个缺陷。根字段的 `intentional_give_up=YES` 不能压过明确的正常结束原因：

1. 当前原生库请求构造器 raw `0x7758e1c` 将 `+0x4a8` 的该字符串默认写为 `YES`，同时将 `+0x1f0` 的异常结束原因默认写为 `NOERR`。控制器 raw `0x7757cf4` 才按自身 `+0x1421` 写入对应的 YES/NO；因此 YES 本身不能证明用户点过放弃。
2. 用户提供的新 73 参考包 `D:/ef/73efootball-eFotball7373.apk.1`（SHA-256 `35ffec6804c69a4fea526a347268a2a3048f86f06c4f47de2086167eb1dc1e34`）的 `assets/gateway-release/work/tools/vscom_result.py` 第 11–16 行明确说明：11.1.1 正常 11:0 获胜时也会发送 `intentional_give_up=YES`。其代码先看 `abnormalend_reason`，只有缺少该原因时才用 YES 兜底。该源码成员 SHA-256 为 `56875e889bcd469e16e4ff0d7e72b06be8ec7523adfb4f0b55d6156506301ae2`；参考来源沿用已有 REA Evidence `ev_683888b167e00db2ebf7aef2b8e7b48764047877cc6b0c4e44ed5a19e91ad2d8`。源码注释是旁证，不是新取得的手机抓包。
3. 从实际 test.3 APK 读取旧纯函数做负面对照：`NOERR + YES + 11:0` 被改为 `0:3 / LOSE / gave_up=True`，仅把 YES 改为 NO 就保留 `11:0 / WIN`。这还会让 `match_stats.record_result` 保存 `excluded_give_up`，从而排除出场、进球和助攻。负面对照记录在忽略提交的 `build/test4-ai-false-forfeit-control.json`。

新逻辑先看明确的结束原因：`NOERR` 等正常原因按真实比分结算；`NORMAL_GIVEUP` 仍按弃赛；两种结束原因都缺少时，才使用旧 YES 标记兜底。未知异常仍保持待核对状态。没有固定胜利、无视比分、把所有异常当正常或猜测网络状态的处理。

## 补上统计即时刷新

持久化 `gameplayers.json` 内的 `games / goal / assist` 为整数，这一形态正确：完整球员 decoder raw `0x757a468` 在 raw `0x757a4d0..0x757a55c` 调用原生整数读取方法，分别写入对象 `+0x10 / +0x14 / +0x18`。

结算回复还有另一种形态：`extra_match_result.gameplayer_update_info[]`。其子项 decoder raw `0x7554bc8` 把这三个字段读为整数向量；consumer raw `0x765ea8c` 在 raw `0x765ebcc..0x765ec04` **取每个非空向量最后一项，覆盖客户端缓存的总数**。之前回复清空了该更新列表，缺少即时刷新通路。

`match_stats.update_response` 只在本场统计已经成功入库后，为冻结的本方阵容填入 `{gameplayer_id, games:[当前总数], goal:[当前总数], assist:[当前总数]}`。单元素向量足以供原生 consumer 读取，不猜测赛事槽位或前后值序号。旧结果重发读取数据库最新总数，防止把随后比赛的计数退回；已解约的实例跳过，不恢复球员。统计缺失或被排除时不生成计数。

本机已有 Unicorn 执行了实际 ARM64 赋值指令，比较单元素、多元素及空向量：单元素与多元素均取末值，空向量保留缓存，三个计数字段之外的字节保持不变。同一份脚本执行原生结果表初始化，得到 `WIN / DRAW / LOSE`，确认现有胜负字符串正确。证据为 `build/native-settlement-test4/executed-result-contract.json`。此为受限指令模拟，不是 Android 启动或比赛测试。

## 失败后可查看的本地反馈

`match_diagnostics.report_text(conn, version=...)` 为只读报告接口，供原生通知页按需展示，最多列出最近三场的开赛进度、游戏上报结束标记、本地结算、实际返回状态、统计保存状态与核对原因、参与人数、进球和助攻事件数。报告不输出账号、令牌、球员身份或名字，不修改结果，不增加弹窗。

`match_protocol.diagnostic` 同时记录结算回复与统计状态，字段名列表过长时先删除可选列表，确保不会被事件日志 4000 字符上限截断为损坏 JSON。

## 历史记录与验证范围

已经在 test.3 被旧逻辑判为弃赛的历史比赛，不能仅凭一个总比分恢复谁进球、谁助攻。新代码不自动翻改旧积分，也不补造个人统计；应从安装本测试包后的新比赛核对。对同一新比赛的重试、服务重启和数据库重开，仍保留原有按比赛 ID 防重复处理。

新增主机回归覆盖正常 `NOERR+YES` 的 11:0、双方位置的胜平负、真实 `NORMAL_GIVEUP`、缺少原因时的旧标记兜底、未知异常、重启与重试、最新计数刷新、防旧回执倒退及反馈报告隐私/只读性质。手机上的终场表现与统计页面仍需实机确认。
