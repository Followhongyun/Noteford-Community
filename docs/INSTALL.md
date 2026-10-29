# Mac 安装与 AI 接入

## 安装应用

1. 从 [最新 Release](https://github.com/Followhongyun/Noteford-Community/releases/latest) 下载 `Noteford-0.1.1-macOS.dmg`。
2. 打开安装盘，将「Noteford 开源版」拖到 Applications。已有开源版时先正常退出，再替换应用。不要删除资料目录。
3. 从 Applications 启动，按应用提示连接本人微信、确认系统权限并选择关注会话。首次同步完成后可阅读和搜索。

支持 Apple 芯片 Mac（M1 及后续）、macOS 13 及以上；没有 Intel 安装包。应用自带运行时，无需单独安装 Node.js 或 Python。开源版与会员开发版身份、资料和授权独立。

## 让本机 AI 使用 Skill 和 MCP

应用安装完成不会擅自修改所有 AI 客户端。请保持 Noteford 开源版运行，在支持本机命令的 AI 客户端中发送：

> 请安装并连接 Noteford 开源版：`npx skills add Followhongyun/Noteford-Community --skill noteford-community`。继续按照 Skill 完成当前客户端 MCP 注册、首次授权和实际读取验收。

这一步安装 Skill 需要可用的 Node.js/npm，Codex 注册脚本需要 Python 3；应用本身不需要这些外部运行时。若本机缺少命令，让 AI 先说明缺失项并按你的选择配置。仅在终端运行 `skills add` 只安装文件，不会自动执行接入流程。

Codex 接入脚本通过官方 CLI 注册独立的 `noteford-community`，不改其他 MCP。配置后在「设置 → 插件 → MCP」确认该名称；当前对话未加载新工具时，新建一个对话再调用。首次读取会在 Noteford 中请求授权，由你亲自确认；安装 Skill 或配置 MCP 不等于允许读取聊天。

接入是否成功，以原生工具实际调用 `list_libraries` 和 `get_status` 为准。可对 AI 说「检查 Noteford 开源版连接，告诉我已授权资料范围」；只有配置存在、列表可见或命令运行成功还不算读取验收通过。其他客户端需按其官方方式配置，本机 stdio 不适用于纯云端客户端。

安装包同时内置 Skill 和注册脚本，位于应用 `Contents/Resources/skills/noteford-community`；高级配置及故障处理见 [连接文档](../desktop/mcp-plugin/skills/noteford-community/references/connection.md)。已安装外部 Skill 的用户仍须更新自己的 Skill 文件，应用升级不会覆盖客户端的 Skill 目录。

## GitHub 自动更新

当前 `0.1.1`，开源版启动约 15 秒后检查本公开仓库的最新正式 Release，此后约每 6 小时检查一次。设置页「应用更新」也可立即检查。发现更高版本后自动下载，完成完整性及 Mac 签名验证后，显示「退出并安装」。正常退出应用时安装，下次打开使用新版；关闭窗口不一定退出应用。

历史 `0.1.5-community.1` 和 `0.1.5-community.2` 用户均须手动安装本次 0.1.1：这是版本编号整理，保留现有功能和资料，不开启自动降级。应用不会下载 GitHub 草稿或预发布版本，不会自动降级，也不会从会员版渠道获取更新。检查和下载只访问 GitHub，不上传聊天资料。

网络不通时保留现有应用并自动重试。可检查网络后重新点击「检查更新」，或从 Release 手动安装。不要删除数据、清理钥匙串、关闭 Gatekeeper 或移除系统隔离标记来处理更新问题。

未来每个新版仍由维护者构建、签名、公证并发布完整更新资产后才会送达。更新保留当前账号、关注和设置；新功能是否加入开源版以该次 Release 说明为准。

开源版在维护者确认发布第一篇 X 推广前固定为 0.1.1；同号修订不触发自动更新。之后的更高正式版本继续通过 GitHub 自动更新。
