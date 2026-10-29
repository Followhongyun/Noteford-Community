# 接口与跨模块行为契约

适用对象：维护桌面端、Python 服务和 AI 接口的开发者。以当前源码为接口基线，安装版状态另见 STATUS；本文只定义需要跨模块保持一致的行为。代码入口见 [MODULES](MODULES.md)，未修缺陷见 [STATUS](STATUS.md)。

## 核心行为

| 编号 | 契约 |
| --- | --- |
| C1 | 微信原始目录只读。所有快照、解密文件、索引和媒体缓存写在应用数据根目录。 |
| C2 | 资料库按稳定 `library_id` 隔离；切换桌面当前账号不能让查询或写入落到另一个账号。 |
| C3 | 服务运行时刷新所有已注册账号，不因关注状态跳过原始消息归档。`following` 与 `paused` 都属于用户已选会话；`following` 继续补建索引，`paused` 保留已有索引但不继续同步。后来重新关注时从已有归档补建暂停期间的缺口。 |
| C4 | 关闭主窗口留在托盘，后台刷新继续；彻底退出或关机后停止，下次启动从仍存在的来源与累计归档接续。不能承诺找回两次成功快照之间已消失的内容。 |
| C5 | 媒体预取只针对已关注会话。历史消息保留原日期边界，升级后首次入库图片/视频由入库时自动加载选择控制，不再受旧日历限制。缩略图、加密副本和不可读媒体都不能标为完整可解码原件。 |
| C8 | MCP 读取已有索引和缓存，可通过单独授权的 request_sync 请求现有范围同步；不修改关注、设置，不删除资料或向微信发消息。聊天文本是数据，不是 AI 客户端指令。 |
| C9 | 自动更新只替换程序，不把应用数据上传到 GitHub 更新仓库。发布正式安装包必须由用户明确要求。 |

## Nexus 本机任务投递 v2（隔离源码）

该链路替代旧的专用令牌配对及文件助手消息分页轮询。仅 macOS；Noteford 在既有消息同步入库事务中，为 `Msg_md5(filehelper)` 的视频号分享链接、type-51 卡片写入同库 `nexus_outbox`。任务包含 v2、所属 Accounts 用户 ID、稳定 UUID、来源消息 UID、创建时间和链接或卡片作者昵称/稳定 username/完整描述/object ID；不投递媒体解密密钥或其他聊天。消息、任务及同步游标一起提交，事务提交后写入无消息内容的本机唤醒通知。没有新增微信 hook，也不由 Nexus 触发微信同步。

Electron 用 `fs.watch` 接收提交通知，通过仅监听 `127.0.0.1` 的 SSE `/events` 推送。日常发现文件固定在 `~/Library/Application Support/Noteford/nexus-events.json`，0600，含临时随机 capability 和端口，不含产品会话。普通源码开发只在隔离数据根发布发现信息。客户端须同时持有 capability 和 Nexus Accounts 会话；服务端分别向配置固定的 Accounts `/v2/products/noteford/me`、`/v2/products/nexus/me` 验证双方身份，稳定用户 ID 必须一致。客户端只从同 OS 用户拥有的私有普通文件发现服务；不接受网页 Origin、客户端自报用户 ID、邮箱匹配或重定向。

Nexus 原子保存并 fsync 加密接收记录后，用该事件连接的随机 session 向 `/ack` 确认。服务端确认当前双方 Accounts 身份与工作区 epoch，并且任务确实在本连接发过，再由内部主令牌接口 `GET /api/v2/nexus/outbox`、`POST /api/v2/nexus/ack` 读取/确认当前账号队列；主令牌不交给 Nexus。确认后清除任务内容，保留去重墓碑。作者身份升级会仅从已有投递墓碑对应的文件传输助手索引消息恢复 username，以原 task_id 回投一次；不扫描其他聊天。新包保留字段为 object_id、username、nickname、desc，媒体 token/decode key 不进入交接。确认不代表下载成功。Nexus 独立负责解析、下载、重试与结果展示。

启动、登录、重连及唤醒补发未确认任务；平时由提交事件推动。SSE 每 30 秒重验身份并发送心跳，不读取消息或队列；Nexus 断线按 0.5–30 秒退避重连。退出/切换工作区同步关闭连接与发现文件，旧 epoch 无法继续投递或确认。服务端撤销在下一次发送、确认或最多 30 秒的身份检查时发现，验证不可用时停止跨应用操作。内容和凭据不进入普通日志。

## Mac 账号绑定

Mac 账号控制接口为 `GET /api/v1/mac/accounts`（仅返回已添加账号的登录状态、跟随设置与建议切换目标）、`POST /api/v1/mac/accounts`（布尔 `follow_wechat`）、`POST /api/v1/mac/accounts/connect`（不传 `library_id` 时添加新账号，传已注册 `library_id` 时重新连接该账号）、`POST /api/v1/mac/accounts/cancel`（取消当前登录）。连接立即返回后台任务状态；任务确认临时副本实际登录账号并完成首次资料读取后，才注册账号并返回 `library_id`，重复账号带 `already_added=true`。均沿用本机服务令牌；Windows 不启用这些接口。

Mac preload 为会话、消息、搜索、群成员、关注和刷新请求绑定发起时的 `library_id`，主进程转为 `X-Noteford-Library`。服务使用请求上下文读取指定资料库，未知 ID 拒绝而不回退。切换期间拒绝新数据操作，切换前的迟到结果丢弃，界面先清空旧状态。资料库列表和账号发现不绑定当前账号。既有外部 AI 授权继续绑定原资料库，不因桌面切换而扩大读取范围。

## 桌面 IPC

React 页面通过 `desktop/electron/preload.cjs` 暴露的 `window.wechatLocal` 和 `window.groupDigest` 调用主进程；主进程在 `desktop/electron/main.cjs` 中校验参数并请求 Python 服务。页面不能直接取得 HTTP Token 或 SQLite 路径。修改跨进程字段时，要同时核对 `service.py`、`main.cjs`、`preload.cjs`、`desktop/src/types.ts` 和实际 UI 调用。

## 本机 HTTP API

