#!/usr/bin/env python3
"""Register Community MCP only when the user has requested Codex connection."""
import argparse
import json
import shutil
import subprocess
from pathlib import Path
from noteford import discovery_file, load_record

NAME = 'noteford-community'


def configure(cli, record, run=subprocess.run):
    command = str(Path(record['command']).resolve())
    # Use the packaged service directly so Python does not become the MCP caller.
    if Path(command).name not in {'noteford-service', 'noteford-service.exe'} or record.get('args'):
        raise RuntimeError('请先安装并打开 Noteford 开源版安装包；不注册开发运行时。')
    args = ['--mcp-auto', '--client-name', 'Codex']

    def read():
        result = run([cli, 'mcp', 'list', '--json'], capture_output=True, text=True, check=True)
        return {item['name']: item for item in json.loads(result.stdout)}

    def matches(item):
        transport = item.get('transport', {})
        return (item.get('enabled', True) and transport.get('type') == 'stdio'
                and transport.get('command') == command and transport.get('args') == args
                and not transport.get('env') and not transport.get('env_vars')
                and not transport.get('cwd'))

    before = read()
    if NAME in before:
        if not matches(before[NAME]):
            raise RuntimeError('同名 MCP 已存在且配置不同或被停用；请先核对，不自动覆盖或启用。')
        return 'already_configured'
    run([cli, 'mcp', 'add', NAME, '--', command, *args], capture_output=True, text=True, check=True)
    after = read()
    if NAME not in after or not matches(after[NAME]):
        raise RuntimeError('注册后的配置与预期不符，请检查 Codex MCP 设置。')
    if {k: v for k, v in after.items() if k != NAME} != before:
        raise RuntimeError('其他 MCP 配置发生变化，请检查；不自动覆盖恢复。')
    return 'configured'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex', help='Codex CLI 路径；默认从 PATH 查找')
    options = parser.parse_args()
    cli = options.codex or shutil.which('codex')
    if not cli:
        raise SystemExit('未找到 Codex CLI；请使用当前客户端提供的 MCP 添加入口，不修改其他客户端。')
    record = load_record(discovery_file())
    result = configure(cli, record)
    print(json.dumps({'status': result, 'server': NAME, 'data_access_verified': False,
                      'next': '在 Codex MCP 列表确认条目，再调用 list_libraries 与 get_status 验证授权读取。'}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError, ValueError) as exc:
        raise SystemExit(str(exc))
