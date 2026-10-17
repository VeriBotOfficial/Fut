# 启动失败：端口占用

用户 2026-10-17 提供的录屏（23.82 秒）末尾显示：

```
本地存档启动失败
java.net.BindException: bind failed:
EADDRINUSE (Address already in use)
```

该错误发生于本地 TCP 监听绑定，不是 SQLite 编译/解析错误。录屏不能识别占用端口的是另一个应用、同应用的其他进程还是其他本地服务；不据此推断占用者。

原 APK `EmbeddedGateway` 构造器固定绑定 `127.0.0.1:8085`，请求路由和 Python `_local_urls` 同样写死 8085。仅改监听端口会让游戏指令或 livedata 下载仍连接旧端口，因此必须一起修改。

测试版改为仅绑定 loopback、端口设为 0，让系统原子分配可用端口。Java 请求路由及健康检查调用实际 `getListeningPort()`；Python 在首次请求前装入等价的 livedata 地址生成器，每个响应只读取一次实际端口。保持文件名、校验值及其余请求数据不变，不探测或复用其他应用的服务，不结束其他进程，不删除存档。

REA/JADX `ev_c51eac58419095f9d98f0a09220fbace7dd0859c15dad18b78b6298eed528ac2` 对已绑定基底代码副本验证：`fi.iki.elonen.NanoHTTPD.getListeningPort()` 返回 `myServerSocket.getLocalPort()`；未创建 socket 时返回 -1。代码副本 SHA-256 为 `abbf810948c114eec96e701de389ca84d66afe4997b7f0c143f0e677559dd258`，其 DEX 来自已验证 1.1.1 APK。

Python 3.12 字节码静态检查确认 `_local_urls(value)` 只递归处理 dict/list，并把 `livedata_list` 中有 name 的条目改写为 loopback URL + `Path(name).name`。新函数保持此语义，替换动态端口；没有运行外来 Python 字节码。

主机回归覆盖已有监听同时存在、新监听独立连通、重启后端口变化、只修改 livedata 地址、输入不变、无效端口拒绝及大响应只读一次端口。仍需同一手机安装测试版，验证第二次启动和其他本地服务共存时正常进入。
