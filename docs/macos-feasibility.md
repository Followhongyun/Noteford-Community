# macOS 可行性记录

> 历史原型/阶段验收，已被后续实现取代；下文“当前”“明早”“待接入”均指原记录阶段，不是日常操作指令。现行流程只操作临时微信副本，不改原版安装；Mac 密钥仅持久保存在钥匙串，`keys.json` 只是旧迁移位置，不得恢复明文运行方案。当前边界见 [数据与安全](DATA-AND-SECURITY.md)、[运行指南](OPERATIONS.md)，安装验收见 [STATUS](STATUS.md)。

适用对象：维护者。本文是 2026-10-15 的技术验证，不是完整的 Mac 移植，也不改变 Windows 版运行路径。只读探测在 `scripts/mac-probe/`。当时原型路径是一次性重签正版微信（已废止，禁止用于当前产品），并在登录时用 interpose 取密钥；代码在 `native/macos/wechat_hook/`、`src/noteford/mac_resign.py`、`mac_capture.py`、`mac_sync.py`、`mac_entry.py`。Electron 只在 darwin 上增加 `noteford:mac-wechat`。Windows 服务原有接口不变，`/api/v1/mac/wechat` 在 Windows 上直接返回未改密钥路径。

云端环境是 Linux，没有安装 Mac 版微信，不能打开 `~/Library/Containers`，也不能对微信进程调用 `task_for_pid`。下面凡是依赖真机文件或进程的判断，都标成待真机。

证据分四档：

| 标记 | 含义 |
| --- | --- |
| 源码 | 本次阅读了本仓库对应文件 |
| 对照 | 本次阅读了公开项目的源码或文档，没有在 Mac 上运行 |
| 推断 | 由上面两类材料推出，没有直接看到 Mac 文件 |
| 待真机 | 要等 `scripts/mac-probe/probe.py` 的报告，或用一把已知密钥做一次 HMAC 校验 |

## 结论

Mac 微信 4.x 的**落盘数据库格式**与本仓库 `crypto.py` 使用的 SQLCipher 4 原始密钥页格式，在公开实现里是同一套参数。账号目录也使用 `db_storage/session/session.db` 与 `db_storage/contact/contact.db` 这一对标记。图片 V2 `.dat` 的容器布局与 `image_crypto.py` 的解密函数一致。

密钥**怎么拿到**不一致。Windows 4.x 仍走 `winmem.py`：打开 `Weixin.exe`，按 `com.Tencent.WCDB.Config.Cipher` 和一段 x86-64 指令字节做 XOR，再取出 `x'<十六进制>'`。Mac 进程名是 `WeChat`。公开工具在 4.0.x 上扫描明文 `x'<64 位密钥><32 位 salt>'`；4.1.10 起这串明文不再常驻，登录时由 `CCKeyDerivationPBKDF` 派生。直接对正版进程做 `task_for_pid` 要求 root，并且要改掉腾讯签名。那条路不进 Noteford。

产品路径已改为第 7 节：**只保留一个正版微信**。Noteford 检测到官网版 `WeChat.app` 后，引导用户做一次性 ad-hoc 重签：`/usr/bin/codesign` 去掉 Hardened Runtime，并加上 `get-task-allow`。重签后用户正常登录或重启这一个微信。Noteford 在数据库打开时用 interpose 钩住 `CCKeyDerivationPBKDF` 和 `CCCryptorCreate`，候选值交给现有 `verify_key` / `find_matching_key`，通过后写入钥匙串。之后在完全磁盘访问权限下只读复制数据库、合并 WAL，持续归档。密钥按库保存；某个库重建导致 HMAC 失败时，下次登录再取一次。保持 SIP 开启，不使用 sudo。临时副本方案已经放弃。

图片 XOR 仍从 `*_t.dat` 文件尾推算。图片 AES 走同一只 `CCCryptorCreate` 钩子，见第 7.7 节，结论待真机。

共享模块可以在 macOS 和 Linux 上导入。`winmem.py` 不再在导入时调用 `ctypes.WinDLL("kernel32")`；`kernel32` 只在 Windows 上第一次真正读进程内存时绑定。`refresh.py`、`selective.py`、`discovery.py`、`cli.py` 取数据库密钥走 `platform_seam.acquire_database_key_candidates`：Windows 仍是原来的内存扫描，macOS 读已有的 `keys.json`，或在显式 `capture=True` 时调用 `macos_database_key_source`。`image_crypto.py` 和 `cdn_session.py` 也改成调用时才加载 `winmem`。在非 Windows 上直接调用内存扫描会得到 `UnsupportedPlatform` 或 `PlatformCapabilityError`，而不是导入崩溃。这只拆开了加载方式，没有改扫描算法，也没有改 Mac 取钥的安全边界。

## 1. 目录、数据库与加密

### 1.1 本仓库已经实现的 Windows 4.x

源码：

