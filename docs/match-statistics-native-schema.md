# 个人比赛统计：当前包体的原生协议证据

更新：2026-10-17。本文记录静态分析，适用于 1.2.0 测试版所使用的 11.1.1 游戏核心；不是手机实测报告。

## 结论与证据范围

已在当前 `libUE4.so` 的请求序列化伪代码中找到个人出场信息和进球、助攻身份列表。它们不是此前猜测的 `player_stats_list`：

- 出场信息：`match_result.myteam_player_list[]`。
- 进球身份：根节点 `goal_player_list[]`。
- 助攻身份：根节点 `assist_player_list[]`。
- 三条路径均携带球员实例的 `id` 和 `serial`，不需要靠球员名字或单独卡片 ID 合并副本。

这解决了字段路径、结构和序列化类型的关键缺口。哪些比赛模式实际走该请求、两个出场标记的全部取值以及边界比赛的表现，仍需调用链证据或手机实测，不能把字段名解释当作已验证的游戏行为。

## 交付物与分析方式

| 项目 | 值 |
| --- | --- |
| 基线 APK | `release/v1.1.1/efootxi-1.1.1.apk`，测试仓库上级工作区中的正式基线，只读 |
| APK SHA-256 | `0a612a8153843c8bfe5fb515eac5b983a6ec377990eaf8635c03034b1f5ae255` |
| APK 成员 | `lib/arm64-v8a/libUE4.so` |
| ELF 大小 | 161,937,720 字节 |
| ELF SHA-256 | `2cea0c85344d7d7f295aea1c2353621f02db78dbccd888a0a21a0508ac330659` |
| 原生分析工具 | 本机 Ghidra 12.1.4 headless，AARCH64，直接离线导入 |
| Ghidra image base | `0x100000` |
| 主候选函数原始虚拟地址 | `0x775915c`，分析地址 `0x785915c` |

首先使用 REA 技能及已有 REA/JADX 证据。当前 Windows REA Ghidra provider 不支持 ARM64 ELF；没有重复调用已知不支持的入口。随后直接调用已安装的 Ghidra headless，实际完成 ELF 导入与聚焦反编译。这部分是本地 Ghidra 结果，**不冒称为 REA 返回的 Evidence**。游戏程序没有被运行或修改。

本地分析材料位于忽略提交的 `build/match-stats-investigation/`：

- `StatsNativeDump.java`：按 `image base + 原始 VA` 定位，先读取 `0xad0df5 + image base` 并确认字符串为 `gameplayer_id`，防止地址整体错位。
- `headless-console.log`：记录 `IMAGE_BASE 100000`，以及 `0785915c / 07895314 / 0776e064 / 076b9dc0` 的反编译成功。
- `775915c.c`、`7795314.c`、`766e064.c`：当前使用的伪代码。
- `native-stat-xrefs.json`、`function-field-refs.json`、`function-boundaries.json`：原始 ARM64 字符串引用和候选边界。

候选函数由定位脚本创建，其 `stats_candidate_*` 名字是分析时命名，不是恢复的原始符号。`775915c.c` 有类型传播警告；输入结构体、部分调用约定和函数边界尚未恢复为完整源码类型。下列结论以明确的对象赋值与循环为依据，不依赖伪代码自动生成的变量名代表真实业务类型。

## 出场行的结构

`775915c.c` 第 155–242 行遍历输入对象 `+0x2c8` 至 `+0x2d0` 的向量，每项步长 `0x80`。每项新建两个对象：内层身份对象和外层球员行。

| 序列化路径 | 观察到的构造方式 | 限定结论 |
| --- | --- | --- |
| `match_result.myteam_player_list[].gameplayer_id.id` | 64 位数值构造调用 `func_0x030cb6a0` | 身份对象中存在独立的数值 ID |
| `...gameplayer_id.serial` | 同一 64 位构造调用 | 同时保留实例 serial，不能忽略 |
| `...play_time` | 从行结构读出 32 位数值，调用 `func_0x030cb670` | 数值字段；未确定它按分钟、秒或其他单位表示时长 |
| `...is_starting_member` | 从 libc++ 字符串布局取内容，调用 `func_0x030cb6dc` | 字符串标记，不是 JSON 布尔值 |
| `...is_join_game` | 同上，取另一独立字符串字段 | 独立于首发标记的字符串字段 |

外层行还包括 `rating`、`position`、`oap`、`base_value`、`is_man_of_the_match`。构造完一行后，调用 `func_0x030ccc54` 追加到 `auStack_118["myteam_player_list"]`；第 582–583 行把整个 `auStack_118` 写入根对象的 `match_result`。因此出场列表明确嵌套在 `match_result` 内，不能从根节点或 `player_stats_list` 取代读取。

另一候选函数原始 VA `0x7795314` 在第 155–242、582–583 行有相同的出场行结构，提供了第二处结构佐证。它没有本节下一部分的根进球/助攻列表；在命令对应关系确认前，不扩展到所有相似请求。

### 出场含义：能证明和仍待确认的部分

静态证据证明每一条身份都有独立的时长、首发标记、参与标记，足以避免把“在 23 人名单内”直接当作“上场过”。但本次聚焦的序列化函数只是复制已有字段，没有展示这些标记在开赛、换人、伤退、红牌或补时阶段的赋值来源。

