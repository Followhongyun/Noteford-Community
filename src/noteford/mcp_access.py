"""Owner-approved local MCP scopes. No writes are performed by MCP clients."""
from __future__ import annotations
import json
from pathlib import Path


def read_grant(root: Path, client_id: str | None) -> dict:
    from .product_workspace import require_active
    require_active(root)
    try:
        payload = json.loads((root / 'data' / 'mcp_access.json').read_text(encoding='utf-8-sig'))
        grant = payload['clients'][client_id]
        if (payload['version'] != 1 or grant['enabled'] is not True
                or not isinstance(grant['library_id'], str)
                or not isinstance(grant['conversations'], list)
                or grant.get('scope', 'selected') not in ('selected', 'all_synced', 'all_indexed')
                or (grant.get('scope', 'selected') == 'selected' and not 1 <= len(grant['conversations']) <= 500)
                or any(not isinstance(item, str) or not item for item in grant['conversations'])):
            raise ValueError('invalid_grant')
        return grant
    except (OSError, ValueError, KeyError, TypeError):
        raise PermissionError('mcp_access_denied: 请在 Noteford 设置中创建或重新授权 AI 连接。') from None