这是桌面主进程与 Python 服务之间的内部协议，路由定义以 `src/noteford/service.py` 为准；不承诺第三方稳定兼容。服务仅监听 `127.0.0.1`。桌面版由主进程分配动态端口并保管 Token；源码命令 `serve-api` 可指定端口。`GET /api/v1/health` 无需认证，其余路由均要求请求头 `X-Noteford-Token`。不要把 Token 写入仓库或日志。多数成功响应为 `{ "ok": true, "data": ... }`，健康检查为 `{ "ok": true }`；失败时返回 `ok: false`、`error`，部分错误另带 `message`。

下表列出当前路由及主要用途；查询参数放在 URL，POST 使用 JSON 对象。具体字段、校验和返回结构以处理函数为准。

| 用途 | GET（读取） | POST（可能改变设置或数据） |
| --- | --- | --- |
| 健康与状态 | `/api/v1/health`、`/api/v1/status` | — |
| 资料库 | `/api/v1/libraries` | `/api/v1/libraries/candidates`、`/api/v1/libraries/register`、`/api/v1/libraries/discover`、`/api/v1/libraries/switch`、`/api/v1/libraries/rename` |
| 会话与消息 | `/api/v1/conversations`、`/api/v1/messages`、`/api/v1/conversations/members`、`/api/v1/contacts`、`/api/v1/search`、`/api/v1/selections` | `/api/v1/conversations/follow`、`/api/v1/conversations/pause` |
| 设置、媒体与刷新 | `/api/v1/settings`、`/api/v1/conversation-media-range` | `/api/v1/conversation-media-range`、`/api/v1/refresh` |

常用读取参数：`messages` 必须指定 `conversation`，可传 `before`、`from_day`、`to_day`、`limit`、`category`、`q`、`sender`、`filter_offset`；`conversations/members` 必须指定 `conversation`；`search` 使用 `q`；其中 `messages` 的 `before`、`from_day`、`to_day` 都是 **Unix 秒时间戳**，不是 `YYYY-MM-DD`；日期区间为 `from_day <= 消息时间 < to_day`。查本地某一天时，传当天本地 00:00 和次日本地 00:00 各自换算的时间戳。MCP 的 `start_day`、`end_day` 则直接使用 `YYYY-MM-DD`，且包含结束日。

主要写入请求体和效果如下；未列出的可选字段及枚举值以处理函数为准。切换资料库会影响后续 HTTP 请求所用的当前账号，因此连续请求前应确认 `/api/v1/libraries`。

| POST 路由 | 主要 JSON 字段 | 效果 |
| --- | --- | --- |
| `/api/v1/libraries/candidates`、`/api/v1/libraries/discover` | 可选 `wechat_root` | 扫描候选；`discover` 可能更新资料库清单 |
| `/api/v1/libraries/register`、`/api/v1/libraries/switch`、`/api/v1/libraries/rename` | `library_id`；重命名另需 `display_name`；切换可选 `initialize` | 注册、切换或改名；初始化可能触发读取 |
| `/api/v1/conversations/follow`、`/api/v1/conversations/pause` | `username` | 改变当前资料库的关注状态；关注可能补建索引 |
| `/api/v1/conversation-media-range` | `library_id`、`conversation`、`start`（YYYY-MM-DD）、`end`（YYYY-MM-DD 或 null 表示至今） | 校验账号和会话后，保存独立范围并仅补齐此会话。GET 使用前两项查询参数 |
| `/api/v1/media/recheck` | POST：`library_id`、`uid` | 需要认证与明确账号；从该账号索引取得消息，拒绝不存在、未关注/暂停或非图片视频消息。只重读该条本地媒体并刷新缓存，响应为单条 messages 的媒体结果。客户端不得提交路径、MD5 或任意网络地址；不访问微信云端，不改原文件和解析日期范围 |
| `/api/v1/refresh` | `{}` | 立即刷新当前资料库；失败时可能返回冲突状态 |

源码调试示例：先用 `python -m noteford.cli serve-api --config <配置文件> --port 17890` 启动独立服务；它会在标准输出显示实际端口和 Token **文件路径**，不会输出 Token。另开 PowerShell 窗口，用数据根目录下实际的 Token 文件读取凭据，然后查询状态：

```powershell
$apiToken = (Get-Content -LiteralPath '<数据根目录>\secrets\api_token.json' -Raw | ConvertFrom-Json).token
$headers = @{ 'X-Noteford-Token' = $apiToken }
Invoke-RestMethod -Uri 'http://127.0.0.1:17890/api/v1/status' -Headers $headers
```

读取某个本地日历日的示例（将日期和会话 ID 换成实际值）：

```powershell
$day = (Get-Date '2026-09-27').Date
$from = ([DateTimeOffset]$day).ToUnixTimeSeconds()
$to = ([DateTimeOffset]$day.AddDays(1)).ToUnixTimeSeconds()
$conversation = [uri]::EscapeDataString('<会话 ID>')
Invoke-RestMethod -Uri "http://127.0.0.1:17890/api/v1/messages?conversation=$conversation&from_day=$from&to_day=$to" -Headers $headers
```

写入请求体示例：`POST /api/v1/conversation-media-range` 使用 `{ "library_id": "<账号 ID>", "conversation": "<会话 ID>", "start": "2026-09-27", "end": null }`，`POST /api/v1/conversations/follow` 使用 `{ "username": "<会话 ID>" }`。示例只说明字段格式，实际调用会修改设置或索引。不要在终端输出、截图或日志中展示 `$apiToken`。HTTP API 可以执行关注、刷新等写操作；给外部 AI 提供只读数据时使用下述 MCP。

## 源码 CLI

CLI 是源码开发与维护入口，当前命令定义在 `src/noteford/cli.py`，不作为安装版用户的稳定接口。进入源码环境后运行 `python -m noteford.cli --help` 查看总命令，运行 `python -m noteford.cli <命令> --help` 查看准确参数。需要配置的命令默认读取当前目录的 `config.local.json`，可用 `--config` 指定；多数单次命令把 JSON 结果写到标准输出。开发版可按 [DEVELOPMENT](DEVELOPMENT.md) 在源码根目录放受 `.gitignore` 排除的本机配置；正式安装版的配置、密钥和结果留在实际应用数据根目录。两者都不得提交仓库。