所以以下是有依据的适配方向，不是全部已实测的语义保证：

1. 使用结算列表中明确的参加比赛/首发标记和正时长判断出场；保持每场同一实例最多加一次。
2. 未上场替补不能仅因出现在开赛阵容、结算数组或拥有 `serial` 就计入出场。
3. 最后时刻换上的球员可能时长舍入为 0；需要结合明确的参加比赛标记，不宜只判断 `play_time > 0`。
4. 标记值是否始终为 `YES/NO`，以及首发但比赛尚未实际开始的中断情况，本次伪代码未证明。适配器应验证输入取值，异常结构保留诊断而不凭空累加。

## 进球与助攻是保留重复项的身份列表

`775915c.c` 第 619–659 行有两个连续循环：

| 输入向量 | 输出路径 | 每项结构与步长 |
| --- | --- | --- |
| 输入对象 `+0x400` 至 `+0x408` | 根 `goal_player_list[]` | 两个 64 位值组成 `{id, serial}`，步长 16 字节 |
| 输入对象 `+0x418` 至 `+0x420` | 根 `assist_player_list[]` | 同样的 `{id, serial}`，步长 16 字节 |

两者都是向数组逐项追加。序列化函数没有按身份去重、没有单项 `count` 字段、没有把它们嵌入 `gameplayer_id` 或 `match_result`。因此输入向量有同一身份的重复项时，输出也保留重复项。适配时不得先转换成集合，否则一人梅开二度会丢失一次。

“每条列表项按一次进球/助攻计数”同时得到用户提供参考 APK 的消费逻辑支持：`ziruMlRecordUserListV85` 对每个含有效 `id` 的对象调用一次 `ziruMlAddUserStatV85`，后者把排行 `value` 加 1。当前包体比该参考消费者显示了更多信息：实际还发送 `serial`，应据此区分同卡副本。

仍需区分证据强度：当前原生序列化证明重复项会被保留，参考消费者证明它按每项一次解释；本次尚未跟到当前比赛引擎把每次进球写入向量的全部生产路径。手机测试仍需验证梅开二度、无助攻进球、乌龙球、点球大战及退赛。

### 空列表与对手列表

两个循环仅在 begin 与 end 不相等时执行，字段访问也在循环内部。本函数没有预先构造空 `goal_player_list/assist_player_list`；零事件时可能省略键，不能要求所有合法 0–0 请求都带 `[]`。

第 724–744 行中的根 `opp_goal_player_list` 和 `opp_assist_player_list` 是每项一个 64 位数值的不同结构，不是 `{id,serial}`。个人持有球员统计只应使用自己的列表，不能把对方列表按相同结构混入。

`match_result.owngoal_user_list` 是另一条整数列表；此处不能据此给某位持有球员分配乌龙球或把全队比分补齐成某位球员的进球。

## 其他看似相关的路径不同时累计

原始 VA `0x766e064` 的伪代码序列化单个事件，含 `game_id`、`goal_gameplayer_id:{id,serial}`、`assist_gameplayer_id:{id,serial}`、双方 side、`gtimer`、`phase`、`match_minute`。它与结束比赛的汇总列表不同；本次不把独立事件和结算列表同时累加，以免同一进球被记两次。

已知 `player_stats_list` 在 134 个既有 MessagePack 响应中只出现于 `CMD_GET_GAMERESULT_LIST`，且为空。它不能替代此次恢复的结算路径。相关只读转接层审计保存在 `build/stats-v2-audit/findings.md`。

## 与 REA 参考包证据的关系

参考包为用户提供的 11.0.1 APK，SHA-256 `4d8d254e95d220e60b903d48b548035c6686edbde89e45552f0a9920ce837b04`；当前核心为 11.1.1，因此当前 ELF 序列化优先用于字段结构，参考包只作消费语义旁证。

- `ev_1dbb2cbb7f338b5a5ee108c36eed70850d5605a077c698185d744f8fa648b2f9`：`ziruMlRecordRealMatchEventsV85` 读取根进球/助攻列表。
- `ev_cb0f9b772519b5461b79e806a3a0b20c068fe4eb1221ed44eb3ab32b8874cea3`：`ziruMlRecordUserListV85` 逐条取 `id`。
- `ev_a2a68f425ebc3a219917751890cee63f6c482630d5c196e0ec373839e134ce50`：`ziruMlAddUserStatV85` 每次加 1。
- `ev_997ace775c3ec0429fe1c085883ea56c4987511077ce9e3fdd81927f15eb9451`：参考 AI 结算没有上述榜单调用。
- `ev_9f78d9127a6fb7d56a66339ba5e9b09b5dcf21d056dc121369f80e50041fb182`：当前基线 `EmbeddedGateway.serve` 完整转接 POST 字节；没有在 Java 层删去统计字段。

静态协议证据支持实现严格的本地结算适配、按 `(id,serial)` 保存，以及按比赛 ID 防止重复入账。手机实测仍负责确认模式覆盖和边界语义；没有实测前，公告应描述测试功能和验证范围，不宣称所有设备已经验证通过。
