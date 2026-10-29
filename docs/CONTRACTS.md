# 接口

主进程接口以 desktop/electron/preload.cjs 为准，Python HTTP 路由以 src/noteford/service.py 为准。除健康检查外，本地接口要求令牌。页面只能通过来源验证后的 IPC 访问。

工作区关闭后旧代次请求拒绝；Mac 请求同时绑定微信资料库，切换后不返回旧资料库响应。MCP 由 mcp_server.py、mcp_access.py、local_pairing.py 和 mac_ai_access.py 实施授权与资料范围校验。

产品账号、官方更新、Nexus 投递接口已经删除。

2026-10-29：状态中的 `sync.last_triggered_at` 是最近一次同步触发时间，`sync.next_at` 为该时间加当前间隔。手动、自动与 AI 同步通过同一启动通知重置后台计划；`last_success_at` 仍保留为成功完成时间，不用于下次同步计时。忙碌未接受的请求不重置计划。
