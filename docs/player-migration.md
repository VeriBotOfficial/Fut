# 1.2.0 测试版球员数据迁移

## 接入与文件

现有 `embedded_gateway.initialize` 外层入口在创建网关连接后调用 `release_save.migrate(conn)`；保留此接口，在连接没有未提交事务且尚未开始处理游戏请求时执行：

```python
import release_save
result = release_save.migrate(conn)
```

必须在首次登录/球员列表/开赛请求响应之前完成。打包包含：

- `gateway/work/tools/player_parameters.py`
- `gateway/work/tools/player_identity.py`
- `gateway/work/tools/player_migration.py`
- `gateway/work/tools/player_release.py`
- `gateway/work/tools/release_save.py`
- `gateway/work/tools/project_info.py`
- `gateway/outputs/reference/efootxi-card-policy.json`

`player_parameters.owned_parameters` 是已有列表和开赛共用出口；现在对已核准卡 ID 应用惯用脚和双风格事实。界面可使用稳定 API `player_parameters.card_details(card_id)`，返回 `name, age, kind, cap, foot, weak_accuracy, weak_usage, attack_style, defence_style, card_id`；未知卡返回 `None`。此 API 不访问外部网络。

## 范围与保留项

精简基准来自随包 SQLite 和外部卡属性缓存，含 7092 个已分类卡、6328 个非 Standard 卡版本。两个 UNKNOWN 定义不自动赠送；不会删除它们。基准约 710 KB，包含姓名年龄，未打包原庞大外部缓存。

现有原生 147 项持有实例仅持久修复槽 43/61/62 和顶层风格。原有能力、级别、加点账本、额外技能、位置、booster、培养基线、阵容引用不改变。

每个已确认非普卡新增一份独立 serial 的正常球员副本，并记录 `efootxi_reserve_gifts`。该表名沿用历史名称以保留旧版领取记录，不表示新球员进入储备。重复启动不再赠送；未来基准新增卡 ID 时只赠新卡；已赠副本转入储备或解约后也不会重新生成。失败会整体回滚卡定义修正、原有实例固有字段修复、赠送及收据。

## test.3：修正预置球员进入储备的问题

test.1/test.2 在写入赠卡时还写入了 `warehouse`；正常球员快照排除了这些条目，所以主列表只有原始 23 人。用户录屏显示储备入口打开后为空；原有构建存档中仍有全部 6328 条赠卡，没有数据库丢卡的证据。此前储备响应采用推测结构，客户端显示为空的具体解析分支尚未验证。

本轮新赠卡直接进入正常「球员」列表。旧存档升级使用独立标记 `efootxi_gifts_main_list_v1`，不依赖已经应用的卡库 policy 摘要；仅移出赠送收据、准确 `(id, serial)` 和原始赠送来源都匹配的存活赠卡。已有解约墓碑、其他同卡副本、手动储备的非赠卡都不改。位置迁移只删除储备关系，不重新创建球员，也不重新写入球员 JSON、技能、加点账本或比赛统计。

迁移前另外保存 `local-club.before-efootxi-main-list-v1.sqlite3`，包含 test.1/test.2 运行后积累的进度；不覆盖最初 `before-efootxi-1200` 备份。完成一次位置迁移后，玩家以后自行转入储备的球员不会在重启时被再次移出。

真实存档副本验证：正常列表 23 → 6351，储备 6328 → 0，球员 JSON、培养账本、赠送记录和阵容摘要完全不变。完整 LOGIN、GET_ENTRY_INFO、GET_MYCLUB_ENTRY_INFO、SET_MYCLUB_ENTRY_INFO 和正常球员列表响应都包含同一组 6351 个实例；关闭数据库后重开仍一致。完整 GET_ENTRY_INFO MessagePack 约 10.61 MB，主机生成耗时约 0.8 秒，这不是手机性能保证；手机仍需检查大列表加载、筛选、滚动与开赛。

