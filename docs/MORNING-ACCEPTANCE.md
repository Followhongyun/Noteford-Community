# 明早验收：Mac 取钥（复制副本 + LLDB + CommonCrypto）

> 历史原型/阶段验收，已被后续实现取代；下文“当前”“明早”“待接入”均指原记录阶段，不是日常操作指令。现行流程只操作临时微信副本，不改原版安装；Mac 密钥仅持久保存在钥匙串，`keys.json` 只是旧迁移位置，不得恢复明文运行方案。当前边界见 [数据与安全](DATA-AND-SECURITY.md)、[运行指南](OPERATIONS.md)，安装验收见 [STATUS](STATUS.md)。

## 已完成（今晚，无需扫码）

- 废弃「重签原版 + DYLD 注入」
- 落地「只签临时副本 + LLDB Launch + 断 CCKeyDerivationPBKDF + 逐库 HMAC」
- CLI：`status` / `prepare` / `capture` / `restore` / `sync`
- 单测覆盖 prepare、HMAC、假 LLDB 捕获、sync 复用

## 你要做的唯一实机步骤（需扫码）

1. **菜单正常退出微信**（不要 `killall`），确认 `pgrep -x WeChat` 无输出。
2. 在 Mac 终端进入源码/worktree，激活 venv：

```bash
cd ~/Noteford/mac-pipeline-wt   # 或已同步改动的仓
source ~/Noteford/源码/.venv/bin/activate
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
```

3. （可选）先只准备副本，不扫码：

```bash
python -m noteford.mac_entry prepare
```

4. **取钥 + 扫码**（核心验收）：

```bash
python -m noteford.mac_entry capture --acknowledge-debug-copy --timeout 300
```

临时微信窗口出现后：点进入 / 扫码 / 手机确认。**不要在副本里聊天。**

5. 成功标志（输出里）：
   - `verified_progress` / `hmac_pass` ≥ 核心库（session + contact）
   - `error` 为 null
   - `keys.json` 在 `~/Noteford/应用/数据/keycap/` 且权限 0600
   - 原版 `/Applications/WeChat.app` 签名仍是腾讯 Developer ID
   - `csrutil status` 仍为 enabled

6. 只读同步解密：

```bash
python -m noteford.mac_entry sync
```

## 约束提醒

- 不改原版、不用 sudo、不关 SIP
- 原版与副本不同时开
- 密钥不进日志；UI/JSON 摘要只有 fingerprint 与数量

## 若失败

| 现象 | 处理 |
|------|------|
| `quit_wechat_normally_first` | 菜单退出后再跑 |
| `launch_failed` | 系统设置里允许开发者工具/调试；不要关 SIP |
| `coverage_incomplete` | 登录后在副本点开聊天/收藏触发更多开库，或加大 `--timeout` |
| `app_store_wechat` | 换官网 Developer ID 安装包 |


## 第 3 轮探测后的结论（2026-10-16）

- SQLCipher/WCDB 在 `WeChat.app/Contents/Resources/wechat.dylib`（~358MB），**不在** `libwxld.dylib`。
- `wechat.dylib` **导入** `_CCKeyDerivationPBKDF` / `_CCCryptorCreate` / `_CCHmac*`；开库时应能命中系统 CommonCrypto。
- `libwxld.dylib` 几乎 strip，只导出 `LdMain`，导入 `CCCrypt` + SecCode*（启动自检）。
- 先前 auto-continue 脚本回调 **不会触发**（self-test 已证伪）；已改为 event-loop。
- 登录成功 ≠ 已开消息库。扫码后请 **点开几个聊天 / 允许手机同步历史**，再等 `probe_hit`。
- 备用：`wechat.dylib` 内 `InitCipherConfig` 字符串 xref（arm64 VA）`0x5c72ab4` 等 10 处，worker 会在模块加载后尝试挂 offset 断点。

Self-test（无需扫码）：

```bash
export PYTHONPATH="$(/usr/bin/lldb -P)"
/usr/bin/python3 src/noteford/mac_lldb_worker.py --self-test
# 期望 self_test_result.ok=true，且有 CCKeyDerivationPBKDF probe_hit
```

## 更新（2026-10-16 夜）：改为「只扫一次码、不点聊天」取钥

