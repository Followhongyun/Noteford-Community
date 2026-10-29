"""Stable bridge control metadata. Never opens pairing or changes connection state."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from mcp.types import Tool, ToolAnnotations

BRIDGE_VERSION = '2'
REFRESH_TOOL = Tool(
    name='refresh_mcp',
    description=('Refresh Noteford MCP catalog from the current application and request client tool-list reload. '
                 'Read-only and idempotent; does not enable a paused connection, pair, change authorization or sync. '
                 'A successful result does NOT prove this client reloaded tools; rediscover tools and verify '
                 'list_libraries plus get_status on this same connection.'),
    input_schema={'type': 'object', 'properties': {}, 'additionalProperties': False},
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, destructive_hint=False, open_world_hint=False),
)


CALL_TOOL = Tool(
    name='call_current_tool',
    description=('Call a current Noteford business tool using its latest schema from refresh_mcp. '
                 'Compatibility path when the host still caches old tool names. Existing authorization and '
                 'validation apply. May request synchronization or pairing: only with explicit user intent. '
                 'Never automatically retry a failed write or sync; control-tool recursion is forbidden.'),
    input_schema={'type': 'object', 'properties': {'name': {'type': 'string'},
                  'arguments': {'type': 'object'}}, 'required': ['name', 'arguments'], 'additionalProperties': False},
    annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False, destructive_hint=True, open_world_hint=True),
)
CONTROL_NAMES = {REFRESH_TOOL.name, CALL_TOOL.name}


def catalog(tools):
    # The bridge owns the reserved name even if a future worker exports it.
    current = [tool for tool in tools if tool.name not in CONTROL_NAMES] + [REFRESH_TOOL, CALL_TOOL]
    return sorted(current, key=lambda tool: tool.name)


def catalog_version(tools):
    body = json.dumps([tool.model_dump(mode='json', by_alias=True, exclude_none=True) for tool in tools],
                      ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(body).hexdigest()


def instructions_version(instructions):
    return hashlib.sha256(instructions.encode('utf-8')).hexdigest()


def runtime_version(paths):
    """Explicit refresh only: stream file hashes; no hashing in the idle watcher."""
    digest = hashlib.sha256()
    for name, _stamp in paths:
        file_digest = hashlib.sha256()
        with Path(name).open('rb') as source:
            while block := source.read(1024 * 1024):
                file_digest.update(block)
        digest.update(file_digest.digest())
    return digest.hexdigest()