| 行为 | 位置 |
| --- | --- |
| 候选根目录：`%APPDATA%\Tencent\xwechat\config\*.ini`，以及文档目录下的 `WeChat Files` / `Tencent Files` | `discovery.py` `candidate_roots` |
| 账号判定：目录内同时有 `db_storage/session/session.db` 和 `db_storage/contact/contact.db` | `libraries.py` `discover_account_directories` |
| 账号目录名可带 `_` 加 4 位十六进制后缀 | `libraries.py` `ACCOUNT_SUFFIX` |
| 页大小 4096，保留区 80（16 字节 IV + 64 字节 HMAC-SHA512） | `crypto.py` |
| 原始 32 字节密钥直接作为 AES-256-CBC 密钥；MAC 密钥是 `PBKDF2-HMAC-SHA512(key, salt XOR 0x3A, iterations=2)` | `crypto.py` `verify_key` |
| 第 1 页 HMAC 覆盖 `page[16:4032]`，比对 `page[4032:4096]`，并追加小端页号 1 | `crypto.py` |
| 数据库密钥来自 `Weixin.exe` 内存里的 `com.Tencent.WCDB.Config.Cipher`，配置块用固定 XOR 掩码解开 | `winmem.py` |
| 图片 V2 魔数 `07 08 56 32 08 07`，偏移 6 处两个小端 uint32（`aes_size`、`xor_size`），密文从偏移 15 开始，AES-128-ECB，尾部单字节 XOR | `image_crypto.py` |
| 图片 AES 从 `Weixin.exe` 私有堆的 32 字符可读串里取前 16 字节，并用真实 `.dat` 密文块验证 | `image_crypto.py` `scan_image_aes_key` |
| 图片 XOR 从 `msg/attach/**/*_t.dat` 尾部 JPEG `FF D9` 推出，不需要进程 | `image_crypto.py` `derive_xor_key` |
| 登录判断只认 `Weixin.exe` / `WeChat.exe`，非 Windows 直接返回 `unsupported_platform` | `login_status.py` |

`winmem.py` 的 XOR 掩码以 `d2c744245802` 开头，是 x86-64 指令字节。指针偏移（名称指针、长度、配置对象 `+0x88`）也是这套 Windows 对象布局。把它换成就 Mach 读内存，不能得到 Mac 上的密钥。

### 1.2 公开资料中的 Mac 微信 4.x

对照来源：

