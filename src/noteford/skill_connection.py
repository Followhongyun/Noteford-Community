"""Public discovery + fresh MCP sessions for local Skill clients. No direct data access."""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
from pathlib import Path
import subprocess
import sys
import uuid

import psutil

from .local_pairing import build_paired_server, open_pairing


def discovery_path() -> Path:
    from .platform_seam import mcp_discovery_file

    return mcp_discovery_file()


def discover(path: Path | None = None) -> dict:
    try:
        record = json.loads((path or discovery_path()).read_text(encoding='utf-8-sig'))
        if record.get('version') != 1 or record.get('product') != 'Noteford':
            raise ValueError('discovery_invalid')
        process = psutil.Process(int(record['pid']))
        if not process.is_running() or Path(process.exe()).resolve() != Path(record['appExecutable']).resolve():
            raise ValueError('app_not_running')
        for key in ('command', 'config'):
            if not Path(record[key]).is_absolute() or not Path(record[key]).is_file():
                raise ValueError('discovery_invalid')
        args = record.get('args') or []
        if not isinstance(args, list) or any(not isinstance(item, str) for item in args):
            raise ValueError('discovery_invalid')
        from .platform_seam import service_command_acceptable

        if not service_command_acceptable(Path(record['appExecutable']), Path(record['command']), args=args):
            raise ValueError('discovery_invalid')
        return record
    except (OSError, KeyError, TypeError, json.JSONDecodeError, psutil.Error) as exc:
        raise ValueError('app_not_running') from exc


