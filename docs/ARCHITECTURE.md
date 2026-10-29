# 架构

Electron 主进程启动冻结的 Python 本地服务，React 通过受限 preload IPC 访问；主进程校验窗口来源并保管本地 API Token。服务监听本机地址。

独立本地工作区由 local-workspace.cjs 管理，每次启动生成生命周期租约；workspace_access.py 校验目录、进程和代次。它不依赖产品账号或云端认证。微信资料库隔离及 MCP 授权独立保留。
