# 连接与验收

## 收到一行安装命令后

用户把 `npx skills add Followhongyun/noteford-skill` 发给 AI 并要求接入时，AI 负责安装及后续连接验收：

1. 确认当前客户端能执行这台电脑的命令；纯云端对话不能借此访问本机。检查现有 Skill 和 Node/npm，已有安装可直接使用。需要安装时使用安装器支持的当前客户端标识，只安装 `noteford`；用户未指定项目范围时使用用户级安装，避免依赖当前工作目录。不使用 `--all`，不修改其他客户端。
2. 读取安装器返回目录中的 `SKILL.md` 和本文件，在当前会话继续。`skills add` 只负责安装文件，不会自动执行 Skill 或证明 MCP 已连接。
3. 已有可用 MCP 时复用当前连接；macOS Codex 已获授权但缺少新版控制工具时，使用下文保留身份的持久入口。其他首次接入使用 Skill 脚本的 `connect`、`tools` 和 `call`，自动发现正在运行的 Noteford；不要求用户寻找端口、复制令牌或填写 MCP JSON。未运行、未登录或资料库未就绪时报告具体缺项；需要用户确认授权时等待确认，不代替用户批准。
4. 实际调用 `list_libraries` 和授权资料库的 `get_status`，确认 `read_only=true` 后才报告连接可用。只停留在安装完成或 `paired: true` 不算接入完成。

首次接入通常经 Skill 脚本调用 MCP，无须注册原生 MCP；已有授权优先沿用原调用入口。只有用户或当前客户端确实需要原生工具时，才配置当前客户端的 Noteford 项，并按该客户端支持的方式刷新、再次验收；保留其他配置，不自行重启客户端。Node、Python 或本机执行能力缺失时说明限制，不能宣称一行命令已完成全部配置。

## macOS Codex 保留既有授权的持久入口

当 Codex 已获得 Noteford 授权，但当前原生工具目录缺少 `refresh_mcp` / `call_current_tool`，优先由当前 Codex 的命令执行工具直接启动安装版服务，保留可继续写入 stdin 的会话（例如持久 TTY/session）：

```sh
exec /Applications/Noteford.app/Contents/Resources/service/noteford-service --mcp-auto --client-name Codex
```

不要在外面套 Node 客户端、Python `subprocess` 或 `scripts/noteford.py`；它们会成为不同的实际调用程序，不能凭名称同为 Codex 就复用原授权。也不要跳过任意解释器、更改凭据或要求用户重新批准来掩盖身份不匹配。`exec` 替换命令 shell，使服务能识别现有 Codex 调用方。只有实际调用方已获授权时才可复用，不保证未授权客户端自动接通。

在**同一个**持久 stdio 会话中，逐行发送 JSON-RPC，并按顺序等待响应：

1. 发送 `{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"Codex","version":"1"}}}`，等 id=1 成功返回。
2. 发送 `{"jsonrpc":"2.0","method":"notifications/initialized"}`。
3. 发送 `{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"refresh_mcp","arguments":{}}}`，读取最新 tools、instructions 和版本摘要；过滤通知，不把通知当成请求响应。
4. 发送 `{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"call_current_tool","arguments":{"name":"list_libraries","arguments":{}}}}`。
5. 使用上一步真实返回的资料库 ID，通过同一入口调用 `get_status`；确认 `read_only=true`、账号和范围符合原授权。后续调用继续使用这个会话并给每个请求不同 id。

此路径是当前会话主动使用的正式 MCP 兼容通道，不是修改或重新注册宿主原生工具目录。只读刷新不应调用 `request_pairing`、切换连接开关或同步消息。若仍返回 `pairing_required`，停止并报告实际调用身份/授权边界，不自动配对或轮询弹窗。应用更新后在保留的通道再调用 refresh_mcp；工具调用不自动重放。

`--action call --tool refresh_mcp` 是一次性业务 worker 路径，不包含桥控制工具，不能代替上述流程。下面的脚本方案适用于其他入口，已有 Codex 授权时不要优先切过去。

## 本机自动接入

适用于本机、能执行命令且已安装此 Skill 的 AI 客户端。Noteford 启动完成后公布不含凭据的发现信息；Skill 脚本验证运行中的应用与服务位置。Windows 发现文件在 `%LOCALAPPDATA%\Noteford\connection.json`，macOS 在 `~/Library/Application Support/Noteford/connection.json`。开发运行和安装包都会写这份文件。云端 AI 无法因此自动访问本机，Skill 也不会自动安装到其他 AI 产品。

已有 MCP 工具可用时直接调用。工具缺失或 Transport closed 时，改用本 Skill 目录中的脚本（将 `<skill>` 替换为已读取 Skill 所在绝对目录，ClientName 使用真实产品名并保持稳定）：

```powershell
& '<skill>/scripts/noteford.ps1' -Action connect -ClientName Codex
& '<skill>/scripts/noteford.ps1' -Action tools -ClientName Codex
& '<skill>/scripts/noteford.ps1' -Action call -ClientName Codex -Tool list_libraries -ArgumentsFile '<private-work>/arguments.json'
```

`ArgumentsFile` 是 UTF-8 JSON 对象；无参数时写入 `{}`。`tools` 返回标准 MCP 工具参数定义，按实际 schema 调用，不猜字段。`call` 输出 MCP 结果，检查 `isError` 和 `structuredContent`，不能仅凭进程退出码判定查询成功。`read_image` 可加 `-ImageDirectory '<private-work>/images'`，将已授权返回的像素保存为独立图片，并返回 `localPath`；用图片查看工具读取，不把 base64 倾倒到对话。