新副本使用当前项目 `recommended_allocation_v1` 推荐算法，在本卡上限内分配点数，同时保存原始基线、实际能力、经验和分类点数账本。不会将预览时的教练加成写入基础能力；可以随后通过现有手动路径重置并重新分配。

1487 Trending 统一按固定卡状态处理（cap 1，无培养点），包含原缓存中 8 张异常 cap>1 的修正。其余已有上限保留：外部 cap=0 的 7 张 Featured 继续为 1；3 张已有本地上限 35、外部 1 的 Legendary 保留原本地覆盖值，记录在基准 `cap_rules`，没有未经证明地全体替换上限。

## 证据边界

本包合法历史开赛样本每人只有 `gameplayer_id/parameters/averaged_overall`，不存在单独的风格和惯用脚字段。原卡库有 1828 张 Left 卡的槽 43=0，6643 张顶层第一风格非零而槽 61=0。这是当前测试修复采用的强静态依据。

Windows REA 原生 provider 不支持该 ARM64 ELF；已验证的直接 Ghidra 离线流程可用于聚焦静态分析，但不能将静态一致性测试等同于真机验证。用户于 2026-10-17 反馈 test.2 比赛内风格和逆足已正常，本轮保留其现有实现。推荐算法是项目既有的拟合规则，不宣称对所有卡证明官方总评最优。

逆足精度和频率在 7093 个有效缓存样本中与原槽 33/45 全部一致，取值 0..3。本次保留这些数值并验证合法范围，**不盲目整体加 1**。UI 显示序号和比赛弱脚效果仍需要真机取证。

## 已运行验证

球员测试覆盖事务失败回滚、幂等、版本增量、释放后不重赠、serial 别名避让/溢出及不复用、已有培养及技能保留、赠卡培养账本/经验一致、赠卡可手动重置、弱脚全部 16 个精度频率组合、旧参数布局与 booster 兼容。启动与开赛测试见下文；完整测试以 `python -m unittest discover -s tests -p 'test_player*.py' -v` 为准。

在原发布种子库的独立副本上迁移全部 7094 个定义：赠送 6328，修正定义 6873，修正旧持有实例 21；最终 6351 持有实例、6328 仓库条目、serial 全唯一。原 23 人能力和培养账本逐项核对保留，全部赠卡等级/点数/身份一致。主机首次约 1.39 秒，重复约 0.012 秒；这些不是手机性能测量。测试数据库约 30.7 MB，原输入 SHA-256 未变。

以上 6328 仓库条目为 test.1/test.2 历史构建结果；test.3 已改入正常球员列表，迁移验证见前文。手机仍需测试正常球员列表滚动、筛选与开赛；不能把容量字段上限视为性能保证。

## 解约持久化

原网关没有处理 `CMD_SELL_MYCLUB_GAMEPLAYER`，只会返回通用成功回执；该命令名已在原 APK 的 `libUE4.so` 字符串中确认。新增 `player_release.release(conn, req)` 在独立事务中删除准确的 `(id, serial)` 实例及仓库、培养、租借关联，清理该实例的阵容引用，并按数据库内 `sell_gp` 记账；不按卡 ID 批量删除同卡的其他副本。

保留 `efootxi_released_players` 墓碑、解约回执、赠送回执与旧 identity 别名。同请求重试不会再次发 GP，已经赠送后解约的卡不会随重启或 policy 更新重新生成。`owned_serial_high_water` 保证已删除实例的短 serial 不再复用，避免旧抽卡或培养回执指向后来获得的另一张卡。

网关接入 `player_release.COMMAND`，调用 `release` 并捕获 `ReleaseError` 返回现有非法参数错误。请求须携带明确的正整数 id 与 serial；支持单个 `gameplayer_id` 或列表 `gameplayer_id_list/gameplayer_list`，拒绝歧义字段和重复实例。原包没有解约的完整捕获请求/奖励响应，额外道具奖励字段尚未推断添加，手机解约流程仍需回归。