def _signed_service_ancestor(process, executable: Path) -> bool:
    """Recognize our moved, still-running macOS bridge by dynamic identity.

    A pathname or caller-supplied identity is insufficient after an app swap.
    Validate the running PID, and reject exit/PID reuse during the check.
    """
    if (sys.platform != 'darwin' or not getattr(sys, 'frozen', False)
            or executable.name != 'noteford-service'):
        return False
    requirement = ('=identifier "noteford-service" and anchor apple generic '
                   'and certificate leaf[subject.OU] = "QD3H9VHB23" '
                   'and certificate leaf[field.1.2.840.113635.100.6.1.13] exists')
    try:
        started = process.create_time()
        result = subprocess.run(
            ['/usr/bin/codesign', '--verify', '--strict', '-R', requirement, str(process.pid)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=3, check=False,
        )
        return (result.returncode == 0 and process.is_running()
                and psutil.Process(process.pid).create_time() == started)
    except (OSError, subprocess.TimeoutExpired, psutil.Error):
        return False


def client_program() -> str:
    """Skip only our own helper and ordinary shells; show the first real caller."""
    own = Path(sys.executable).resolve()
    shells = {'powershell.exe', 'pwsh.exe', 'cmd.exe', 'conhost.exe'}
    try:
        for process in psutil.Process().parents():
            executable = Path(process.exe()).resolve()
            if executable == own or executable.name.lower() in shells:
                continue
            if _signed_service_ancestor(process, executable):
                continue
            return str(executable)
    except psutil.Error:
        pass
    return 'unknown'


def connected_server(record: dict, client: str, program: str, *, metadata_only=False):
    if sys.platform == 'darwin':
        from .mac_ai_access import direct_server
        direct = direct_server(Path(record['config']))
        if direct is not None:
            return direct
    if metadata_only:
        # Build identical schemas without opening Keychain, tokens or pairing state.
        return build_paired_server(Path(record['config']), None, '', lambda: program)[0]
    pairing = open_pairing(Path(record['config']))
    token = pairing.auto_token(client, program)
    server, state = build_paired_server(Path(record['config']), pairing, token, lambda: program)
    state['client_name'] = client
    return server


async def call(record: dict, client_name: str, program: str, action: str, tool: str | None, arguments: dict, *, expected_generation: str | None = None) -> dict:
    from mcp import Client
    from .mcp_connection_state import connection_state, request_generation
    before = connection_state(Path(record['config']))
    if expected_generation is not None and before[1] != expected_generation:
        raise ValueError('mcp_connection_changed')
    if not before[0] and action != 'tools':
        raise ValueError('mcp_disconnected')
    with request_generation(before[1]):
        server = (connected_server(record, client_name, program, metadata_only=True)
                  if action == 'tools' else connected_server(record, client_name, program))
        async with Client(server, read_timeout_seconds=30) as client:
            if action == 'tools':
                result = await client.list_tools()
            else:
                result = await client.call_tool('request_pairing' if action == 'connect' else tool, {'client_name': client_name} if action == 'connect' else arguments)
        if connection_state(Path(record['config'])) != before:
            raise ValueError('mcp_connection_changed')
        # Each reader checks this generation again at its authorization boundary.
        payload = result.model_dump(mode='json', by_alias=True, exclude_none=True)
        if action == 'tools':
            from .mcp_instructions import INSTRUCTIONS
            payload.setdefault('_meta', {})['noteford'] = {'instructions': INSTRUCTIONS}
        return payload


def run(argv: list[str] | None = None, *, serve: bool = False) -> int:
    # Frozen Python may ignore PYTHONIOENCODING. Keep Skill pipes deterministic.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('--client-name', required=True)
    parser.add_argument('--discovery', type=Path)
    parser.add_argument('--action', choices=['connect', 'tools', 'call'], default='connect')
    parser.add_argument('--tool')
    parser.add_argument('--image-directory', type=Path)
    parser.add_argument('--expected-config', type=Path)
    parser.add_argument('--expected-generation')
    args = parser.parse_args(argv)
    try:
        if serve:
            from .mcp_bridge import serve as serve_bridge
            asyncio.run(serve_bridge(args.client_name, args.discovery))
            return 0
        record = discover(args.discovery)
        if args.expected_config:
            from .mcp_connection_state import connection_state
            if (Path(record['config']).resolve() != args.expected_config.resolve()
                    or connection_state(args.expected_config)[1] != args.expected_generation):
                raise ValueError('mcp_connection_changed')
        program = client_program()
        if not args.client_name.strip() or len(args.client_name) > 80 or program == 'unknown':
            raise ValueError('client_unknown')
        arguments = json.loads(sys.stdin.read() or '{}') if args.action == 'call' else {}
        if not isinstance(arguments, dict) or (args.action == 'call' and not args.tool):
            raise ValueError('arguments_invalid')
        result = asyncio.run(asyncio.wait_for(call(record, args.client_name, program, args.action, args.tool, arguments, expected_generation=args.expected_generation), timeout=40))
        if args.image_directory:
            for item in result.get('content', []):
                if item.get('type') == 'image':
                    extensions = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp'}
                    suffix = extensions.get(item.get('mimeType'))
                    if not suffix:
                        raise ValueError('image_format_unsupported')
                    args.image_directory.mkdir(parents=True, exist_ok=True)
                    target = args.image_directory.resolve() / (str(uuid.uuid4()) + suffix)
                    target.write_bytes(base64.b64decode(item.pop('data'), validate=True))
                    item['localPath'] = str(target)
        print(json.dumps(result, ensure_ascii=False))
        return 2 if sys.platform == 'darwin' and result.get('isError') else 0
    except Exception as exc:
        known = {'app_not_running', 'discovery_invalid', 'client_unknown', 'arguments_invalid', 'image_format_unsupported',
                 'mcp_disconnected', 'mcp_connection_changed', 'mcp_connection_invalid', 'mcp_account_changed',
                 'product_workspace_locked', 'product_workspace_changed'}
        code = str(exc) if str(exc) in known else 'connection_failed'
        print(json.dumps({'ok': False, 'error': code, 'message': '请打开 Noteford 后重试。若应用已运行，请等待启动完成；不要反复修改 MCP 配置。' if code == 'app_not_running' else '连接未完成，请检查应用状态；不要绕过授权或连续重试。'}, ensure_ascii=False))
        return 2
