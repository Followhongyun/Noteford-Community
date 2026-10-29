# 模块地图与代码入口

适用对象：要定位改动位置的开发者。这里说明各模块**负责什么、入口在哪里**；用户行为定义见 [PRODUCT](PRODUCT.md)，跨模块规则见 [CONTRACTS](CONTRACTS.md)。

| 责任 | 主要入口 | 改动时要核对 |
| --- | --- | --- |
| 桌面启动、托盘、更新、IPC 与媒体协议 | `desktop/electron/main.cjs`、`desktop/electron/app-updates.cjs`、`desktop/electron/private-update-provider.cjs`、`desktop/electron/platform-seam.cjs`、`desktop/electron/permission-guide.cjs`、`desktop/electron/mac-permissions.cjs`、`desktop/electron/preload.cjs` | 页面权限、本地 Token、数据根目录、更新后的服务进程。Mac 更新后台下载并经原生验证后暂存，正常退出安装，不主动重启；受保护下载处理器在 `distribution/private-updates.cjs`，上线边界见 OPERATIONS。Windows 与 macOS 的图标、服务命令、发现目录和权限引导只在接缝里分支 |
| 页面、导航、账号选择、设置 | `desktop/src/App.tsx`、`desktop/src/components/PermissionGuide.tsx`、`desktop/src/components/SettingsView.tsx`、`desktop/src/types.ts`、`desktop/src/components/` | 首次添加、切换资料库、Mac 权限引导、错误提示与筛选状态。点自动化「授权」会弹出控制系统事件和微信的对话框；系统不再询问时打开自动化设置并写明要开的开关。点完全磁盘访问「授权」打开对应设置页，并在访达里显示 Noteford.app，需要点「+」加入后再重启。完全磁盘访问不以能列出容器根目录为准 |
| 统一会话筛选 | `desktop/src/components/UnifiedChatSearch.tsx`、`ChatTimeFilter.tsx`、`ChatDatePicker.tsx`、`ChatView.tsx` | 右对齐条件标签、类型/群成员多选、时段筛选与时间定位、非未来月历及结果定位返回 |
| 会话分类结果 | `desktop/src/components/ChatResults.tsx`、`ChatView.tsx` | 按天倒序的媒体正方形网格、文件/链接列表、原消息定位与返回；下载策略沿用消息组件 |
| 账号发现与独立资料库 | `discovery.py`、`libraries.py`、`metadata.py`、`assets.py` | 多账号身份、目录规范化、昵称头像与隔离 |
| 微信读取、快照、密钥与解密 | `winmem.py`、`platform_seam.py`、`snapshot.py`、`refresh.py`、`crypto.py`、`image_crypto.py` | 微信源目录只读、版本兼容、密钥和 WAL 一致性。Windows 内存扫描惰性绑定 `kernel32`；macOS 密钥走 `keys.json` / interpose，不在导入时加载 `winmem` |
| 原始消息累计与选择性索引 | `archive.py`、`indexer.py`、`selective.py`、`processing.py` | 未关注归档、关注后补建、消息身份与去重 |
| 消息模型、查询与媒体 | `decode.py`、`repository.py`、`media.py`、`media_backfill.py`、`voice_decode.py` | 日期边界、分页、发送者、不可读媒体和缓存 |
| Mac 自动媒体与转发附件 | `mac_completion.py`、`mac_cdn.py`、`mac_attachments.py`、`mac_emoticon.py`、`mac_image_decode.py`、`mac_image_build.py` | 登录后队列、只读账号绑定的 CDN 凭据、HTTPS 下载与完整性校验、结构化转发附件、随包无网络图片解码器；Windows 不使用这些模块 |
| Mac 账号与首次引导 | `mac_accounts.py`、`mac_auto.py`、`decrypted_archive.py`、`App.tsx`、`ContactDirectory.tsx` | 单账号连接、目录优先、全量后台索引、按账号记忆关注引导；旧资料库隔离保留，Windows 不启用 |
| 本地服务、周期刷新、设置与内部 API | `service.py`、`sync_state.py`、`runtime_lock.py` | HTTP Token、调度生存期、失败状态及资料库选择 |
| AI 接入 | `mcp_server.py` | 只读授权、账号 ID、稳定分页；远程见 `remote_mcp.py`，工具契约见 CONTRACTS |
| 已选会话历史与上下文 | `history_read.py`、`history_coverage.py`、`repository.py` | following/paused 与授权交集、快照游标、历史覆盖缺口 |
| 桌面后台服务恢复 | `desktop/electron/service-recovery.cjs` | 单一子进程、健康检查、退出与唤醒重试 |
| Apple 圆角与 Mac 原生绘制 | `desktop/src/apple-corners/`、`desktop/electron/mac-corners.cjs`、`desktop/electron/mac-corner-renderer.cjs` | 保留 CSS 几何，Mac 公共 CALayer 连续轮廓优先；特殊几何/其他平台使用 G3。缓存、状态/尺寸监听与受限数字 IPC；无屏幕读取。约束见 DEVELOPMENT |
| 源码 CLI 与旧每日任务路径 | `cli.py`、`batch.py`、`jobs.py`、`Scripts/run-daily.ps1` | 不要把旧任务安装脚本当成当前桌面日报入口 |
| 缓存、快照与离线备份 | `maintenance.py`、`backup.py` | 保留策略、受保护路径、执行前预览 |
| 构建与公开安装包 | `Scripts/build-windows.ps1`、`Scripts/publish-github-release.ps1`、`desktop/package.json`、`desktop/scripts/mac-pack.cjs` | 仅正式版请求触发；版本、签名、数据保留与元数据校验。Mac 的 beforePack 把服务冻成 arm64 自包含程序，失败则中断，不再退回系统 `python3`。afterPack 从里向外用固定 Developer ID 签署动态库、framework、辅助程序和外层 app，并用 `codesign --verify --deep --strict` 把关。Mac 资源不含 `noteford-book-v1.ico`、`noteford.ps1` 和 `winmem.py`。共用 `files` 排除全部 `.ico` 和 `.ps1`，Windows 的 `files` 再加回 `app.asar`。图标和 `noteford.ps1` 仍打进 Windows 安装包，路径不变 |
| macOS 重签取密钥与只读同步（Windows 运行路径不改） | `mac_key_storage.py`、`scripts/mac-probe/`、`scripts/mac-capture/`、`native/macos/wechat_hook/`、`mac_resign.py`、`mac_capture.py`、`mac_auto.py`、`mac_sync.py`、`mac_entry.py`、`platform_seam.py`、`desktop/electron/mac-wechat.cjs`、`desktop/electron/wechat-connect.cjs` | 副本取完删除并注销嵌套 App；扫码卡是权限向导最后一步。Windows 服务在 `/api/v1/mac/wechat` 与 `/api/v1/mac/auto-capture` 都返回未改路径 |

