# 1.2.0 测试环境

> **置顶声明｜开发来源与修改许可**
>
> efootxi 第一版基于 73@**7othree** 版本开发，已获得修改许可。后续版本由 maxlzyver 持续修改与维护，具体变更以各版本更新公告为准。

当前另有 **test.6 / 1205 本地测试**，不上传 GitHub。新一轮的参考包对照、名单补齐、修正与验收边界见 [本轮调查](test6-myleague-investigation.md)。下方保留 test.3 的构建与验证记录。

更新：2026-10-17。本页对应 **test.3 / buildRevision 1202**。本轮主机回归完成，APK 构建与附件核验以本轮报告为准；未完成 Android 真机验收。

## 隔离配置

| 项目 | 测试版 | 正式版 |
|---|---|---|
| 仓库 | `maxlzyver/efootxi-test` | `maxlzyver/efootxi` |
| 应用名称 | efootxi 测试 | efootxi |
| 包名 | `com.efootball.revival.test` | `com.efootball.revival.local` |
| 本地网关 | `127.0.0.1` 上系统分配的可用端口 | `127.0.0.1:8085` |
| 更新频道 | 测试仓库 `updates/channel.json` | 原正式频道 |
| 存档与频道缓存 | 测试应用私有目录 | 原正式应用私有目录 |

测试包与正式包并存，不能覆盖正式包；正式存档不自动复制。后续测试包使用相同包名和签名覆盖安装，通过递增 `buildRevision` 识别，安装仍由 Android 系统确认。游戏资源依赖的 `versionCode=311010101` 保持不变；版本名 `1.2.0`、构建号 `1202`、标签 `v1.2.0-test.3`。

设备基线为 Android 12/API 31、ARM64，不代表所有设备和驱动已测。首次进入自动下载资源，无需手工选包、搬运或解压；完整资源准备后可离线进入。正式资源只作为已验证的只读来源复用，测试频道及缓存独立。

## 输入与受检原生补丁

基线为已发布 efootxi 1.1.1 APK，SHA-256：
`0a612a8153843c8bfe5fb515eac5b983a6ec377990eaf8635c03034b1f5ae255`。

其 `lib/arm64-v8a/libUE4.so` 的 SHA-256：
`2cea0c85344d7d7f295aea1c2353621f02db78dbccd888a0a21a0508ac330659`。

构建入口为 `python tools/build_test_apk.py`。它将网关提取到独立构建目录，覆盖审阅后的代码与公告，迁移独立种子并检查数据库；原输入不修改。历史 `build_efootxi.py` 不作为本轮独立测试包入口。

test.3 不再声称原生核心完全未改。本轮通过 `tools/native_ui_patch.py` 汇总两组补丁：

- `patch_native_skill_ui.py`：在已识别的技能候选函数范围内扩为 63 项目录，沿用原分类和确认路径，不修改比赛效果参数映射。
- `patch_native_fps_ui.py`：原有四项后追加第五项 120，加入菜单索引与原生 120 枚举的双向转换和文字显示，保留原保存路径与渲染上限逻辑，不设固定模拟帧率。

补丁先核对整库来源及每段原始字节，保证等长、无重叠，核验允许范围外内容不变，不追加 ELF 段。逐段前后值及库哈希写入 `native-ui-patch.json`。其他原生库及原始游戏 DEX 1–6 保持并独立校验；应用身份、启动器、本地网关和公告配置另在成员变更范围内。

FPS 已核对正常调用的寄存器、栈、文字分配及保存恢复；新短辅助代码的部分入口/退出窗口仍使用旧函数的展开描述，异步采样或异常恰在这些指令时，回溯信息可能不准确。此边界与手机验证要求见 [原生帧率菜单](native-fps-menu.md)，不将主机指令测试当成所有 Android 环境的运行保证。

## 构建输出与存档保护

构建需要本地原 APK、Android 工具、JDK、Python 依赖及签名材料；这些不提交到代码仓库。构建脚本核验签名、API 31 包身份、16 KiB 对齐及 ZIP 成员哈希。输出目标为 `release/v1.2.0-test.3/`，包含 `efootxi-test-1.2.0.apk`、`verification.json`、`native-ui-patch.json`、`apk-update.json` 与 `SHA256SUMS`。是否完成构建、签名和发布以本轮实际报告/附件为准，历史已发布附件不原地替换。

线上启动保留 `release_save.migrate(conn)` 桥接。SQLite `backup` 在迁移前保存含 WAL 的快照，完成标记与对应改动共同提交；失败保留原库并显示阶段与根异常。旧赠卡从储备迁入主列表有独立一次性标记及新备份，不复用早期备份覆盖玩家后续培养。仅独立构建种子允许 `clean_seed(conn, new_test_seed=True)`；实时启动不清抽卡回执、不按空列表重播种。

## 本地验证与真机边界

本轮完整 Python 回归 **265 项通过，无跳过**；Java 资源与更新频道 **55 组、253 项断言通过**。覆盖存档迁移与保护、原生技能五槽/删除、跨登录请求、升级回复、三模式正常完赛与个人统计、公告、下载频道以及原生技能/FPS 菜单补丁。

常规命令为 `python -m unittest discover -s tests -v`。网关测试需要 `msgpack`；真实协议测试需要本地 `build/gateway-fixture` 等交付物副本。原生指令测试另需本地 Unicorn、Capstone、Keystone 等分析依赖；本次环境位于 `build/native-analysis-deps`。缺少可选 fixture 或依赖导致的跳过不能当作已验证；本轮完整运行没有跳过。原生补丁机器码已固化在审阅模块中，正常产品构建不需要临时联网安装汇编器。

真实交付物检查使用临时 SQLite/协议副本及限定 ARM64 函数，没有启动 Android 游戏或执行抽卡动画。已有用户反馈确认风格、心有灵犀、逆足与解约正常；其余完整手机验收包括覆盖安装、普通列表大量赠卡、技能选择/删除/半场表现、加点即时刷新、赛后与重启统计、公告红点、120 档实际帧率与速度、球队球衣、组合包页面刷新及黑屏/闪退复现。

瞬发射门、强力抢断未开放；心有灵犀比赛可用不等于已确认新增训练映射。中国版原创 FP 卡面本轮未补齐。详细范围见 [测试状态](test-1.2.0-status.md)、[技能兼容性](skills-compatibility.md) 和 [新参考包对照](reference-73-comparison.md)。

发布只操作测试仓库。流程见 [资源与 APK 发布](resource-updates.md)。反馈：[lzyuan666666@gmail.com](mailto:lzyuan666666@gmail.com)。资源托管在 GitHub，下载慢或失败时建议更换好用、稳定的梯子（网络代理）及更快的网络，实际速度取决于连接。
