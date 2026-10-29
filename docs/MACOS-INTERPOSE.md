# macOS 取钥主路线：DYLD interpose 注入

> 历史原型/阶段验收，已被后续实现取代；下文“当前”“明早”“待接入”均指原记录阶段，不是日常操作指令。现行流程只操作临时微信副本，不改原版安装；Mac 密钥仅持久保存在钥匙串，`keys.json` 只是旧迁移位置，不得恢复明文运行方案。当前边界见 [数据与安全](DATA-AND-SECURITY.md)、[运行指南](OPERATIONS.md)，安装验收见 [STATUS](STATUS.md)。

目标：洪云对副本**扫一次码**即可稳抓密钥，不靠事后挂 LLDB，能扛住微信反篡改自检，
SIP 保持开启，不碰 /Applications/WeChat.app。

## 原理
- 微信 4.1.10+ 为「共用 passphrase」模型：登录瞬间内存里有一个 32 字节口令，
  各库密钥 = PBKDF2-HMAC-SHA512(passphrase, 每库 salt, 256000, 32)。抓到这一次
  派生的**输入**即可离线派生所有库（含没点开的聊天库），无需点聊天。
- 用 `__DATA,__interpose` 做的钩子 dylib 通过 `DYLD_INSERT_LIBRARIES` 注入副本，
  **开机即在位**，在微信首次 crypto 调用之前就拦截 `CCKeyDerivationPBKDF`，
  比 LLDB 事后 attach（落在最早那次 PBKDF 之后）更稳——这正是此前「副本活满却零命中」
  的大概率根因。

## 关键工程点
1. 钩子 `native/macos/key_hook/wechat_key_hook.c`
   - interpose `CCKeyDerivationPBKDF`（记录输入 passphrase=pbkdf_in、输出=pbkdf_out）
     与 `CCCryptorCreate`（记录 32B AES key=aes_key，双保险）。
   - 只在 rounds≥1000 或 dkLen==32 时记录，减噪；**只写 0600 的 JSONL 候选文件，
     绝不写 stdout/日志**。
   - constructor 写一条 `hook_loaded` 标记，证明 dylib 真被注入。
   - 编译：`clang -arch arm64 -dynamiclib -O2 wechat_key_hook.c -o libwechat_key_hook.dylib -framework CoreFoundation`
2. 合并式重签（`mac_resign.merged_entitlements_plist`）
   - 保留微信原 entitlements，并入三项注入关键授权：
     - `com.apple.security.get-task-allow`
     - `com.apple.security.cs.allow-dyld-environment-variables`（否则内核会剥离 DYLD_INSERT_LIBRARIES）
     - `com.apple.security.cs.disable-library-validation`（否则主进程拒载 ad-hoc/异团队钩子）
   - 只签主 bundle（不 --deep），嵌套组件保留原版 Tencent 签名以过自检。
   - 导出原 entitlements 失败必须**报错中止**，不静默回退最小授权。
3. 注入时把钩子 dylib 拷进副本 bundle（`Contents/Frameworks/`），即使副本保留
   app-sandbox 也能读到；启动副本时设 `DYLD_INSERT_LIBRARIES` 与 `WECHAT_KEY_HOOK_OUT`。

## 流程
`python -m noteford.mac_entry capture --acknowledge-debug-copy`
（默认走 interpose；加 `--lldb` 用旧 LLDB 回退路线）
→ prepare 副本（合并式重签）→ build_hook → 注入启动 → 洪云扫一次码
→ 读候选 passphrase → `derive_passphrase_keys` 离线派生 + page1 HMAC 逐库验真
→ 写 keys.json(0600) + keychain。

## 已验证（本地，无需登录）
- 独立 C 程序注入自测：hook_loaded + pbkdf_in(=passphrase) + pbkdf_out 正确写入 0600 文件。
- 合并 entitlements 在真实微信 bundle 上签名+校验通过，三项注入授权确实写入。
- 离线派生/验真链路（passphrase 与 direct-key 两路）单测通过。

## 尚未验证（需洪云在场 / 需退出其正在用的微信）
- 把副本真正启动 + 注入 + 存活 ≥3 分钟不被 exit 255 + 弹登录窗 + 扫一次码抓到真钥。
  这一步会短暂退出他正在使用的微信，须他在键盘前扫码，故留待其方便时执行。