| 类别 | 命令 | 用途与副作用 |
| --- | --- | --- |
| 状态与查询 | `diagnose`、`index-status`、`app-status`、`libraries`、`selections`、`conversations`、`messages`、`search` | 查看环境、索引或已收录数据；查询范围由当前配置和已建索引决定。 |
| 采集与索引 | `scan-keys`、`scan-image-key`、`snapshot`、`probe`、`index`、`refresh` | 扫描来源、创建快照/解密副本、建立或更新索引；可能访问微信进程和本地私密数据。 |
| 选择与资料库 | `follow`、`pause`、`library-switch`、`library-discover`、`library-rename` | 改变关注状态或资料库注册信息；`follow` 可补建索引。 |
| 旧批处理入口 | `daily-batch`、`daily-job`、`daily-all` | 旧批处理流程，群日报功能已下线，不再使用。 |
| 服务与清理 | `serve-api`、`cleanup` | `serve-api` 启动本机 HTTP 服务和后台调度；`cleanup` 默认仅预览保留策略，传 `--apply` 才实际清理缓存和快照。 |

CLI 不接受 MCP 的 `library_id` 作为统一查询参数；许多命令使用配置中的当前账号。多资料库操作前先核对目标配置及当前资料库，尤其是会写入或清理的命令。这里维护命令职责与风险，详细参数以 CLI `--help` 为准，不手工复制第二套完整参数表，也不为此创建生成文件目录。

## 官方 MCP（资料只读，可单独授权刷新同步）

由同一应用服务的 `--mcp --config <绝对配置路径> --client <连接 ID>` 提供 stdio MCP。以设置页生成的配置为准，不再接受无授权连接。每次工具调用重新核对 `data/mcp_access.json`；每个连接绑定一个资料库。新全量连接使用 `scope=all_indexed`，包含该库全部已索引会话；旧 `all_synced` 仍只包含 following/paused，不含 unselected 旧缓存。旧 `selected` 连接继续保存明确会话清单，但实际可读范围始终是该清单与当前 `following/paused` 的交集。连接不切换桌面当前账号，读取工具不使用桌面 HTTP Token；request_sync 仅由内部桥接使用凭据且不向 AI 返回；连接管理仅由桌面受限 IPC 执行，不暴露为 MCP 工具。

| 工具 | 主要参数 | 行为 |
| --- | --- | --- |
| `list_libraries` | 无 | 仅返回连接授权的账号 |
| `list_conversations` | `library_id`；可选 `query/state/groups_only/limit/offset` | 仅授权会话，按 `next_offset` 翻页；limit 1–100 |
| `read_messages` | `library_id/conversation_id`；可选 `start_day/end_day/sender_id/query/category/limit/offset` | 日期为包含边界的本地 YYYY-MM-DD；按 `next_offset` 翻页；limit 1–100 |
| `get_history_overview` | `library_id`；可选 `conversation_id/limit/offset` | 列出当前授权会话的索引计数、最早/最新消息时间、刷新错误标记和来源库数；limit 1–100 |
| `read_history` | `library_id`；可选 `cursor/conversation_id/query/start_timestamp/end_timestamp/limit/scan_budget` | 在固定初始快照中按稳定入库顺序完整遍历；`end_timestamp` 不包含边界；limit 1–100，scan_budget 1–20,000 |
| `search_messages` | `library_id/query`；可选 `limit/cursor/scan_budget` | 复用历史遍历游标搜索全部授权索引，`items` 保持旧结构；limit 1–100 |
| `get_status` | `library_id` | 协议版本、账号最近成功归档时间、授权范围的最新入库/消息时间、增量索引状态及安全化 `sync` 状态/最近回执；`source_online=null`，不证明源在线或已成功完成最新采集 |
| `get_message` | `library_id/message_uid` | 回查原消息；未授权与不存在均返回 found=false |
| `get_message_context` | `library_id/conversation_id/message_uid`；可选 `direction=around/before/after`、`limit` | 按稳定消息键读取锚点周围、之前或之后的上下文；limit 1–100，使用 continuation 中对应 UID 继续 |
| `read_changes` | `library_id`；可选 `cursor/start/limit` | 按入库变化序号读取增量；limit 1–100，默认 50 |

`get_history_overview` 的 `indexed_count`、最早/最新时间和 `source_database_count` 只描述当前索引。单会话的来源库数去重；totals 中该字段为各会话来源库数的合计，可能包含同一来源库的重复计数。零消息的已选会话也会出现；`has_refresh_error` 仅表示存在安全化错误状态，不返回底层错误文本或来源路径。原始来源总行数没有持久化，因此 `source_total_known=false`，固定 coverage 为“已选会话的已索引历史；本机/微信缺失的旧记录未知”。这些字段不能证明微信曾经存在的所有消息均已取得。实现入口见 [`history_coverage.py`](../src/noteford/history_coverage.py)。

`read_history` 首次调用固定索引快照上界，随后必须原样传回 `cursor`，即使本页零命中，只要 `has_more=true` 仍要继续。每页最多返回 100 条，顺序是稳定的 SQLite 入库 `rowid` 顺序，不代表消息发送顺序。查询、时间和会话过滤属于游标身份；`end_timestamp` 为排除边界。首次调用之后新入库的消息不属于该快照；需要“完整历史 + 后续增量”时，必须先取得下述临时增量起点，再遍历历史，最后从临时起点追平变化。快照内已有消息被编辑或删除、journal epoch 变化、授权或已选范围变化时返回 `cursor_reset_required`，客户端必须按完整衔接流程重做。实现入口见 [`history_read.py`](../src/noteford/history_read.py)。

`search_messages` 使用同一固定快照与游标，不再以最近 20,000 条为完整性上限。关键词继续兼容匹配 `text/title/sender/chat`，不缩减为只搜索正文和标题；`items` 保持既有字段形状。`cursor/has_more/scanned/total_messages/matched_count_this_page` 表示本次扫描进度，零命中页也可能仍有后续。旧 `results_limited/truncated_scan` 字段继续返回以兼容客户端，但完整性以遍历到 `has_more=false` 为准。