Python 包位于 `src/noteford/`；表中省略该共同前缀。对应临时数据测试位于 `tests/test_*.py`，GitHub 检查在 `.github/workflows/checks.yml`。按改动定位最窄的验证入口，流程见 [DEVELOPMENT](DEVELOPMENT.md)。


官方 MCP 新入口：`mcp_access.py`（默认拒绝和授权读取）、`mcp_changes.py`（消息事件与游标）、`desktop/electron/mcp-connections.cjs`（桌面授权与插件导出）、`desktop/electron/local-consent.cjs`（原生确认队列与失败回收）、`desktop/electron/mcp-prompt.cjs`（Noteford 配对与验收提示词）、`desktop/electron/shared/client-setup.cjs`（vendored 客户端配置渲染）、`desktop/src/components/McpConnections.tsx`（设置 UI）、`desktop/mcp-plugin/`（官方插件模板及可选 `noteford` Skill）。公共工具参数以 [CONTRACTS](CONTRACTS.md) 为准。

本机热更新启动：`Scripts/start-live.ps1` 校验现有数据根；`desktop/scripts/local-live.cjs` 管理 Vite、代码观察、语法检查和自动重开；`desktop/electron/main.cjs` 的 `--local-live` 负责复用显式数据根与原应用身份。此模式不改变安装版更新机制。


- Mac 完整卸载：`desktop/native/uninstall.swift` 为独立 AppKit 确认工具；`desktop/scripts/mac-uninstaller.cjs` 构建独立运行时并交给固定正式签名流程；`src/noteford/mac_uninstall.py` 执行固定范围清理；隔离删除测试在 `tests/test_mac_uninstall.py`。


`mac_key_storage.py` 统一 Mac 微信密钥的钥匙串迁移、资料库缓存作用域及捕获临时文件清理；`keychain_secret.py` 提供不经命令行传输密钥的原生可变长条目读写。Windows 现有文件流程保持兼容。
