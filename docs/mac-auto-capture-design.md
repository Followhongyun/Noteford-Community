# Noteford macOS 全自动取钥流程（Task B 设计）

> 历史原型/阶段验收，已被后续实现取代；下文“当前”“明早”“待接入”均指原记录阶段，不是日常操作指令。现行流程只操作临时微信副本，不改原版安装；Mac 密钥仅持久保存在钥匙串，`keys.json` 只是旧迁移位置，不得恢复明文运行方案。当前边界见 [数据与安全](DATA-AND-SECURITY.md)、[运行指南](OPERATIONS.md)，安装验收见 [STATUS](STATUS.md)。

目标：用户装好 Noteford 后**只扫一次码**,副本的 复制 → 重签 → interpose 注入 →
启动 → 抓 passphrase → 派生校验写 `keys.json` → 清理副本 → 重开正版,全部后台
自动、用户无感,**正版微信全程不碰签名**。

## 单一入口

`noteford.mac_auto.run_auto_capture(config, *, resign_actions, interpose_actions,
store, **deps) -> dict`（薄封装），核心是 `AutoCapture.run() -> AutoCaptureReport`。

App（或 `mac_entry serve` 的 HTTP 路由）只调这一个入口。所有协作者都可注入,
因此无需真机 GUI / 真扫码即可单测。

## 阶段（也是重试的恢复点）

idle → preflight → (already_satisfied?) →
  是：sync → done
  否：prepare_copy → build_hook → await_scan → derive_verify → sync → done
失败任意阶段 → failed；**finally 永远 restore（删除副本 + 重开正版）**。

## 四条硬保证（由被编排的各层强制）

1. **不碰正版签名**：正版只用菜单 quit + 按精确路径重启（`restore_original` /
   `launch_exact`),从不 codesign `/Applications/WeChat.app`。
2. **绝不 nohup**：副本由 `launch_with_hook` 的 `subprocess.Popen` 直接启动。任何
   会跨 exec 剥 `DYLD_*` 的 wrapper 都会让钩子静默失效（X1c 已验证）。GUI 进程由
   钩子的 execve/posix_spawn 重注入续上。
3. **总是自清**：无论成功 / 超时 / 异常,`finally` 里 `restore_original` 一定执行,
   副本在注销嵌套 `.app` 之后整包删除,正版被重开。历史 `.disabled` 残留同样删除。
4. **幂等**：`already_satisfied()` 读 `keys.json`,对当前库逐一 page-1 HMAC 校验;
   若所有可见库都已有当前有效 key,则**直接返回、不扫码、不建副本**。

## 幂等判定

`verify_keys_against_libs(keys, databases)`：对每个库用 `crypto.verify_key`
（PBKDF2-SHA512 2 轮算 mac_key,HMAC-SHA512 校验首页）判定 key 是否对当前库有效。
库被重建 / 换 salt 后旧 key 失效 → 返回需要重新扫码。

## 失败与重试

- **登录窗没弹 / 扫码后没抓到 PBKDF**（`no_passphrase_captured` / `coverage_incomplete`）：
  `failed` + `needs_scan=True`,**不自动重试**，只提示再扫一次。
- **基础设施失败**（`copy_failed` / `codesign_failed` / `codesign_verify_failed` /
  `entitlements_export_failed` / `timeout`）：`run_with_retries` 最多再试 2 次，
  指数退避，重试用 `force=True` 先删旧副本。
- **异常**：捕获后只回传 `error` 码（如 `hook_build_failed`/`codesign_failed`),
  **绝不回传或记录任何密钥 / passphrase**。
- 重跑即重试：`keys.json` 已有效则跳过整个扫码。副本若还不是 `LSUIElement` agent，会先删掉重做。
- 超时：`scan_timeout_seconds`（默认 600s）到点后 capture 结束,finally 删除副本。

## 安全

- `AutoCaptureReport.public()` 只含计数与阶段名,**不含任何密钥**。
- `keys.json` 由取钥层以 0600 写入 state_dir,**不进仓库、不进日志**。
- 钩子输出走沙盒容器内路径（`sandbox_candidates_path`,Task A 修复）。

## 待接线（后续）

- `mac_entry` 增加 `auto` 子命令 / `serve` 增加路由,调用 `run_auto_capture`。
- 真机联调一次全自动闭环（本阶段不做,已有 `keys.json`,不再找洪云扫码）。
- 可选：取钥成功后触发后台增量 `sync` 的定时任务。