每次执行建立新的 MCP 会话，授权凭据由应用加密管理，不返回给 AI。应用更新或重启后重新执行即可；应用未运行时请用户打开它。授权等待或拒绝时停止，不轮询弹窗。已连接的原生 MCP 与 Skill 入口可能是不同连接，首次切换会单独请求授权。

macOS 或没有 PowerShell 时，把上面的命令换成：

```sh
'<skill>/scripts/noteford.sh' --action connect --client-name Codex
'<skill>/scripts/noteford.sh' --action tools --client-name Codex
'<skill>/scripts/noteford.sh' --action call --client-name Codex --tool list_libraries --arguments-file '<private-work>/arguments.json'
```

`noteford.py` 与 shell 入口参数相同。macOS 图片导出使用 `--image-directory '<private-work>/images'`；读取返回的 localPath，不把 base64 输出到对话。当前脚本导出支持 PNG/JPEG/WebP；其他格式报错时不要声称已查看图片。

需要注册原生 stdio MCP 的客户端，Windows 可将命令设为 `powershell.exe`，参数为 `-NoProfile -File <skill>/scripts/noteford.ps1 -Action serve -ClientName <真实产品名>`。macOS 可将命令设为 `python3`，参数为 `<skill>/scripts/noteford.py --action serve --client-name <真实产品名>`。这属于客户端可选配置，通常只用 Skill 即可查询，无须配置这一步。

## 本机直接读取与首次配对

Mac 已开启「本机 AI 直接读取」时，脚本优先使用该模式，`request_pairing` 返回现有 `access_mode` 和 `scope`，不会逐客户端弹窗。不要要求已授权用户重复批准。新授权 scope=all_indexed 并允许请求刷新同步；旧 following/all_indexed 授权和同步权限不自动变化，以实际返回及 get_status.capabilities 为准。未开启该模式时才走以下单独配对流程：

1. 如果工具返回 `pairing_required`，调用 `request_pairing`，`client_name` 填当前 AI 产品名。请用户在 Noteford 自动弹出的原生连接请求中确认这是刚刚发起的连接，并选择允许或拒绝。无需验证码，不代替用户点击允许。
2. 等待用户明确说明已确认后再继续，不连续刷新请求。用户拒绝后 5 分钟内同一连接不再弹窗；请求和确认均有 5 分钟有效期。Skill 管理首次连接凭据，已配对连接不会因首次设置期限结束而失效。仅支持当前 Noteford 原生确认流程；若应用仍要求验证码，请先更新应用。
3. 用户确认后再次调用 `request_pairing`。`paired: true` 只证明配对完成，不证明服务或资料库可读。

## 可读状态验收

配对完成后调用 `list_libraries`。选择返回的授权资料库 ID，再调用 `get_status`。只有两次实际调用成功且 `get_status.read_only` 为 `true`，才可告诉用户连接已经可用。

如果资料库为空、未初始化、授权被撤销或 `get_status` 失败，准确报告当前状态并停止成功声明。`source_online` 为 `null` 表示来源在线状态未知；`latest_indexed_at`、`latest_message_timestamp` 和 `archive_last_success_at` 都不能单独证明微信当前在线或刚完成采集。

连接可用后用 `get_history_overview` 核对可读覆盖，并按 next_offset 读完需要的概览。Mac 旧 following 仅含已关注会话；all_indexed 含该账号全部已索引会话。单独授权也支持 all_indexed；旧 all_synced 包含 following/paused，selected 还受显式清单限制。不能根据模式名称扩大范围；以服务返回为准。覆盖数量、时间和来源库数只说明本地索引，source_total_known=false 时不能声称微信全部历史完整。完整读取与游标恢复遵循 [完整历史与增量](history.md)。

`get_status.sync` 和最近回执是本地保存状态。`partial` 不能当作 success；`scheduler_running/next_at/stage/failed_conversations` 只能说明当前应用调度与记录，不能证明微信在线、电脑睡眠期间执行过同步或所有旧记录均已取得。

get_status.read_only 描述资料读取；request_sync 另外通过 capabilities.request_sync 明确授权，工具本身标记为非只读。新的 AI 会话仍应先完成上述状态验收。不要通过读取本地配置、授权文件或数据目录代替 MCP 工具调用。


## 稳定刷新与兼容调用入口

新版 `--mcp-auto` 桥始终列出 `refresh_mcp` 和 `call_current_tool`；应用暂不可用时仍保留这两个控制工具，并在 tools/list 元数据中明确 catalog_available=false。`refresh_mcp({})` 返回当前应用的完整 schema 与版本摘要，发标准变化通知；不代表宿主接收或替换了模型工具缓存。bridge_version 是常驻桥协议实现版本，runtime_version 是按顺序对服务文件各自 SHA-256 再聚合的摘要，catalog_version 是排序、规范序列化的完整工具定义 SHA-256；instructions 来自本次新 worker 的说明，instructions_version 是其 UTF-8 SHA-256；握手时的常驻桥说明可能仍旧，以刷新返回值核对业务用法。不是产品发布号，也不是签名验证结果。明确刷新才读取服务文件计算哈希，空闲检测只 stat。

若宿主不重新暴露工具，使用它原先已加载的 `call_current_tool({"name":"list_libraries","arguments":{}})`，再将返回的 library_id 传给同入口的 get_status。新业务工具先读 refresh_mcp.tools 的实际 schema。兼容入口不会扩大服务端权限，SDK 仍验证参数；禁止把控制工具作为 name，禁止自动重放同步。明确报告是当前连接的兼容调用成功还是原生工具路由已更新。

这些控制工具属于稳定桥；直接 `--mcp --pair`、远程 MCP 或一次性 Skill 脚本不因此自动得到相同入口。应用更新保留现有授权；已运行旧桥需要客户端支持的单服务装载操作一次，单靠应用按钮或一个新的独立 stdio 测试不能证明旧会话已迁移。