`get_message_context` 的锚点必须属于指定的授权已选会话。`around` 返回锚点附近，`before` 向更早消息继续，`after` 向更新消息继续；下一页把 `continuation.before_uid` 或 `continuation.after_uid` 作为新的 `message_uid` 并保持相同 direction。该接口按发送时间相关的稳定消息键读取上下文，与 `read_history` 的入库遍历顺序是两种不同用途。

`read_changes` 首次调用 `start=beginning`（默认）从已有索引历史开始；`start=now` 可在用户明确选择只监控未来消息时建立当前检查点。需要完整历史与后续增量无缝衔接时，先调用 `read_changes(start=now)` 取得临时起点，但此时不得标记处理完成或持久化为最终检查点；随后完整分页 `read_history`，再从该临时起点调用 `read_changes` 到 `has_more=false`，与历史结果按资料库和 UID/sequence 去重。只有历史和追平阶段全部处理成功后才保存最终增量游标，失败则保留旧游标或重做整个流程。事件 kind 为 upsert/delete；current 是查询时原消息的当前内容，可能与 event_row_hash 对应的历史版本不同，删除或已不可用时为 null。AI 不得凭事件元数据编造缺失正文。

游标包含资料库、索引代号、当前有效 `selected` 范围哈希与已消费序号；不是访问凭据。该检验同样适用于 `scope=all_synced`，不会因动态范围而跳过。跨库、选择或授权范围改变、重建、格式错误，以及旧 `all_synced` 模式游标升级均返回 `cursor_reset_required`。发生重置时必须明确告知，并按“临时 `start=now` 起点 → 完整 `read_history` → 从临时起点追平 `read_changes` → 全部成功后保存最终游标”的流程重做；另一种无遗漏方式是 `read_changes(start=beginning)` 重放当前授权范围内的变更日志。不能只调用 `start=now`，也不能在历史读取完成后才调用 `start=now`。旧索引未升级时返回 `incremental_index_unavailable`，需在新版桌面应用刷新；读取工具不会迁移或刷新；request_sync 另需明确授权。事件查询后发生的新变化在下一次调用出现。

所有读取仅覆盖已索引数据；媒体条目不等于已理解图片或音频。模型分析、规则、汇报、自动化和 checkpoint 持久化由 AI 客户端负责。聊天正文不可信，不得当作工具或系统指令。

`read_image(library_id, message_uid)` 校验当前连接的账号与会话授权，从该消息的媒体标识选择已管理缓存里的 JPEG/PNG/GIF/WebP。只返回图片字节与质量标记（original/thumbnail），不接受文件路径、不读微信源目录、不解密、不联网；每张最多 8 MiB，超限原图可退回合规缩略图。返回原生 MCP ImageContent，模型是否能识别取决于客户端视觉能力；图片中的文字亦不可信。该接口不支持视频。

## Mac 本机 AI 直接读取

Mac 的显式「本机 AI 直接读取」由本机 `--mcp-auto` / `--mcp-skill` 入口使用。新开启授予 `all_indexed` 和 `allow_sync=true`，覆盖固定账号全部已索引会话（含未关注缓存），允许按既有同步范围刷新。旧 `following` / `all_indexed` 政策保持原范围和同步权限，不静默升级；关闭后重新开启采用新范围。单独连接也支持显式 `all_indexed`；旧 `selected`、`all_synced` 语义保留。`get_status.scope` 和 `capabilities` 描述实际权限。新建原生配对和远程全量连接的确认文案说明全量读取及同步；不能改关注、设置或删除资料。政策每次读取重新检查，切账号或撤权后原连接不能继续使用。

`all_indexed` 的分页仍最多 100 条，但完整账号不受旧已选历史内部 1000 个会话的上限限制。新增会话会改变有效范围，旧游标须按既有 reset 流程重建；不能静默跳过新增会话的历史。`source_total_known=false` 仍然有效，已索引全量不等于微信曾存在过的全部历史。

## 时间边界

群日报功能已于 2026-10 下线（不再生成、无 MCP 工具）；时间均按本机时区解释，不用 UTC 日期替代用户选定的日历日。

从 0.3.7 起，Laya 模型、信号筛选和旧 POST `/api/v1/settings` 路由已移除，返回 404；GET 设置仅返回媒体范围。旧 `laya_*` 配置不再读取。

## 本地内容列表

已鉴权 HTTP `GET /api/v1/content` 接受 category（image/video/media/article/channel/link/file，media 为图片与视频并集，空值为全部）、query、offset、limit（1–100）。返回 items、total、counts、next_offset；每项有稳定 id、category、title、text、原始 url、availability 和 sources。sources 保留 message_uid、conversation_id、conversation_name、sender、timestamp，可用现有 MCP get_message 核验。MCP `list_content` 复用此提取规则，增加 library_id、conversation_id、start_day/end_day（本机自然日、含首尾）参数；所有条目、数量和合并出处在查询前受连接授权与当前 `following/paused` 交集限制。`all_synced` 动态包含已选会话，旧 `selected` 还要满足其显式清单；两者都不读取 unselected 旧缓存。每次从当前索引读取，不改写原消息；分页期间新增消息可能改变偏移。

选入、移除或修改授权导致有效会话集合变化时，需要重建依赖范围哈希的历史或增量游标；不能用旧游标静默跳过范围变化。following 与 paused 互换不改变历史可读集合，因此单纯暂停/恢复不会要求重置。

旧 `/api/v1/media-window` 在已存在资料库时返回 400，提示使用会话范围，避免旧客户端声称修改成功但实际解析规则未变。旧 GET settings 保留兼容字段。日期按本地自然日计算，包含结束日；拒绝反向范围和未来日期。

头像读取：认证 GET `/api/v1/avatars` 使用显式 `library_id` 和 JSON 数组 `usernames`（最多 2000 项，每项最多 256 字符），返回已导出的本地头像路径，由桌面媒体协议转换。该读取不触发网络下载。同步只补查已关注会话及已索引发言者，头像下载限制为联系人元数据里的 `wx.qlogo.cn`、`mmhead.c2c.wechat.com`、`mmhead.hk.wechat.com`，升级 HTTPS、禁止跳转、每请求 5 秒和 2 MiB 上限、最多四个并发。`status.avatar_sync` 仅含数量与完成时间，不含姓名或头像 URL。

