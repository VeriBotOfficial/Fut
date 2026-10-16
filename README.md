# 73efootball 自动资源准备测试版

安装 APK 后，首次打开会自动下载、校验并导入配套资源；准备完成后继续进入游戏原有启动流程。后续启动检查本地资源，无需重复下载。

## 下载与使用

到 [Releases](https://github.com/maxlzyver/73efootball/releases) 下载自动下载测试版或 Android 12 兼容测试版的 `.apk` 文件。`resources-311010101-v1` 是程序使用的配套资源版本，用户无需手动下载其中的 ZIP。

- APK 约 2 GB，首次资源下载约 3 GB，建议 Wi-Fi，并保持资源准备页面打开。
- 需要 64 位 ARM Android 设备。原测试版最低系统声明为 API 32（Android 12L）；[Android 12 兼容测试版](https://github.com/maxlzyver/73efootball/releases/tag/bootstrap-11.1.1-android12-v2) 调整为 API 31，手机桌面名称为 `efootxi`。
- 下载期间显示进度，失败后可重试；关闭进程后再次打开会尽可能续传。
- 建议安装后预留至少 5 GB 可用空间供资源准备。程序会按实际剩余资源检查空间。
- GitHub 下载表现取决于当前网络；首次准备需要能够访问 GitHub Releases。
- 这是重新签名的测试 APK，签名不同于用户提供的原 APK，同包名旧版无法直接覆盖安装。已有存档时先通过原应用可靠备份；卸载通常会删除应用数据。

## 验证范围

资源已逐文件校验。构建流程验证 APK 签名、ZIP 对齐，并检查原游戏 payload 是否保持一致。下载器核心逻辑通过独立 Java 测试。

当前没有连接 Android 测试设备，未验证实机安装、游戏菜单或比赛运行；也未承诺所有游戏模式能够离线使用。请把此版本视为需要实机反馈的测试包。

Android 12 兼容测试版只修改安装清单中的最低系统声明和显示名称，保留其他 7,792 个文件的内容。已核对 API 32 接口的版本判断，并校验签名、对齐和全部文件哈希；尚未进行 Android 12 真机兼容与性能测试。

## 实现

- 在 `GatewayBootActivity.onCreate()` 调用系统 `super.onCreate()` 后插入资源准备判断。
- 新增 `classes8.dex`，用 Android 平台 API 显示下载界面并处理资源部署。
- APK 内置资源版本、包/文件大小及 SHA-256 清单，下载地址固定到相应 Release。
- 每包下载后校验、解压并删除临时 ZIP；只写清单中指定的 `assetpacks/` 和 `Download/` 文件。
- 完成标记绑定资源清单；正常启动检查文件存在及大小，资源缺失时恢复准备。

仓库源码仅包含本次新增的资源准备逻辑和打包工具，不包含游戏的原始开发工程。

## 本地构建

构建脚本当前按本次 Windows 工作区准备：JDK 21、Android API 36 的 `android.jar`、Build Tools 36.0.0、Apktool 2.12.1。工具放在 `.tools/android/`，具体路径见 `tools/build_apk.py`。原 APK 与资源 ZIP 默认位于 `D:/ef/`。

```text
python tools/package_resources.py
python tools/build_apk.py
python tools/verify_apk_payload.py
```

构建会在 `signing/` 首次生成独立签名材料。保留该目录供以后同签名更新；它已被 Git 忽略，不能上传到公开仓库。输出与本地验证报告位于 `release/`。

制作 Android 12 / `efootxi` 变体时，将已验证的上一版自动下载 APK 路径传给 `tools/build_android12_variant.py`。该工具限定输入 SHA-256，只修改二进制安装清单，复用签名材料，并验证其他所有文件不变。`tools/check_android_api_delta.py` 可使用 SDK 的 `api-versions.xml` 审核 DEX 引用；静态引用检查不代表实机执行结果。
