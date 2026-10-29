"""Stable stdio transport; one current-service worker per request, no replay."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from .mcp_connection_state import connection_state

MAX_OUTPUT = 16 * 1024 * 1024
MAX_INPUT = 1024 * 1024


ERRORS = {
    'product_workspace_locked': '当前产品账号已退出或工作区不可用，请恢复原账号后重试。',
    'product_workspace_changed': '产品账号工作区已变化，请仅重连此 MCP 服务，不会自动转移授权。',
    'app_not_running': 'Noteford 尚未启动或正在更新，请打开应用并等待恢复后重试。',
    'discovery_invalid': 'Noteford 服务发现记录无效，请检查应用启动状态。',
    'mcp_disconnected': 'Noteford MCP 已断开，请在应用中重新连接。',
    'mcp_account_changed': '账号已切换，请在 AI 客户端仅断开并重连 Noteford MCP；不会自动转移授权。',
    'mcp_connection_changed': '连接代次或服务已变化，旧结果已丢弃；如请求过同步，请先检查状态。',
    'mcp_connection_invalid': 'Noteford 连接状态无效，已停止读取，请检查应用连接设置。',
    'mcp_busy': 'Noteford MCP 并发已满，本次请求未排队，请稍后重试。',
    'mcp_timeout': 'Noteford 工具调用超时；如请求过同步，请先检查状态。',
    'client_unknown': '无法识别 AI 客户端程序，未绕过授权，请检查客户端连接。',
    'arguments_invalid': '控制工具参数无效，请按 refresh_mcp 或 call_current_tool 的最新 schema 调用。',
    'tool_unavailable': '当前业务工具不存在或不允许递归调用控制工具，请先刷新目录。',
    'mcp_worker_failed': 'Noteford 服务调用失败，请检查应用状态。',
}


def error_message(error):
    code = 'mcp_timeout' if isinstance(error, asyncio.TimeoutError) else str(error)
    if code not in ERRORS:
        code = 'mcp_worker_failed'
    return f'{code}: {ERRORS[code]} 本次操作未自动重放。'


class Bridge:
    def __init__(self, client_name: str, discovery: Path | None = None):
        self.client_name = client_name
        self.discovery = discovery
        self.config: Path | None = None
        self.active = 0
        self.control_lock = asyncio.Lock()
        from .skill_connection import discovery_path
        from .mcp_refresh import Refresh
        self.refresh = Refresh(discovery or discovery_path())

    def snapshot(self, metadata=False):
        from .skill_connection import discover
        record = discover(self.discovery)
        config = Path(record['config']).resolve()
        if self.config is not None and config != self.config:
            raise ValueError('mcp_account_changed')
        self.config = config
        from .libraries import load_root_config
        from .mcp_refresh import stamp
        runtime_paths = [Path(record['command'])]
        runtime_paths += [Path(arg) for arg in record.get('args', []) if Path(arg).is_absolute()]
        # Development source changes; frozen modules live in the service binary.
        if not getattr(sys, 'frozen', False):
            runtime_paths += [Path(__file__).with_name(name) for name in
                              ('mcp_server.py', 'skill_connection.py', 'mac_ai_access.py', 'local_pairing.py', 'mcp_instructions.py')]
        root = Path(load_root_config(config)['project_root'])
        self.refresh.track([config, root/'data/mcp_connection.json',
                            root/'data/mac_ai_access.json', root/'data/mcp_access.json', *runtime_paths])
        enabled, generation = connection_state(config)
        if not enabled and not metadata:
            raise ValueError('mcp_disconnected')
        runtime = tuple((str(path), stamp(path)) for path in runtime_paths)
        return record, (str(config), generation, record.get('instance'), record['pid'], runtime)

    async def request(self, action: str, tool: str | None = None, arguments: dict | None = None, *, expected_snapshot=None):
        if self.active >= 4:
            raise ValueError('mcp_busy')
        payload = json.dumps(arguments or {}, ensure_ascii=False).encode('utf-8')
        if len(payload) > MAX_INPUT:
            raise ValueError('mcp_input_too_large')
        record, snapshot = self.snapshot(metadata=action == 'tools')
        if expected_snapshot is not None and snapshot != expected_snapshot:
            raise ValueError('mcp_connection_changed')
        command = [record['command'], *record.get('args', []), '--mcp-skill',
                   '--client-name', self.client_name, '--action', action,
                   '--expected-config', snapshot[0], '--expected-generation', snapshot[1]]
        if self.discovery:
            command += ['--discovery', str(self.discovery)]
        if tool:
            command += ['--tool', tool]
        self.active += 1
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            async def exchange():
                process.stdin.write(payload)
                await process.stdin.drain()
                process.stdin.close()
                result = bytearray()
                while chunk := await process.stdout.read(65536):
                    result.extend(chunk)
                    if len(result) > MAX_OUTPUT:
                        raise ValueError('mcp_output_too_large')
                await process.wait()
                return json.loads(result)
            result = await asyncio.wait_for(exchange(), timeout=45)
            # No old-account or pre-disconnect response may reach the client.
            if self.snapshot(metadata=action == 'tools')[1] != snapshot:
                raise ValueError('mcp_connection_changed')
            if process.returncode not in (0, 2) or not isinstance(result, dict):
                raise ValueError('mcp_worker_failed')
            if result.get('ok') is False:
                code = result.get('error')
                raise ValueError(code if code in ERRORS else 'mcp_worker_failed')
            return result
        finally:
            self.active -= 1
            if process is not None and process.returncode is None:
                # Only this request's child, never an existing MCP client process.
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), 2)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()


def create_server(bridge: Bridge):
    from .mcp_refresh import RefreshServer
    from mcp.types import CallToolResult, ListToolsResult, TextContent

    from .mcp_control import BRIDGE_VERSION, CONTROL_NAMES, catalog, catalog_version, instructions_version, runtime_version

    async def current_catalog():
        result = ListToolsResult.model_validate(await bridge.request('tools'))
        result.tools = catalog(result.tools)
        return result

    async def list_tools(_context, _params):
        bridge.refresh.context(_context)
        try:
            return await current_catalog()
        except (ValueError, OSError, asyncio.TimeoutError) as exc:
            # A stable control entry remains discoverable while the app is down.
            return ListToolsResult(tools=catalog([]), _meta={'noteford': {
                'catalog_available': False, 'error': error_message(exc)}})

    async def refresh_control(arguments):
        if arguments:
            raise ValueError('arguments_invalid')
        if bridge.control_lock.locked():
            raise ValueError('mcp_busy')
        async with bridge.control_lock:
            _, before = bridge.snapshot(metadata=True)
            result = await current_catalog()
            # Explicit refresh hashes current service bytes for reproducible evidence.
            runtime = await asyncio.wait_for(asyncio.to_thread(runtime_version, before[4]), 10)
            if bridge.snapshot(metadata=True)[1] != before:
                raise ValueError('mcp_connection_changed')
            instructions = (result.meta or {}).get('noteford', {}).get('instructions')
            if not isinstance(instructions, str):
                raise ValueError('mcp_worker_failed')
            enabled, _ = connection_state(bridge.config)
            await bridge.refresh.notify()
            payload = {'status': 'refresh_requested', 'bridge_version': BRIDGE_VERSION,
                       'runtime_version': runtime, 'catalog_version': catalog_version(result.tools),
                       'instructions': instructions, 'instructions_version': instructions_version(instructions),
                       'runtime_version_kind': 'sha256_of_ordered_service_file_sha256_digests',
                       'catalog_refreshed': True, 'tool_count': len(result.tools),
                       'tools': [tool.model_dump(mode='json', by_alias=True, exclude_none=True) for tool in result.tools],
                       'connection_enabled': enabled, 'notification_requested': True,
                       'client_reload_verified': False, 'fallback_tool': 'call_current_tool',
                       'next_steps': ['rediscover_tools', 'list_libraries', 'get_status']}
            return CallToolResult(content=[TextContent(type='text', text=json.dumps(payload))],
                                  structured_content=payload)

    async def call_current(arguments):
        if (not isinstance(arguments, dict) or set(arguments) != {'name', 'arguments'}
                or not isinstance(arguments['name'], str) or not isinstance(arguments['arguments'], dict)):
            raise ValueError('arguments_invalid')
        name = arguments['name']
        if name in CONTROL_NAMES:
            raise ValueError('tool_unavailable')
        _, before = bridge.snapshot()
        tools = await current_catalog()
        if name not in {tool.name for tool in tools.tools}:
            raise ValueError('tool_unavailable')
        if bridge.snapshot()[1] != before:
            raise ValueError('mcp_connection_changed')
        # The current SDK worker validates business arguments and authorization.
        # There is deliberately no replay if this operation fails or changes era.
        return CallToolResult.model_validate(await bridge.request('call', name, arguments['arguments'], expected_snapshot=before))

    async def call_tool(_context, params):
        bridge.refresh.context(_context)
        try:
            if params.name == 'call_current_tool':
                return await call_current(params.arguments)
            if params.name == 'refresh_mcp':
                return await refresh_control(params.arguments)
            return CallToolResult.model_validate(await bridge.request('call', params.name, params.arguments))
        except (ValueError, OSError, asyncio.TimeoutError) as exc:
            return CallToolResult(is_error=True, content=[TextContent(type='text', text=error_message(exc))])

    from .mcp_instructions import INSTRUCTIONS
    return RefreshServer('Noteford', instructions=INSTRUCTIONS, on_list_tools=list_tools,
                         on_call_tool=call_tool, lifespan=bridge.refresh.lifespan,
                         on_subscriptions_listen=bridge.refresh.listen)


async def serve(client_name: str, discovery: Path | None = None):
    from mcp.server.stdio import stdio_server
    server = create_server(Bridge(client_name, discovery))
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