媒体缓存协议 `noteford-media` 支持单个 HTTP 字节范围，成功返回 206 与对应 Content-Range；无范围返回 200，不可满足的范围返回 416。WXGF 静态图的本地转换缓存使用 PNG 和源内容 SHA-256 标记；源文件变化后重新转换，失败时保留原缓存与回退信息。转换只在桌面媒体解析路径进行，MCP read_image 仍只读已生成缓存。


### 本机红包状态

桌面消息 `payment.local_state` 可为 `received`、`exhausted`、`expired` 或 null。消息分页额外返回 `payment_states`（当前会话索引 UID → 同一状态），供已加载的历史页增量更新。状态以当前资料库账号的 general.db/redEnvelopeTable 为源，按 session_name + message_server_id 关联，不用昵称、金额或时间近似匹配。冲突关联与未知值保持 null。

读取器只在源 DB/WAL 大小或修改时间变化后制作只读快照，10 秒内复用内存结果；快照总大小限制 128 MiB，采用已有缓存密钥且禁止内存重扫。解密副本只在资料库 runtime 的独立临时目录内存在，读取完成即清理。缺少源文件、密钥、表或快照失败清除该读取器状态，不把失败解释为领完。该能力不发送查询/领取/拆包/支付请求，不保证微信尚未写入本机的状态新鲜度。


图片原文件定位：消息自身文件不可用时，桌面解析器按该账号索引中消息 XML 的直接 `img.md5` 查找同账号 `hardlink.db/image_hardlink_info_v4`，以 `dir2id` 还原实际文件位置。只接受规范目录和非缩略图文件名，解密后的内容 MD5 必须匹配，再以当前消息媒体标识缓存；不按图片相似度猜配，不跨账号。资源 DB/WAL 变化后使用已有缓存密钥制作只读临时快照，最多 128 MiB、10 万条映射，10 秒内复用；逐条重新检测绕过时间缓存检查源变化。失败丢弃旧映射并保留原有媒体回退。普通阅读、重新检测与后台媒体补查共用此规则；MCP 仍只读取已授权消息对应的已有缓存。


### 显式获取完整媒体

`POST /api/v1/media/recheck` 保持原请求 `{library_id, uid}` 及响应结构。认证和指定资料库/关注会话检查通过后，先读取已校验独立缓存及本地文件，缺失完整内容时尝试独立 CDN 下载。客户端不能提供远程 URL、媒体密钥、账号凭据或保存路径。只读消息接口与 MCP 不触发此下载。

成功的 `media.status` 为 `ready`。失败保留现有缩略图/缺失状态，额外返回 `media.download_error` 固定枚举：`session_unavailable`、`account_mismatch`、`download_busy`、`network_unavailable`、`download_timeout`、`cdn_rejected`、`missing_download_reference`、`integrity_failed`、`invalid_response`、`too_large`、`insufficient_space`、`storage_unavailable`、`unsupported_download_format`。错误不携带底层响应或凭据；`cdn_rejected` 不等同于“文件确定过期”。成功缓存可在微信退出后继续读取。


### 范围内自动补全

桌面本地服务启动串行自动媒体工作线程，自动获取已关注（following）会话、各自媒体日期范围内已索引的图片和视频。paused/未关注/历史范围外/其他类型不发起自动下载；结束日期为至今时按本地日期延续。范围调整会唤醒工作线程，新同步消息由后续轮询纳入。现有只读消息/MCP 调用不会启动该线程。

`GET/POST /api/v1/conversation-media-range` 的原字段保持不变，额外返回 `automatic: {running, completed, waiting}`：当前会话是否在处理、当前范围内已记录就绪及等待重试任务数，不是全部待处理量或完成百分比。任务状态保存在所属资料库 `data/automatic_media.sqlite`，不含媒体密钥、会话凭据或聊天正文。中途范围变化/暂停触发内部 `download_cancelled`，不把半成品标为 ready。

### 隐藏独立表情

受现有本机鉴权保护的 `GET /api/v1/settings` 返回布尔字段 `parse_stickers`，缺省为 `true`。界面“隐藏表情”取此值的反向：开启隐藏写入 `false`，关闭隐藏写入 `true`，保留已存偏好的实际显示行为。`POST /api/v1/settings/parse-stickers` 接受 `{ "enabled": false }`，只接受 JSON 布尔值，成功返回 settings；无效值返回 400 `invalid_sticker_parsing`，写入失败返回 500 `settings_save_failed`。桌面 IPC 继续绑定当前产品账号工作区及其 epoch，MCP 不提供此设置写入操作。

选择原子写入当前产品账号的 `config.local.json`；落盘成功后才更新既有资料库配置并唤醒后台媒体线程，新建资料库继承该值。不同产品账号不共用此偏好。`false` 时，Mac 自动媒体队列跳过独立表情，表情重查不读取原始 XML 或缓存像素，下载入口和进行中的后续分块/写入检查返回 `download_cancelled`；不删除索引、任务或已有缓存。重新开启继续沿用原有任务及退避规则，`auto_media_loading=false` 仍停止自动队列。界面对 `emoji`/`gif` 类型统一显示 `[表情]`（含引用），普通图片/视频和文字中的表情不受影响。该开关不是数据删除或 AI 访问权限控制。

### 微信同步间隔

受现有本机鉴权保护的 `GET /api/v1/settings` 返回 `sync_interval_seconds` 和兼容字段 `sync_interval_minutes`（10 秒档为 1/6 分钟）。`POST /api/v1/settings/sync-interval` 接受 `{ "minutes": 15 }`，要求整数、在 5–1440 分钟内且为 5 的倍数；也接受 `{ "seconds": 10 }`，秒数仅允许 10 或 300–86400 内的 300 的整数倍；无效输入返回 400，原配置保持不变。成功返回 settings，并原子保存到现有 `auto_refresh_interval_seconds` 字段。默认保持 5 分钟，已有配置和当前选择在升级时保持不变。显式保存的 10 秒原样读取；其他旧配置将有效间隔限制在 5–1440 分钟内，不为此改写原文件；调度和返回设置共用该有效间隔。原有单一周期任务运行中重新读取间隔，所有已添加账号共用此频率；只读 MCP 不提供该写入操作。间隔按每轮开始时间计算，最短可选 10 秒，其他档位继续每 5 分钟递增；一个写入轮次未结束时不会并发启动下一轮。