### 为什么能只扫码、不点聊天
两个独立项目已验证微信 Mac **4.1.10+ 是「共用 passphrase」模型**：
- `TANGandXUE/wcdb-key-tool`（真机 18/18）、`wechat-cli.com`（4.1.13 实测 27/29）。
- 登录/初始化时微信就会开 session.db、contact.db 等核心库，调用系统
  `CCKeyDerivationPBKDF`，内存里只存**一个 32 字节 passphrase，所有库共用**。
- 每个库 `enc_key = PBKDF2-HMAC-SHA512(passphrase, 该库salt, 256000, 32)`。
  → **抓到登录那一次 passphrase（函数输入 x1），就能离线派生所有库**（含从没点开的
  message/聊天库），**不需要点进聊天、不需要退登重登、不关 SIP**。

### 取钥管线改了什么
1. **抓共用输入 passphrase + 离线派生全部库**（`mac_lldb_worker.py`）
   - 断 `CCKeyDerivationPBKDF`，命中 256000 轮 SHA512 调用时读**输入 x1=passphrase
     (x2=passwordLen)**，不再逐库等输出 enc_key。
   - 新增纯函数 `derive_enc_key` / `derive_passphrase_keys` / `try_passphrase`：
     对 xwechat_files 下**每个库各自的 salt** 用 PBKDF2-SHA512(passphrase, salt, 256000, 32)
     派生，再用 page-1 HMAC-SHA512 验真（mac_key = PBKDF2(enc_key, salt^0x3a, 2, 32)，
     HMAC over page[16:4032]+le32(1) == page[4032:4096]），通过才写入 `keys.json`(0600)。
   - 保留**兜底**：rounds==2 直接抓 per-DB enc_key；`CCCryptorCreate` 抓 AES key。两种模型都试。
   - 同时在 corecrypto 层断 `ccpbkdf2_hmac`（输入 x2），覆盖 CommonCrypto 内部路径。
2. **merge-式重签解 exit 255**（`mac_resign.py`）
   - 新增 `merged_entitlements_plist`：**导出副本(=原版)原有 entitlements，并入
     get-task-allow 后再签**，尽量保留微信原签名声明（旧做法只写一个 get-task-allow 的
     最小 plist，会抹掉原权限，更易触发 4.1.x 反篡改自检 exit 255）。
   - 只处理副本，**绝不碰 /Applications/WeChat.app**；仍不加 `--options runtime`（硬化运行时挡调试）。
3. **并行方案（已评估，待真机验证）**：DYLD `__DATA,__interpose` 钩子
   `native/macos/key_hook/wechat_key_hook.c` —— 钩子**开机即在位**，比 LLDB 事后 attach
   更稳，能稳抓登录最早那次派生（疑似我们「副本活满却零命中」的根因）。只记录候选到
   0600 文件（`WECHAT_KEY_HOOK_OUT`），再离线用 `derive_passphrase_keys` 派生。
   构建命令见该文件头注释。**尚未接入主流程，需真机验证注入与命中后再决定是否转正。**

### 你早上要做的（只一步，顶多扫一次码）
```bash
cd ~/Noteford/mac-pipeline-wt
source ~/Noteford/源码/.venv/bin/activate
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
# 终端需有「完全磁盘访问」，否则读不到容器内 .db
python -m noteford.mac_entry capture --acknowledge-debug-copy --timeout 300
```
临时微信窗口出现后**扫码登录即可，不用点聊天**。成功标志：`keys.json` 里库数覆盖
session+contact（理论上一次 passphrase 命中就会写入全部可派生库），`error` 为 null，
`csrutil status` 仍 enabled，原版签名仍是腾讯 Developer ID。

### 仍需真机验证的点
- `CCKeyDerivationPBKDF` arm64 入参约定（x1=password, x2=passwordLen）在 4.1.15 成立；
  `ccpbkdf2_hmac` 的 corecrypto 寄存器约定**可能随 macOS 构建变化**，代码已按
  x1=passwordLen/x2=password 处理，若真机命中为 0 需复核寄存器映射。
- merge-式重签能否真正把 exit 255 概率降下来（导出 entitlements 的 `--xml` 格式在旧系统可能不同，
  导不出会自动退回最小 get-task-allow）。
- interpose 注入在 merge 重签副本上的实际命中率。