## 启动与存档保护

统一启动依次执行：policy 校验、迁移前 SQLite 快照、owned identity、旧展示默认值、efootxi 项目展示、球员迁移。每个阶段与自身 marker 同事务提交；后续阶段失败时保留已完成阶段，第二次启动继续剩余工作。错误包含阶段名，原库保留，不删除、不重建、不以球员数量为 0 判断新用户。

迁移前通过 `Connection.backup` 生成 `local-club.before-efootxi-1200.sqlite3`，包含尚在 WAL 中的已提交内容；经 `quick_check` 后原子发布，后续不覆盖。禁止复制正在使用的裸 `.sqlite3` 文件代替 SQLite 快照。启动传入未提交事务会报错并保留调用方事务，不代替调用方提交。

从实时启动路径移除了 `reset_pools`：启动不能清空历史抽卡回执或消耗记录。`clean_seed(conn, new_test_seed=True)` 只允许构建时明确指定的独立种子副本。identity 迁移新增取得写锁后再次检查完成 marker，避免两个首次启动同时判断未完成后重复建表。

实际原 APK fixture 发现且已修复的新接入错误：`managed_agents` 的字段是 `agent_id/name/enabled/replacement/mode`，没有 `json`。项目展示迁移现按实际列结构处理 `agents.json` 和 `managed_agents.name`，整个阶段原子提交。此错误由真实数据复现；不能据此宣称用户原 1.1.1 启动失败也是同一原因，旧故障仍需根异常日志。

测试验证连续关闭重开三次、全解约空库重开、WAL 备份含已提交进度、写失败后重启恢复、展示迁移失败回滚、并发首次 identity 迁移，以及拒绝隐式种子清理。

## 开赛是否回退数值

`test_player_match_parameters.py` 使用原 APK 解出的完整种子与协议 fixture，在临时副本完成真实启动迁移。选取 19 岁 Big Time Messi `89136409091415` 的两份独立 serial，通过实际 `CMD_USE_MYCLUB_CAREERPLAN` 分别重置后手动投入 4 点传球和 4 点下肢力量，再通过技能命令分别添加不同特殊技能。

检查详情、LOGIN、两种 ENTRY_INFO、`_home_entries`、普通/AI 联赛/My League 的 `CMD_START_GAME`，以及首发替补列表倒序、关闭数据库后重开。全部 147 项按位置、可训练能力、弱脚、惯用脚/风格、技能、booster 和其他固有字段比较；只允许已说明的身份/booster 规范化及开赛额外技能位叠加。两份实例保持自己的手动训练值，没有回到 `_careerplan_base_parameters`，开赛响应没有修改保存的球员对象。

上述是实际数据经 Python 网关输出的自动化验证，未执行 Android 比赛引擎；不能据此证明引擎内部受状态、教练、战术或技能影响后的所有显示数值完全相同。真机验收仍应比较同一卡的详情、赛前和比赛内数值，并记录卡 ID、serial、阵容位置与教练。

## 部分中国版原创 FP 卡面

已定位用户两个样例：Odegaard 85 CMF 为 `106772618646508`，Falcao 77 CF/40 岁为 `105592576309572`。二者有对应外部图像 URL，但外部 PNG 不能直接视为 Unreal 可消费的卡面资源。

现有 GET_ENTRY_INFO 模板首卡为普卡，`offline_card_variation=0`、`offline_native_card_type=1`；当前转换保留部分模板值并以长卡 ID 回退 `offline_base_player_id`，存在展示映射疑点。但其他正常卡同样走此路径，尚不足以判定此处是中国版原创 FP 缺图的根因。资源 ZIP 外层是 CPK/PAD 包等，未进一步全量解包确认内部缺失，因此不能声称已证明卡面文件不存在。

本模块不改卡 ID、不删卡、不插入占位图。可在测试公告说明：**部分中国版原创 FP 卡面暂无法显示，本测试版尚未补齐对应图像。**