## 同步与分页补充（2026-10-11，源码）

- `POST /api/v1/settings/sync-interval` 接收整数 `minutes`，在 5–1440 分钟内且为 5 的整数倍；原子保存后现有唯一周期线程在下一次检查时读取（至多约 1 秒）。周期按 start-to-start 计算；长轮次占过下一个开始点时不并发、不连续追跑。电脑睡眠恢复后只补一次当前轮次。15 秒 UI 轮询仅查询本地状态，不代表同步微信。
- 阶段 deadline 只在 catalog、头像、媒体和清理的边界检查，不能中断正在执行的阻塞文件或数据库 I/O。到期后已完成的归档/索引可记成功，后续可选维护阶段延后；这不把未完成的 selected refresh 记作成功。`paused` 会话保持暂停，不因恢复、登录返回或重试自动改为 following。
- 同步只有一个写入者。`data/state/sync.json` 保存当前阶段及最近 20 次安全化回执；`partial` 与 `success` 分开，单个已选会话失败时保留上次完整成功时间并记录失败会话数量，不写账号、路径、聊天正文或底层异常文本。MCP `get_status` 和 HTTP 状态只读该保存状态；状态可证明应用保存了什么结果，不能证明微信在线。状态页的 `next_at` 来自主进程内当前调度状态，间隔调整、重试和恢复后实时变化。实现见 [`sync_state.py`](../src/noteford/sync_state.py) 和 [`service.py`](../src/noteford/service.py)。
- Electron 主进程独立于 renderer 监控本地服务。连续三次健康检查失败后终止旧服务，并等待其实际退出回调后才启动替代进程，禁止两个数据库写入者重叠；应用仍在托盘时继续运行。彻底退出 Noteford 或电脑处于睡眠期间不承诺执行同步。实现见 [`service-recovery.cjs`](../desktop/electron/service-recovery.cjs)。
- 普通消息分页新增 `before_uid` 与返回值 `next_before_uid`，在当前账号、会话内按 `(create_time, sort_seq, server_id, uid)` 继续；不存在或跨会话游标拒绝。旧 `before` 秒级查询兼容，但同秒分页应使用 UID 游标。筛选列表的 `next_offset` 规则不变。
- `/status` 的 `sync` 包含 `state`、`last_success_at`、`started_at`、`finished_at`、固定分类 `error`、`scheduler_running`、`interval_minutes`、`next_at`。最近成功只在整次刷新完成后更新，失败不清除；新进程识别未完成的 running 记录为 interrupted。来源不可用与密钥不可用分别提示，不能据此断言微信在线。
- 启动 HTTP 写入服务前对数据根取得系统文件锁，冲突报 `data_root_in_use`；只读 MCP 不启动采集调度。老版本、旧 CLI 维护任务不认识该锁，切换版本与备份前仍必须停止所有旧写入进程。

## 当前账号的本机登录检测

认证 `/api/v1/status` 新增 `wechat_login`：`state=active|not_running|unknown`、固定 `reason`、UTC `checked_at`、恒为 null 的 `network_online`。active 表示同一微信主进程当前同时持有该资料库精确来源目录下的 session/session.db 和 contact/contact.db；不表示服务器认证或网络在线。无主进程且检查完整时为 not_running；权限受限、版本不支持、只见其他账号、文件枚举不完整时为 unknown，不凭历史缓存推断登录。只读枚举进程文件句柄，不扫描凭据、不请求微信网络；结果在内存缓存 10 秒，不向响应暴露进程号、文件路径或其他账号。

## 引用目标与精确上下文

桌面 messages IPC 与 GET `/api/v1/messages` 支持 `around_uid`（锚点前后上下文，包含锚点）和 `after_uid`（锚点之后，排除锚点）。只允许未筛选查询，不能混用两者或与 `before/before_uid` 混用；错误为 `incompatible_message_cursor`。锚点必须属于当前账号与所选会话，否则 `message_cursor_unavailable`。排序沿用 `(create_time, sort_seq, server_id, uid)`，同秒分页不遗漏。

`message.quote.server_id` 保持字符串；`state` 为 `available/unavailable`，`target` 是同账号、同会话内唯一原生 ID 对应的消息或 null。不允许按文本、姓名或时间回退匹配。嵌套引用只返回快照，不递归展开 target；用户逐层打开时按 UID 拉取并解析。目标媒体按目标自身 UID 的策略读取，媒体地址经过原有桌面协议注册。界面丢弃账号/会话变化及被取消请求的结果。


媒体范围 `automatic` 保留 `waiting` 计数字段以兼容已有客户端，并增加 `failures` 列表：`uid`、`timestamp`、固定中文 `reason`、固定错误 `code`、`attempts`、`paused`。不返回凭据或 CDN 原始响应。该字段仅保留兼容，界面不再提供范围日历及未完成列表；非运行、非就绪记录均计入。


### 移除媒体日历后的加载边界（0.3.29）

`message_media_policy.range_exempt` 为向后兼容的附加整数列，既有记录默认 0，自动/手动选择不改写；新版触发器仅对首次入库消息写 1。自动图片/视频队列要求 following 且 automatic=1，并满足 range_exempt=1 或原日期范围。旧范围接口保留维护兼容，只影响历史记录；新消息不受旧结束日期截断。完整缓存复用和终止失败不重试规则不变。界面删除范围查询轮询，普通日期筛选不变。

## 远程 MCP 契约（feat/remote-mcp）