- [ylytdeng/wechat-decrypt](https://github.com/ylytdeng/wechat-decrypt) README、`find_all_keys_macos.c`、`decode_image.py`（2026 年仍在更新，README 写明 macOS 路径和 SQLCipher 4）
- [TANGandXUE/wcdb-key-tool](https://github.com/TANGandXUE/wcdb-key-tool) README 与 `wcdb_key_tool_macos.py`（作者称 Mac 4.1+ 的 LLDB 路径已在真机核对 18/18 个库）
- [0xd219b/wechat-cli](https://github.com/0xd219b/wechat-cli) 对新旧目录和 HMAC 参数的说明
- [Thearas/wechat-db-decrypt-macos](https://github.com/Thearas/wechat-db-decrypt-macos) 的 `verify_keys.py`
- [wechat-decrypt issue #96](https://github.com/ylytdeng/wechat-decrypt/issues/96)：4.1+ 不再把原始密钥明文留在内存
- SQLCipher 原始密钥与 HMAC：[Zetetic SQLCipher API](https://www.zetetic.net/sqlcipher/sqlcipher-api/)（`PRAGMA key = "x'<64 hex>'"` 跳过口令 KDF，HMAC 密钥仍用 2 次 PBKDF2）
- WCDB 上游：[Tencent/wcdb](https://github.com/Tencent/wcdb)

这些实现把数据目录写成：

```text
~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<账号>/db_storage/
```

应用包是 `/Applications/WeChat.app`，进程名 `WeChat`，bundle id `com.tencent.xinWeChat`。4.1 还有 `WeChatAppEx` 辅助进程。Linux 版微信使用 `~/Documents/xwechat_files/`，所以 Mac 上这个非沙盒路径也要看一眼。

[WeChatDataAnalysis issue #101](https://github.com/LifeArchiveProject/WeChatDataAnalysis/issues/101) 记录：较新的 4.1 客户端把正在使用的库放在 `Documents/app_data/xwechat_files`，旁边旧的 `Documents/xwechat_files` 可能是过期副本。用过期副本的 salt 做校验会失败。探测脚本和 Mac 同步都先看 `app_data`，两处都有账号标记时采用 `app_data`。哪一处才是洪云机器上的活动目录，待真机。

`wcdb_key_tool_macos.py` 的 `verify_enc_key` / `_decrypt_page` 与 `crypto.py` 逐项相同：页 4096、盐 16 字节、保留区 80、AES-256-CBC、HMAC-SHA512、盐逐字节 XOR `0x3A`、MAC 密钥 2 次 PBKDF2、第 1 页把盐替换成 `SQLite format 3\0`。页格式一致是对照结论。洪云机器上的文件是否就是这种头，待真机。

4.1.10 起，同一份工具先用 passphrase 做 `PBKDF2-HMAC-SHA512(passphrase, 每库 salt, 256000, 32)`，再用上面的页 HMAC 验收。派生之后的 32 字节才是 `crypto.py` 要的原始密钥。磁盘格式没有因此改掉。`platform_seam.derive_sqlcipher4_raw_key` 把这一步留成纯函数，供以后导入 passphrase 时使用。

旧版 Mac 微信 3.x 是另一套布局，本仓库不打算读：

```text
~/Library/Containers/com.tencent.xinWeChat/Data/Library/Application Support/com.tencent.xinWeChat/<版本>/<哈希>/
```

wechat-cli 把它标成 old format。那一代常用 SQLCipher 3 的 1024 字节页。探测脚本若只看到 `Message/*.db`、看不到 `db_storage`，会标成 `legacy_message_dir`。

### 1.3 差异

| 项目 | Windows 4.x（源码） | Mac 4.x（对照，待真机确认文件） |
| --- | --- | --- |
| 数据根 | ini 指向的目录，常见在文档库的 `xwechat_files` | 沙盒 `Documents/app_data/xwechat_files`；旁边的 `Documents/xwechat_files` 可能是过期副本 |
| 账号标记文件 | `db_storage/session/session.db` 与 `contact/contact.db` | 公开工具按同一相对路径收集 `.db` |
| 数据库页格式 | `crypto.py` 的 SQLCipher 4 原始密钥 | 公开校验函数与 `crypto.py` 相同 |
| 每库密钥 | 候选里可以带各自的 salt | 公开工具按 salt 分库，同一 salt 共用一把密钥 |
| 4.1+ 密钥材料 | 仍从 `Config.Cipher` 解出原始密钥（wcdb-key-tool 的 Windows 栏；本仓库生产路径） | 内存里不再长期保留 `x'<96 hex>'`，保留 passphrase |
| 进程 | `Weixin.exe` | `WeChat`，另有 `WeChatAppEx` |
| 读内存 API | `OpenProcess` + `ReadProcessMemory` | `task_for_pid` + `mach_vm_region` |
| 图片容器 | V2 魔数与 AES-ECB + XOR | `decode_image.py` 的字段布局与 `decrypt_v2_bytes` 相同 |
| 图片 AES 来源 | 进程堆里的 16 字节 ASCII | README 称可由磁盘 `kvcomm` 推出，不扫进程 |
| 图片 XOR | `*_t.dat` 尾字节 | 同一 JPEG 尾字节算法，路径仍写在 `msg/attach/.../Img/` |
| 配置发现 | `%APPDATA%\Tencent\xwechat\config\*.ini` | 公开工具不读 ini，直接固定容器路径 |

WAL：`crypto.py` 的 `merge_encrypted_wal` 假定 SQLite WAL 头 32 字节、帧头 24 字节、页载荷仍用同一页密码。SQLCipher 的 WAL 就是这个形状。Mac 工具的解密示例只解主库文件。WAL 头是否仍是 `37 7f 06 82/83`，由探测脚本读取前 4 字节回答，待真机。

### 1.4 决定格式是否相同的一次实验

探测脚本只看文件头。要确认能解密，需要在真机上拿一把已经由公开工具验证过的 32 字节原始密钥，对其中一个 `.db` 调用现有 `crypto.verify_key`。通过就说明页格式与 Windows 实现相同；失败则先停止复用 `crypto.py`，不要改探测脚本去扫内存。

这台云端机器做不了该实验。

## 2. 密钥方案

### 2.1 `task_for_pid` / `mach_vm_read`

做法：root 取得微信任务端口，枚举可读内存，在 4.0.x 上搜索 96 位十六进制字面量，再用第 1 页 HMAC 验收。

可行性：公开项目在重签名之后的 4.0.x 上做成了。4.1.10 之后同一搜索找不到原始密钥（issue #96，以及 wcdb-key-tool 把内存扫描降为 4.0.x 路径）。

对用户的要求：

- 用 sudo 运行扫描器。
- 先退出微信，执行 `codesign --force --deep --sign - /Applications/WeChat.app`，再打开微信。微信每次更新都可能把官方签名装回来，需要再做一次。
- 系统完整性保护（SIP）是否必须关闭，公开资料不一致。wechat-cli 写要关闭 SIP；ylytdeng 与 wcdb-key-tool 只要求 ad-hoc 重签名后 sudo。Apple 的 [Hardened Runtime](https://developer.apple.com/documentation/security/hardened-runtime) 与 [`com.apple.security.get-task-allow`](https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.security.get-task-allow) 说明：带 Hardened Runtime、且没有调试授权的发行版进程，普通程序不能取任务端口；root 对已去掉 runtime 标志的 ad-hoc 签名目标通常可以。SIP 本身见 [Apple 平台安全指南](https://support.apple.com/guide/security/system-integrity-protection-secb7ea06b49/web)。探测脚本会同时记下 `csrutil status`、codesign 的 runtime 标志和 `task_for_pid` 的 `kern_return`，用真机结果结束这个分歧。

合规与安全：

- 重签名会换掉腾讯的 Developer ID，完全磁盘访问、摄像头等按签名授予的权限会失效，微信自己的完整性检查也可能失败。
- 正式版 Noteford 要公证，就必须开 Hardened Runtime。第三方公证应用拿不到调试器授权，不能对别人的进程调用 `task_for_pid`。扫描器只能是用户单独运行的未公证工具。
- 把重签名和内存扫描放进安装包，等于让 Noteford 修改并调试另一个厂商的应用。Windows 版读 `Weixin.exe` 不需要破坏它的签名，Mac 上这一步的风险更高。

维护成本：微信一更新就要重新签名；4.0 与 4.1 的内存形态已经分叉；ARM 与 Intel 的调用约定不同。不适合作为产品主路径。

探测脚本会做一次附加尝试，只统计内存区数量，不调用 `mach_vm_read`，不把内存写进报告。附加失败是预期中的有用结果。

### 2.2 用户导入密钥

做法：用户用自己选择的外部工具导出原始密钥或 passphrase，在 Noteford 里选择该文件。记录形状沿用 `candidate_keys.json`：`key_hex` 为 32 字节，可选 `salt_hex` 16 字节。passphrase 则按第 1.2 节的 256000 次 PBKDF2 逐库派生后再交给 `find_matching_key`。

可行性：解密代码已经存在。导入本身不依赖 SIP、签名或微信版本的内存布局。微信若在重新登录后轮换 passphrase，用户需要再导入一次。这与 Windows 上重新扫描内存是同一类操作，只是步骤显式。

对用户的要求：本机已登录微信；愿意运行一次外部提取工具，或从该工具已经生成的 JSON 中导入。Noteford 不附带提取器。

合规与安全：密钥文件和现有 `libraries/<id>/secrets/` 一样敏感。Mac 上应用自己的数据密钥改放钥匙串（见第 4 节），不把提取过程放进 Noteford。用户需要知道外部工具会读取微信进程。

维护成本：Noteford 侧只维护导入、校验和存储。微信升级破坏的是外部提取工具，而不是页解密。

这是 2026-10-15 上午的建议。当天下午曾改为临时副本，晚上改为第 7 节的重签正版。`ImportedKeySource` 仍留在 `platform_seam.py`，供对照测试。

### 2.3 LLDB、动态库注入

做法：公开工具在重签名后的微信上，用 LLDB 断在 `CCKeyDerivationPBKDF` 或 `CCCryptorCreate`，要求用户退出账号再登录，从参数里读取 passphrase 或瞬时 AES 密钥。也有项目用 `DYLD_INSERT_LIBRARIES` 挂系统库。

可行性：wcdb-key-tool 把 LLDB 路径标成 Mac 4.1+ 的已验证手段。它依赖系统符号，不依赖微信被剥掉的私有符号，所以比硬编码偏移耐升级。前提仍是任务端口能加上，也就是第 2.1 节的重签名和 root。注入还要求目标允许外部动态库，官方 Hardened Runtime 默认不允许。

对用户的要求：安装 Xcode 命令行工具、重签名、sudo、在捕获窗口内重新登录微信。

合规与安全：调试器附加和向微信注入代码，都比只读打开数据库更接近控制另一个进程。公证应用不能携带 `get-task-allow` 或关闭库校验的授权来给自己开这条路。

维护成本：系统符号比微信偏移稳，但捕获时机绑在登录动作上。LLDB 附加正版进程仍然要任务端口，产品不走这条。

当前实现用同一组 CommonCrypto 符号，通过装进正版包内的启动脚本做 interpose。`com.apple.security.cs.debugger` 是受限制授权，Developer ID 公证不会发给普通应用，所以辅助进程不走调试器附加。流程在第 7 节。

### 2.4 微信钥匙串

本次没有找到可靠资料证明 Mac 微信 4.x 把 WCDB 原始密钥或 passphrase 放在登录钥匙串里。4.1+ 的公开分析把材料放在进程内存和登录时的 CommonCrypto 调用里。

不能据此写成「钥匙串里一定没有」。探测脚本**不**调用 `security` 导出密码。若以后要查，只列出服务名，不打印 secret。在有阳性证据之前，不把钥匙串当作密钥来源。

### 2.5 图片密钥

XOR：算法在 `derive_xor_key`，只读缩略图文件尾。只要真机存在 `*_t.dat`，就可以复用，不必扫进程。路径是否仍是 `msg/attach/**/Img/*_t.dat`，待真机。

AES：Windows 必须扫进程，密钥是 16 字节 ASCII，算法是 AES-128-ECB。ylytdeng README 写 Mac 用 `find_image_key_macos.py` 从 `kvcomm` 缓存推导。该文件本次返回 HTTP 451，算法没有复核，不能写成可用来源。探测脚本只报告名字里带 `kvcomm` 的目录。

同一只 `CCCryptorCreate` 钩子会交出 16 字节 AES 密钥。能否在登录或打开图片时拿到、并被一张 V2 `.dat` 验过，见第 7.7 节，待真机。`image_keys.json` 的字段形状保持 `aes_key_ascii` 与 `xor_key`。

### 2.6 已被取代的建议

下面是拍板之前的建议，不再作为产品路径：

1. 真机先跑只读探测，确认版本、目录、页对齐、WAL 魔数和 V2 魔数。这一步仍然要做。
2. 页格式仍以 `crypto.verify_key` 为准。
3. 手动导入密钥，以及后来的临时副本，都已放弃。当前是第 7 节：一次性重签正版，登录时取密钥，然后持续只读归档。关闭 SIP 和 sudo 仍然不进 Noteford。
4. 图片 XOR 复用现有文件算法。图片 AES 用同一只 `CCCryptorCreate` 钩子，见第 7.7 节。

## 3. 真机探测

`scripts/mac-probe/probe.py` 只用 Python 标准库。说明在 `scripts/mac-probe/README.md`。

它会记录：微信 `Info.plist` 版本和 bundle id、codesign 是否带 runtime / ad-hoc / `get-task-allow`、`csrutil status`、`WeChat` 与 `WeChatAppEx` 进程、容器内 `Documents/app_data/xwechat_files`、`Documents/xwechat_files` 和 `~/Documents/xwechat_files` 是否可读、账号布局、数据库头部分类、WAL 头 4 字节、最多 24 个 `.dat` 的魔数、`kvcomm` 目录名。输出写到用户指定的目录。

限制：不写微信目录，不解密，不提取密钥，不重签名，不改 SIP。附加成功时只数内存区。Linux 上 `--self-test` 用临时样例检查分类器，并核对样例文件的大小和时间戳没有变化；本次已在云端 Linux 跑过 `--self-test`。这不能代替真机报告。

分类名 `possible_sqlcipher4_page4096` 只表示「不是明文 SQLite、长度是 4096 的倍数」。它不是解密成功。

## 4. 平台抽象

共享的取钥调用已经接到 `platform_seam.acquire_database_key_candidates`：Windows 行为留在 `winmem`，macOS 不扫描内存。桌面启动、托盘、服务命令、发现记录和资料来源的平台差异在 `platform_seam.py` 与 `desktop/electron/platform-seam.cjs`。下面是各边界的现状。

| 边界 | 现在 | Mac 目标 | 桩 |
| --- | --- | --- | --- |
| 安装版数据根 | `%LOCALAPPDATA%\Noteford`；便携布局仍看 EXE 旁的 `.portable-layout` 或 `app/` 父目录 | `~/Library/Application Support/Noteford`。便携目录规则可保留，但是 Mac 的正常分发物是 `.app` | `default_data_root` |
| 微信搜索根 | `discovery.candidate_roots` | 第 1.2 节的容器路径。ini 扫描先不搬 | `wechat_search_roots` |
| 进程名 | `Weixin.exe` | `WeChat`、`WeChatAppEx` | `wechat_process_names` |
| 数据库密钥 | `acquire_database_key_candidates` 在 Windows 上调用 `winmem.scan_config_cipher_candidates` | 一次性重签正版后，登录时 interpose，HMAC 通过写入钥匙串 `noteford-wechat-db-keys`。内存扫描直接拒绝；已有密钥读 `keys.json` | `macos_database_key_source`、`mac_resign`、`MacMemoryKeySource` |
| 应用机密 | DPAPI 包住 `mcp_remote.key` / `mcp_local.key`（`remote_mcp.py`） | 钥匙串一项：服务 `com.followhongyun.noteford`，账户 `noteford-data-key`，`kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`。磁盘上只留「密钥在钥匙串」的标记，不写密钥字节。换机器或换用户后要重新配对，这一点和 DPAPI 相同 | `MacKeychainSecretProtector` |
| 写锁 | `runtime_lock.py`、`local_pairing.py` 已按 `win32` / `fcntl` 分支 | 继续用 `fcntl.flock` | `writer_lock_backend` 只返回名字 |
| 服务文件 | `resources/service/noteford-service.exe`（`main.cjs`、`skill_connection.py`） | 同目录下无后缀的 `noteford-service`，PyInstaller `--onedir` 产物，随 `.app` 签名 | `service_executable_name` |
| 发现记录 | `%LOCALAPPDATA%\Noteford\connection.json` | `~/Library/Application Support/Noteford/connection.json`。开发运行和安装包都会写。安装包里的服务是冻结后的 `noteford-service`。开发运行仍可以是 `python3 -m noteford.service`。客户端另有 `noteford.sh` / `noteford.py` | `mcp_discovery_file`、`service_command_acceptable` |
| 登录状态 | `login_status.py` 枚举进程已打开文件；非 Windows 返回 `unsupported_platform` | 在完全磁盘访问下用 `psutil.Process.open_files()`，进程名改走上面的表 | 未改 |
| 托盘 | `Tray`，点按显示，关闭窗口隐藏，菜单只有「退出」。图标仍是 `.ico` | 菜单栏用模板 PNG。加载失败只记日志，窗口继续可用。Mac 上关闭窗口仍隐藏，Dock 图标保留；菜单有「打开」和「退出」 | `trayIconPath`、`presentTray` |
| 标题栏 | `titleBarOverlay` | 使用系统红绿灯。`titleBarOverlay` 是 Windows 覆盖层。darwin 左侧竖栏导航，并为红绿灯留出顶部空位 | `desktop/electron/window-chrome.cjs`、`desktop/src/App.tsx` |
| 开机启动 | 当前用户「启动」文件夹里的快捷方式，参数 `--background`（`docs/OPERATIONS.md`）。`Scripts/install-daily-task.ps1` 是每日任务，不是登录项 | `app.setLoginItemSettings({ openAtLogin: true })`，参数仍带 `--background`。需要登录项说明时用 `SMAppService` | 只写在本文 |
| 打包 | `electron-builder --win nsis`，`Scripts/build-windows.ps1`。`package:win`、NSIS 和 `build.win.icon` 未改。窗口图标 `noteford-book-v1.ico` 和 Skill 脚本 `noteford.ps1` 改由 `build.win.extraResources` 打进安装包，安装后的路径与原来相同。`app.asar` 里的 `.ico` 和 `.ps1` 由 `build.win.files` 加回。Windows 的 beforePack 仍用 `.venv-build/Scripts/python.exe`，并继续校验原来的 ffmpeg 源码包 | `npm run package:mac` 产出 arm64 `.dmg` 和给自动更新用的 `.zip`，里面是 `.app`。Resources 不含 `noteford-book-v1.ico` 和 `skills/noteford/scripts/noteford.ps1`，`noteford-src` 的 Python 过滤掉 `winmem.py`。`mac.files` 里的排除减不掉共用 `files` 已选进 `app.asar` 的文件，所以共用清单在末尾排除全部 `.ico` 和 `.ps1`，Windows 的 `files` 再把这两类加回 `app.asar`。`noteford.sh` 和 `noteford.py` 仍在。Mac 的 beforePack 只用 `.venv-build/bin/python3` 冻结服务（没有这个虚拟环境时，打包机上的 `python3 -m venv` 会先建好它），不再下载那份 Windows ffmpeg 源码包。包内的 `libffmpeg.dylib` 必须与本机 Electron 自带的预编译库一致。beforePack 必须产出 arm64 的 PyInstaller `--onedir` 服务；产物若是调用系统 `python3` 的脚本，或不是 arm64 Mach-O，打包中断。安装后的应用不再退回 `python3 -m noteford.service`。公证和 Apple 证书仍未做 | `desktop/scripts/mac-pack.cjs`、`desktop/package.json` 的 `mac` |
| 开发脚本 | `Scripts/*.ps1` | Mac 开发用 shell 入口，另做。本次不增加 | 无 |

微信数据库钥匙串与上表的应用机密是两项。应用机密由 `MacKeychainSecretProtector` 写入登录钥匙串（账户 `noteford-data-key`），磁盘上的 `mcp_local.key` / `mcp_remote.key` 只保留标记。数据库密钥和图片 AES 仍由 `mac_capture.SecurityKeychainStore` 写入，账户分别是 `noteford-wechat-db-keys`、`noteford-wechat-image-aes`，服务名同为 `com.followhongyun.noteford`。

导入期绑定已经拆开：`WinDLL` 在 `winmem._ensure_windows` 里，只在 Windows 上第一次读内存时执行。`discovery.py`、`image_crypto.py`、`refresh.py`、`selective.py`、`cdn_session.py` 和 `cli.py` 导入时不再加载 `winmem.py`。`platform_seam.py` 在导入时仍不加载 `winmem.py`，也不加载 `mac_capture.py`；`macos_database_key_source` 到被调用时才导入。Windows 上的扫描函数、XOR 掩码和 `candidate_keys.json` 写法保持原样。

## 5. 工作量

用户要求的 3 到 5 天验证，对应的是本文、探测脚本和接口桩。它不包含能在 Mac 上打开资料库的应用。下面按组件估，不把日历天数当成承诺。

| 后续工作 | 规模 | 前提 |
| --- | --- | --- |
| 拆开 `WinDLL` 的导入期绑定 | 已在源码完成。共享模块可以导入；Windows 扫描仍只在 win32 上执行 | 无 |
| 按探测结果参数化目录和进程名 | 小 | 真机报告与第 1.2 节一致 |
| 复用 `crypto.py` 解密和 WAL 合并 | 小；HMAC 一次通过则解密函数不用改 | 重签后的登录捕获在真机上让 `verify_key` 通过 |
| 重签、登录捕获、只读同步原型 | 已写在本分支。云端不能编译 dylib，也不能登录微信 | 第 7.9 节 |
| Mac 最小入口 | Electron 的 `noteford:mac-wechat` 与 `python -m noteford.mac_entry`。`noteford.service` 已能在非 Windows 上导入；真机打开资料库仍待密钥与文件布局验收 | 导入期绑定已拆开 |
| 手动导入密钥、临时副本 | 不再是产品路径 | — |
| 图片 XOR；图片 AES 走同一只钩子 | XOR 可复用。AES 的 16 字节过滤和密文块校验已写，调用时机待真机 | 第 7.7 节 |
| Electron 数据根、服务名、菜单栏、登录项、标题栏 | 中。窗口壳可以先于微信数据做 | 无 |
| 签名、公证、嵌套 Python 二进制、Mac 自动更新 | 大 | Apple 证书和一台可公证的 Mac |
| 语音 SILK、WXGF / FFmpeg 的 Mac 二进制 | 独立，可后置 | 数据库阅读已经可用 |
| 把内存扫描做进应用 | 不做 | — |

若真机文件头不是 4096 对齐，或 `verify_key` 失败，解密会从「复用」变成「先解释新的页格式」，规模不再是小。探测脚本就是为了在写那些代码之前看到这个结果。

## 6. 待决问题

1. 真机微信的 `CFBundleShortVersionString` 是 4.0.x 还是 4.1.10 及以后。
2. 账号目录是否在容器内的 `xwechat_files`，标记文件是否仍是 `session.db` 与 `contact.db`。
3. 数据库大小是否为 4096 的倍数，且文件头不是 `SQLite format 3`。
4. `.db-wal` 前 4 字节是否为 `377f0682` 或 `377f0683`。
5. `.dat` 是否为 V2 魔数，缩略图是否名为 `*_t.dat`。
6. 是否存在 `kvcomm` 目录。
7. 未重签名、SIP 保持开启时，普通用户和 root 的 `task_for_pid` 分别返回什么。
8. 重签后的登录捕获拿到的 32 字节候选能否通过 `crypto.verify_key`。这仍是「Mac 文件可以用现有解密函数打开」的分界，完成前保持为对照后的推断。
9. 微信更新后，Noteford 是否能凭版本号和签名状态提示再次重签。代码已写，真机更新待观察。
10. 阅读容器目录是否要求完全磁盘访问权限。
11. 只做 Apple Silicon，还是同时做 Intel。钩子 Makefile 默认同时编 arm64 与 x86_64；真机先在 Apple Silicon 上跑。
12. 重签后从 Dock 打开微信，启动脚本是否真的把 interpose 库载入进程。待真机。
13. 打开一张聊天图片后，`image_fingerprint` 是否出现。待真机。
14. 哪些校验签名的小程序会报错。待真机记下名字即可。
15. 从官网覆盖安装后，`codesign -dv` 是否回到腾讯 Developer ID。待真机。

## 7. 重签正版微信，登录时取密钥，然后持续归档

洪云最终决定放弃临时副本。Mac 版只操作一个正版微信：一次性 ad-hoc 重签，之后用户照常登录和重启，Noteford 在数据库打开时取密钥，并用完全磁盘访问权限持续只读归档。保持 SIP 开启，不使用 sudo。分发是私域的 Developer ID 包，不进 Mac App Store。

对照来源仍是 [wechat-cli MACOS-KEY-CAPTURE.md](https://wechat-cli.com/MACOS-KEY-CAPTURE.md) 和 [WeChatDataAnalysis issue #101](https://github.com/LifeArchiveProject/WeChatDataAnalysis/issues/101)。前者证明 interpose 能在 4.1.13 官网包上抓到可校验的密钥，但他们用的是副本。本方案把同一只钩子装进重签后的正版本体。issue #101 确认 `rounds=2` 的 password 才是主密钥，并提醒重签可能让登录态失效。

云端是 Linux。dylib 没有编译，微信没有启动。下面凡是依赖真机进程的句子都标了待真机。

### 7.1 用户流程

1. Noteford 查看 `/Applications/WeChat.app`。bundle id 必须是 `com.tencent.xinWeChat`，且 `codesign -dv` 同时含有 `Developer ID Application` 和 `Tencent`。App Store 签名直接拒绝。
2. 若状态是 `needs_resign`，界面提示一次性重签，并说明第 7.4 节的副作用。用户确认后继续。
3. 退出微信。用 `/usr/bin/codesign --force --deep --sign - --entitlements <清单>` 重签这个正版包。清单只有 `com.apple.security.get-task-allow`。命令不带 `--options runtime`，从而去掉 Hardened Runtime。不使用 sudo。
4. 同一次操作把 `Contents/MacOS/WeChat` 换成启动脚本，原 Mach-O 改名为 `WeChat.bin`。脚本设置 `DYLD_INSERT_LIBRARIES`、`NOTEFORD_CAPTURE_SOCKET` 和 `NOTEFORD_CAPTURE_PATH` 后 exec 原二进制。Dock 和 Finder 会丢掉 `DYLD_*`，脚本在进程启动前把变量加回去。先改脚本再签名。
5. 用户重新登录这一个微信。4.1.10 及以后，派生发生在登录打开数据库时。
6. Noteford 监听本机 socket。候选值用 `find_matching_key` 校验。至少一个账号同时通过 `session/session.db` 和 `contact/contact.db` 才写入钥匙串。微信保持打开，不删除、不还原这个包。
7. 之后按第 7.6 节持续同步。某个库 HMAC 失败时，状态变成 `recapture`，提示用户重启微信以再次取密钥，不必每次都重签。
8. 微信更新把官方签名装回来，或版本号与 `~/.noteford/resign.json` 不一致时，状态回到 `needs_resign`，Noteford 提示再签一次。

### 7.2 技术架构

```text
/Applications/WeChat.app
  Contents/MacOS/WeChat      启动脚本
  Contents/MacOS/WeChat.bin  原来的 Mach-O
        | exec + DYLD_INSERT_LIBRARIES
        v
libnoteford_wechat_hook.dylib
        | JSON 行
        v
~/.noteford/capture.sock 或 capture.jsonl
        v
mac_capture.capture_login
        | verify_key / find_matching_key
        v
钥匙串 com.followhongyun.noteford
        v
mac_sync：只读快照 + decrypt_database + merge_encrypted_wal
```

钩子仍是 `native/macos/wechat_hook/hook.c`。它 interpose `CCKeyDerivationPBKDF` 和 `CCCryptorCreate`，不调用 `task_for_pid`。JSON 行带上 rounds、salt、password、derived，供第 7.3 节过滤。

调试器辅助进程需要 `com.apple.security.cs.debugger`。这是受限制授权，Developer ID 公证不会把它发给普通私域应用。因此实现选择 interpose，而不是让 Noteford 自己去 `task_for_pid`。重签时加上的 `get-task-allow` 只放在这一个被重签的微信上，Noteford 自己的公证包不带这个键，也不带调试器授权。

`mac_resign.assess_wechat` 根据签名文本、启动脚本和记录的版本给出 `ready` 或 `needs_resign`。原因包括 `first_resign`、`update_restored_signature`、`version_changed`、`foreign_adhoc`、`app_store_wechat`。`resign_official` 在 codesign 失败时把 `WeChat.bin` 移回原位。

入口：

| 调用方 | 位置 | Windows 上的行为 |
| --- | --- | --- |
| Python | `python3 -m noteford.mac_entry status\|resign\|watch\|sync` | 非 darwin 的 resign/watch/sync 返回 `darwin_required`，不改文件 |
| Electron | 仅 `process.platform === 'darwin'` 时注册 `noteford:mac-wechat` | 不注册，原 `noteford.service` 启动代码保持原样 |
| 已运行的 Windows 服务 | `GET/POST /api/v1/mac/wechat` | 返回 `windows_key_path_unchanged` |

`noteford.service` 导入时不再加载 `winmem.py`，因此 Mac 上可以加载共享服务、监控和 MCP。密钥、重签和只读同步仍走上面的最小入口；内存扫描在非 Windows 上直接拒绝。

### 7.3 版本兼容

活动库目录仍优先 `Documents/app_data/xwechat_files`。

| 调用 | 条件 | 当作数据库密钥的字节 |
| --- | --- | --- |
| `rounds=256000` | salt 等于某个库第 1 页 salt | `derived` |
| `rounds=2` | salt 等于该页 salt 逐字节 XOR `0x3A` | `password` |
| 其他 rounds，或 salt 对不上 | 丢弃 | 无 |

`rounds=2` 的 `derived` 是 HMAC 子密钥，不拿去解密。`rounds=256000` 的 `password` 是 passphrase，也不直接解密。两条都要过 `crypto.verify_key`。4.1.10 及以后必须在登录时抓。4.1.13 build 269578 的 `rounds=2` 来自 issue #101，单测已覆盖过滤和 HMAC，洪云机器上的版本待确认。

### 7.4 重签的副作用

这些是重签正版本体的预期后果，不是每次同步都会发生：

- 签名和 CDHash 改变后，微信登录态失效，需要重新扫码。完全磁盘访问、摄像头等按签名授予的权限也可能要重新允许。待真机确认实际掉了哪些授权。
- 个别会校验微信签名的小程序可能打不开或报错。具体名单待真机。
- 微信更新会用官方包覆盖签名和启动脚本。Noteford 比较 `CFBundleShortVersionString`、`CFBundleVersion` 和当前签名，不一致就提示重签。
- 从官网覆盖安装可恢复腾讯 Developer ID。本机没有腾讯证书，不能靠 `codesign` 把官方签名签回去。恢复步骤在第 7.9 节。

### 7.5 签名、公证与分发

Noteford 的 Mac 包使用 Developer ID，做公证，打开 Hardened Runtime，不进 App Store。钩子只注入用户本机已经重签的微信，不随包去替换别人的 `/Applications/WeChat.app`。目标微信是官网版。App Store 版重签后可能被系统拒绝启动，代码遇到 App Store 签名会返回 `app_store_wechat` 并且不改包。

### 7.6 持续同步归档

有完全磁盘访问权限后，Noteford 只读读取容器里的数据库。`mac_sync.sync_databases` 调用现有 `snapshot.copy_database_snapshot` 把 `.db` 和旁边的 `-wal` 复制到 Noteford 数据目录，再用该库自己的密钥调用 `decrypt_database` 和 `merge_encrypted_wal`。源文件只被读取。

密钥按库存放。同一 salt 可以复用。复制件的第 1 页 HMAC 失败时，该库状态是 `recapture`，不把旧密钥当成新库的密钥。这覆盖数据库重建后 salt 改变的情况。下次用户登录或重启微信时重新捕获。`refresh.py` 已能在 Mac 上导入。它在需要新密钥时经 `platform_seam` 分发：Windows 仍写原来的内存扫描结果；macOS 使用已捕获的 `keys.json`，不会改走内存扫描。持续归档本身仍是 `mac_sync`。

### 7.7 图片 AES

Windows 的图片 AES 是 16 字节 ASCII，对 V2 `.dat` 偏移 15 起的数据做 AES-128-ECB。同一只 `CCCryptorCreate` 钩子已经记录 `key_len=16`。原型只接受 16 字节字母数字，并用调用方提供的 16 字节密文块解密；明文以 JPEG、PNG、GIF、RIFF 或 `wxgf` 开头才写入钥匙串账户 `noteford-wechat-image-aes`。

这一步待真机。登录可能不解密图片，要在微信里点开一张聊天图片。数据库密钥不依赖图片结果。`kvcomm` 磁盘推导没有复核到脚本。XOR 仍从 `*_t.dat` 文件尾计算。

### 7.8 云端已经验证的范围

`PYTHONPATH=src python3 -m pytest tests/test_macos_feasibility.py tests/test_mac_capture.py`：17 项通过。覆盖 rounds 过滤、构造页 HMAC、重签落在正版本体上、签名失败时恢复 Mach-O、版本变化提示、App Store 包拒绝、登录捕获不复制微信、覆盖不足不写钥匙串、只读同步与 HMAC 失败触发重取、Windows 路由返回 `windows_key_path_unchanged`、导入这些模块时不加载 `winmem`。`scripts/mac-probe/probe.py --self-test` 仍通过。

待真机：编译通用 dylib、对真实微信执行重签、Dock 启动是否载入钩子、扫码后的 HMAC、钥匙串项、图片 AES、小程序签名报错、更新后的提示、官网覆盖安装是否恢复腾讯签名。

### 7.9 洪云真机步骤

在自己的 Mac 上做。保持 SIP 开启。不要加 sudo。报告里不要贴数据库、salt、密钥或账号目录名。

1. `csrutil status` 应含 `enabled`。
2. 重签前抄两行，后面用来对比：

```bash
codesign -dv --verbose=4 /Applications/WeChat.app 2>&1 | grep Authority
defaults read /Applications/WeChat.app/Contents/Info CFBundleShortVersionString
```

Authority 应同时有 `Developer ID Application` 和 `Tencent`。

3. 把终端加入「系统设置 → 隐私与安全性 → 完全磁盘访问权限」。
4. 编译钩子：

```bash
cd native/macos/wechat_hook && make
file libnoteford_wechat_hook.dylib
```

`file` 应同时有 `arm64` 和 `x86_64`。云端 Linux 编不了。若通用库失败，试 `make ARCHS=arm64`，并在回报里写明。

5. 先看状态，不改微信：

```bash
python3 scripts/mac-capture/run.py status \
  --app /Applications/WeChat.app \
  --state "$HOME/.noteford"
```

期望 `reason` 为 `first_resign`。

6. 一次性重签。这会退出微信，改 `/Applications/WeChat.app`，然后重新打开它：

```bash
python3 scripts/mac-capture/run.py resign \
  --app /Applications/WeChat.app \
  --hook native/macos/wechat_hook/libnoteford_wechat_hook.dylib \
  --state "$HOME/.noteford"
```

等价的签名命令是：

```bash
/usr/bin/codesign --force --deep --sign - \
  --entitlements "$HOME/.noteford/entitlements.plist" \
  /Applications/WeChat.app
```

授权清单里只有 `com.apple.security.get-task-allow`，没有 Hardened Runtime。脚本会先装启动器再调用它。不要对这个包再加 `--options runtime`。

7. 在重新打开的微信里扫码登录。然后：

```bash
python3 scripts/mac-capture/run.py watch \
  --app /Applications/WeChat.app \
  --state "$HOME/.noteford" \
  --timeout 180
```

成功时 JSON 的 `error` 为 `null`，`databases` 里有 `fingerprint` 和 `source`。把这份 JSON 发回。不要导出钥匙串。

8. 再跑一次 `status`。`state` 应为 `ready`。`codesign -dv` 现在应是 ad-hoc，这是预期结果。

9. 持续同步需要本机已经有数据库目录。把活动库的父目录传给 `--db-root`（不要把路径发出来）：

```bash
python3 scripts/mac-capture/run.py sync \
  --state "$HOME/.noteford" \
  --project "$HOME/.noteford/archive" \
  --db-root /路径/xwechat_files
```

`needs_recapture` 为 true 时，重启微信再跑一次 `watch`。源数据库应只被读取。

10. 图片 AES 另跑一次 `watch`，加上 `--image-dat /路径/某张图.dat`，并在微信里点开一张聊天图片。`image_fingerprint` 为空只说明这次没有拿到能解开该块的 16 字节密钥。

11. 恢复官方签名：退出微信，从微信官网下载安装包，覆盖安装 `/Applications/WeChat.app`，然后删除 `$HOME/.noteford/resign.json`。容器里的聊天记录不必删。再跑第 2 步的 `codesign -dv`，Authority 应回到重签前的腾讯行。不要尝试用 `codesign` 伪造腾讯签名。
