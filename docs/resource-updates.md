# efootxi 资源和 APK 更新

游戏包名保持 `com.efootball.revival.local`。手机显示名称为 `efootxi`。
当前兼容范围是 Android 12（API 31）及以上、ARM64；“全兼容版”不表示支持所有 Android 版本或 32 位手机。

更新入口固定为：

`https://github.com/maxlzyver/efootxi/releases/download/updates/channel.json`

资源附件放在有固定版本标签的 Release 中。应用联网时先读取小体积更新清单，确认有新资源版本时校验手机已有资源，只下载缺少或校验不符的文件所对应的 ZIP。每个新文件单独打包；相同资源仍引用历史附件。已经下载的 ZIP 可在服务器支持时断点续传。普通重开沿用原有资源就绪检查，不会每次读取数 GB 内容重新计算 SHA-256。离线启动跳过远程更新检查，使用有效本地清单和已准备完整的资源；不完整的首次下载仍需要联网继续。

不要直接替换既有资源 ZIP，也不要删除仍被当前清单引用的历史资源 Release。仅在网页上传一个新 ZIP 不会自动触发客户端更新；应使用以下工具生成完整清单，再最后发布 `channel.json`。

## 发布新资源

准备 Python 3 和已经登录 `maxlzyver` 的 GitHub CLI。当前工作目录应为项目根目录。工具的 `prepare` 命令只写本地输出；`publish` 才会向 GitHub 写入。

1. 下载当前更新清单和它引用的资源清单：

   ```powershell
   New-Item -ItemType Directory -Path 'release/current-update' -Force
   gh release download updates --repo maxlzyver/efootxi --pattern channel.json --dir release/current-update --clobber
   $efootxiChannel = Get-Content -LiteralPath 'release/current-update/channel.json' -Raw | ConvertFrom-Json
   Invoke-WebRequest -Uri $efootxiChannel.resources.manifestUrl -OutFile 'release/current-update/manifest.json'
   ```

   `release/current-update/channel.json` 的 `revision` 是当前清单序号，下一次必须加一或使用更大的整数。

2. 准备包含新版本**完整资源集合**的 ZIP。ZIP 内路径仍以 `assetpacks/` 或 `Download/` 开头，不能额外套一层文件夹。工具直接扫描这个 ZIP 的实际内容和 SHA-256，不依赖旧版 `config/source-resources.json`。

3. 生成新版本。以下示例假设当前清单 `revision` 为 1；按实际值修改 `--revision`，使用未用过的资源标签和一个空输出目录：

   ```powershell
   python tools/publish_resource_update.py prepare `
     --source 'D:/ef/new-resources.zip' `
     --previous-manifest 'release/current-update/manifest.json' `
     --previous-base-url $efootxiChannel.resources.baseUrl `
     --apk-json 'release/current-update/channel.json' `
     --tag resources-20261017-v1 `
     --revision 2 `
     --output 'release/update-20261017-v1'
   ```

   输出目录包含资源清单 `manifest.json`、新的 `channel.json`、新增或变更资源的 ZIP，以及 `publish-plan.json`。检查报告中的 `newOrChangedEntryCount` 和 `uploadBytes`；未变化资源不会重复打包上传。移除的路径会列在 `removedPaths` 中，客户端不会主动清理这些路径，避免删除游戏管理的数据。

4. 发布：

   ```powershell
   python tools/publish_resource_update.py publish --directory 'release/update-20261017-v1'
   ```

   若 `gh` 不在 PATH，可追加 `--gh '.tools/github/cli/bin/gh.exe'`。工具先校验本地文件，再核对历史资源和 APK 的 GitHub 大小与 SHA-256，上传新的资源 Release，核对上传结果，最后替换更新入口中的 `channel.json`。发布结束会生成 `publication.json`。不要同时运行多个发布任务；工具在最终替换前再次检查频道是否被其他任务修改，但 GitHub 的附件替换 API 不提供原子的条件更新。

上传中断时，旧清单会保持使用旧资源，直到最后的清单替换开始。GitHub 替换附件会有短暂不可用窗口，客户端必须保留已验证的本地清单用于失败回退。若发布失败，工具会报告停止阶段，以及是否已开始替换频道。先检查对应草稿或资源 Release；工具拒绝覆盖已有资源标签。可用新的标签重新 `prepare`、`publish`，不要修改已经被客户端引用的资源附件。草稿通过 Release 数字 ID 校验，避免尚未创建 Git 标签时按标签查询返回 404。

## 初始化已有资源

已有资源 ZIP 已经发布且校验过时，可以直接复用资源清单，无需重新打包或上传资源内容：

```powershell
python tools/publish_resource_update.py prepare `
  --prepare-existing 'release/resources/manifest.json' `
  --previous-base-url 'https://github.com/maxlzyver/efootxi/releases/download/resources-311010101-v1/' `
  --resource-version 311010101-github-v1 `
  --apk-json 'release/apk-update.json' `
  --tag resources-control-v1 `
  --revision 1 `
  --output 'release/initial-update'
```

首次发布前，仓库需已有公开的 `updates` Release；其中可以暂时没有 `channel.json`。初始化后的资源版本字符串保持原值，避免仅增加下载 URL 就让已有完整资源触发一次全量校验。

## APK 覆盖更新

资源更新不需要重新安装 APK。APK 有代码改动时，仍使用同一包名和本地同一签名密钥，才能覆盖已经安装的版本并保留应用数据。签名密钥和密码不能上传仓库。

当前游戏的资源目录依赖原始 `versionCode=311010101`，因此这个值保持不变；更新检测使用独立递增的 `buildRevision`。例如 efootxi 1.1 使用 `versionName="1.1"`、`buildRevision=1100`。下一 APK 的 `buildRevision` 必须更大。

APK 元数据应根据实际构建和签名结果填写，不能仅改版本字符串：

```json
{
  "buildRevision": 1100,
  "versionCode": 311010101,
  "versionName": "1.1",
  "url": "https://github.com/maxlzyver/efootxi/releases/download/v1.1/efootxi-1.1.apk",
  "sha256": "实际APK的64位小写SHA256",
  "bytes": 2035221824,
  "signerCertificateSha256": "实际签名证书的64位小写SHA256",
  "minSdk": 31,
  "abi": "arm64-v8a"
}
```

先发布并校验新的 APK，再使用其元数据生成新的更新清单。APK 覆盖安装仍需要下载完整 APK，并由用户确认 Android 系统安装提示；不会另外创建一个不同包名的游戏，也不能绕过 Android 安装确认。

## 清单协议

`channel.json` 包含 `schemaVersion: 1`、单调递增的 `revision`、固定 `packageName`，以及：

- `resources`：`resourceVersion`、`gameVersionCode`、`manifestUrl`、`sha256`、`bytes`、`baseUrl`。
- `apk`：上述 APK 元数据。

资源 `manifest.json` 保留原有 `schemaVersion`、`packageName`、`versionCode`、`resourceVersion` 和 `files`。每个 ZIP 包有 `name`、`bytes`、`sha256`、`expandedBytes`、`entries`，并可有独立的 `url`。`entries` 为当前所需文件的路径、大小和 SHA-256。

如果复用历史 ZIP 中的一部分文件，包字段 `allowSubset: true` 允许安装器在验证整个 ZIP 的 SHA-256 后，跳过未列出的其他成员；跳过的成员不会写入游戏目录。清单中所有声明的成员仍必须存在并通过校验。这样已有文件保持复用，新增或变化文件由独立的小包补充。