- 入口：`noteford-service --mcp-remote --config <config.local.json> --public-url https://<host> [--port 8765] [--admin-port 8766]`；环境变量 `NOTEFORD_REMOTE_ADMIN_SECRET`（≥32 字符，桌面端每次随机生成）必填。
- 公网端点（127.0.0.1:port）：`/mcp`（streamable HTTP，Bearer 必需，scope `wechat.read`）、`/.well-known/oauth-authorization-server`、`/.well-known/oauth-protected-resource/mcp`、`/register`、`/authorize`、`/token`、`/revoke`、`/oauth/consent`（验证码页）、`POST /oauth/consent/code {request, code}`、`GET /oauth/consent/status?request=`。
- 管理端点（127.0.0.1:admin-port，请求头 `x-noteford-admin`）：`POST /admin/pair-code` → `{code, expires_at}`；`GET /admin/pending` → `[{display_id, client_name, created_at, expires_at}]`；`POST /admin/approve {display_id, grant_client}`；`POST /admin/deny {display_id}`；`GET /admin/grants` → `[{id, client_name, grant_client, created_at, active}]`；`POST /admin/revoke {id}`；`GET /admin/health`。
- 令牌主体 = `mcp_access.json` 连接 ID；工具集与参数与本机 MCP 相同（资料只读，刷新同步另需 allow_sync）。连接被禁用或令牌过期/撤销时 `/mcp` 返回 401。
- 验证码：8 位，字符集 `ABCDEFGHJKMNPQRSTUVWXYZ23456789`，输入忽略大小写和分隔符；5 分钟、一次性；单请求 5 次错误锁定，累计 10 次错误作废验证码。

## 本地 MCP 配对契约

- 启动：`noteford-service --mcp --config <config> --pair <nfp_令牌>`；不再接受旧 `--client <ID>` 参数。
- 未授权时所有工具返回错误文本 `pairing_required: …请在 Noteford 弹窗中确认本次连接，并选择允许或拒绝`；新增只读工具 `request_pairing(client_name?)` → `{paired: true}` 或 `{paired: false, message}`。
- 桌面端 JSON CLI：`--mcp-local-admin --config <config> issue <连接ID>` → `{token}`；`requests` → 未处理的请求 ID；`review <request_id>` → 请求来源、账号与范围并一次性进入待确认状态；`approve|deny <request_id>`；`bindings`；`revoke <binding_id>`。输出 `{ok, result|error}`。
- 本机默认使用原生确认，无需验证码。弹窗只显示「客户端名称」请求连接 Noteford，提供允许与拒绝，默认拒绝。请求发现与确认各 5 分钟有效；确认期间重复调用不生成新请求，拒绝后同一绑定 5 分钟内不能重复弹窗。未知程序路径拒绝请求；确认后授权前重新核对已绑定范围，范围变化拒绝授权。桌面在弹窗前固定授权账号，不能因切换账号改变授权目标。仅支持当前请求确认流程，已删除旧 `lookup`/`pending` 管理命令及验证码生成、校验、旧客户端 ID 分支。
- 配对存储的数据密钥在 Windows 上仍由 DPAPI 保护。macOS 放进登录钥匙串（服务 `com.followhongyun.noteford`，账户 `noteford-data-key`）。磁盘上的 key 文件在 Mac 上只是标记，不含密钥字节。

消息列表 `at_time` 参数按时间戳查找当前会话首条 `create_time >= at_time` 消息并返回其上下文；同秒按现有 sort_seq/server_id/uid 顺序稳定定位。不得与其他游标、筛选或 offset 混用；没有后续记录时返回空列表。

会话筛选 `category` 支持以逗号分隔的类别组合（新增 image/video，原 media 保留），类别间取并集；`sender` 支持逗号分隔的微信用户名并集。类别、发送人、关键词与 from_day/to_day 时间条件取交集，分页沿用 filter_offset；起点包含、终点不包含。

## Skill 本机发现与调用

应用启动完成后发布发现记录（version=1），包含 product、instance、pid、appExecutable、command、config、skillPath，不含令牌和聊天数据。Windows 路径仍是 `%LOCALAPPDATA%/Noteford/connection.json`，安装版的 command 仍必须是应用旁的 `resources/service/noteford-service.exe`。macOS 路径是 `~/Library/Application Support/Noteford/connection.json`。安装包的 command 是 `Contents/Resources/service/noteford-service` 这份冻结后的自包含程序，不调用系统 `python3`。开发运行仍可以是 `python3` 且 args 为 `-m noteford.service`。开发运行和安装包都会写这份文件；冒烟测试不写。退出仅清理自身实例。`--mcp-skill --client-name NAME --action connect|tools|call [--tool TOOL]` 读取该发现记录，call 从标准输入读取 JSON 对象，以标准 MCP Client 调用既有配对服务器。返回标准 MCP 结果，工具错误看 isError；`--mcp-auto --client-name NAME` 提供同权限的原生 stdio 入口。Windows 客户端脚本是 `noteford.ps1`，macOS 是 `noteford.sh` / `noteford.py`。公开发现不是授权。

## 已移除的群安全接口

群安全不再是应用内置能力。桌面 `monitor`、`monitor-update`、`monitor-import` IPC 与 GET/POST `/api/v1/monitor` 已移除，旧 HTTP 路径返回 404。MCP 不再注册 `list_monitored_groups`、`read_monitor_results`、`get_monitor_result`；调用旧工具按未知工具拒绝，不提供兼容写入或读取旧侧库的后门。

应用不再发布 `monitor-bridge` 范围、接收报告或启动本机模型。未来独立 Skill 使用通用授权查询与增量接口；这不增加任何写权限或自动扩大群范围。旧文件保留规则见 [DATA-AND-SECURITY](DATA-AND-SECURITY.md)。

## 产品工作区契约

账号 IPC 仍负责注册、登录、恢复和退出。数据 IPC 仅在认证后的工作区激活时注册，preload 附加主进程签发的 `workspaceEpoch`，主进程同时验证发送窗口和当前代次。`signed-in` 状态只在资料服务就绪后发布，附带 `workspaceEpoch` 和旧资料工作区标识；产品账号 ID 用于隔离页面偏好。旧 `local` 动作不再授权读取。

受桌面管理的数据根包含 `.product-workspace.json`，由安装级 `data/runtime/product-account/active-workspace.json` 控制当前代次。未激活、桌面已退出、旧代次 HTTP 请求返回 401；MCP 返回访问错误，不返回原数据。Mac 公开捕获请求若包含目录/应用覆盖字段，返回 403。未受桌面管理的独立 CLI 配置继续保持原有行为。无登录会话的 smoke 验证登录页已挂载且资料页不可见，不绕过产品认证。


### AI 附件读取和同步请求

`read_attachment(library_id,message_uid,offset=0,limit=262144,expected_sha256=null)` 只读取消息绑定的现有缓存，不接受路径、不下载来源。limit 最多 1 MiB；返回 base64 data、mime_type、size、sha256、next_offset、quality。续读必须携带首块 sha256，内容变化时报 attachment_changed_restart；缓存路径必须位于该资料库 media-cache 内。`get_message` 对转发记录提供脱敏的 record_items，保留子项 UID，不暴露缓存路径；解析上限沿用 300 项与 5 层。

`request_sync(library_id)` 要求当前授权 `allow_sync=true`，通过私有本机桥接提交给运行中的同步服务，并在服务端再次验证授权和账号。该工具 `read_only_hint=false`，不会修改关注、设置或删除资料。accepted/requested 仅代表提交；busy 不重复排队，最终结果通过 get_status 保存的同步回执检查。服务只刷新指定授权资料库的既有同步范围。旧读取连接默认没有此权限。桥接运行信息只供服务内部使用，不通过 MCP 返回凭据。


### 本机 MCP 断开与重连

设置中的「MCP 连接」仅控制当前产品账号的本机 MCP/CLI 调用；暂停状态与随机连接代次保存在该工作区的 `data/mcp_connection.json`，不修改配对凭据、读取范围或同步设置。状态「允许连接」不等于客户端正在连接。断开阻止新工具调用，重新连接保留授权并开启新代次；已开始的同步仍可能完成，应先读 `get_status`，不自动重放失败或不确定请求。远程 HTTP MCP 的授权/生命周期仍由其独立设置管理。

新版 `--mcp-auto` 保留 stdio 通道，每次工具调用通过已验证的 discovery 启动当前服务的一次性 `--mcp-skill` worker。每条桥最多四个并行 worker，不积压等待队列；单次 45 秒、输入 1 MiB、输出 16 MiB 上限，取消或超时只回收自己启动的 worker。工作区、连接代次及应用实例在返回前再次核对；旧结果不会跨代次返回。代次同时贯穿 worker 前置检查、工具授权和同步服务的提交/开始执行检查，旧排队同步不得在重连后开始。工具和系统说明沿用原服务，资料和凭据不写入桥日志。

同账号应用重启或服务更新后，后续请求可继续使用同一 stdio 通道。桥固定首次使用的产品账号，切到其他账号需在客户端仅断开并重连 Noteford MCP，不能自动转移授权。已经运行的旧 `--mcp-auto` 没有桥能力，首次迁移也必须仅重连这一 MCP 服务一次；不要求退出整个 ChatGPT/Codex，具体管理入口取决于客户端。桥自身的 Python 运行时仍常驻，其代码/运行时安全更新不能宣称在原进程中生效，也需单连接重连。显式 `--mcp --pair` 仍为直接会话；新代码遵守断开控制，但服务代码更新需单连接重连。


### MCP 工具列表刷新

新版 stdio 桥声明 tools.listChanged。MCP 2026-07-28 使用官方 SDK 的 subscriptions/listen，仅向订阅 toolsListChanged 的流发送事件，保留订阅 ID/过滤语义；旧协议使用连接上的 notifications/tools/list_changed。最多 4 个订阅，每流最多 8 个待发送事件，客户端重新订阅后必须自行重新列举，不重放通知或工具调用。

桥每 2 秒只检查 discovery、当前配置/连接代次/授权策略及服务文件的 stat 元数据，合并同一周期的变动，不启动 worker、不解密、不扫描索引；检测服务替换或重连后通知客户端重新 tools/list。工具列表不做应用层缓存，每次列举都由当前服务返回。请求前后同时核对服务文件身份及修改时间，执行期间换包与连接代次变化均丢弃旧结果，已开始的同步可能完成且不能自动重放。暂停时仍可读取工具元数据；切换产品账号不自动接管新账号。

应用未启动、主动断开、账号/工作区变化、调用超时及并发已满返回不同错误代码；服务返回的 pairing_required 保留，不把授权拒绝混为断线，也不自动重新配对。客户端必须实现旧协议通知或新协议订阅并重新列举才能更新其工具缓存；服务端不能强制刷新客户端已冻结的路由，不能使未注册服务凭空出现。已运行的旧桥本体仍需要一次服务级重连才获得本机制。


稳定桥拥有固定 `refresh_mcp({})`（只读、幂等）和 `call_current_tool({name, arguments})` 两个控制工具；即使业务目录暂不可读仍可发现控制入口。refresh 返回当前完整 schema、bridge/runtime/catalog 版本及 client_reload_verified=false，并请求标准通知，不自动恢复暂停或创建配对。运行时版本只在明确刷新时流式计算文件摘要；普通检测仅 stat。业务目录列举使用不可执行业务的元数据 reader，不打开 Keychain、不创建自动配对令牌。

兼容入口每次重新读当前目录，再由最新 worker 执行同一业务调用及原有参数/授权验证。保守标记非只读、非幂等；不允许递归控制工具、不放宽权限、不重放同步。它使已加载新桥的客户端即使仍缓存旧工具名称也可调用最新业务工具；不等于客户端原生路由已刷新。品牌 Skill 的“更新mcp”流程依次刷新、尝试重新发现，再直接或经兼容入口执行 list_libraries/get_status 验收；旧桥或未注册服务不伪造刷新成功，斜杠命令支持取决于宿主。

refresh_mcp 同时返回新 worker tools/list 元数据携带的最新 instructions 及其 SHA-256 instructions_version，避免沿用常驻桥初始化时的旧说明。说明缺失则刷新失败，不声称完整刷新成功；客户端是否采用这些说明仍需实际验收。
