"""Narrow bridge to the running desktop synchronizer. Never changes selection/settings."""
from __future__ import annotations
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request_sync(reader, library_id):
    from mcp.server.mcpserver.exceptions import ToolError
    grant = reader.grant(library_id)
    if grant.get('allow_sync') is not True:
        raise ToolError('sync_not_authorized: 此连接未授权刷新同步，请在应用中重新授权。')
    root = reader._registry().app_root
    try:
        record = json.loads((root/'data/runtime/mcp-service.json').read_text())
        port = record['port']
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError()
        secret = json.loads(Path(record['token_file']).read_text())['token']
        from .mac_ai_access import MacAiReader
        payload = {'library_id': library_id,
                   'mode': 'direct' if isinstance(reader, MacAiReader) else 'grant',
                   'client_id': reader.client_id}
        from .mcp_connection_state import expected_generation
        if expected_generation() is not None:
            payload['connection_generation'] = expected_generation()
        reader.grant(library_id)
        request = Request(f'http://127.0.0.1:{port}/api/v1/mcp/refresh',
                          data=json.dumps(payload).encode(),
                          headers={'Content-Type': 'application/json', 'X-Noteford-Token': secret}, method='POST')
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=5) as response:
            result = json.loads(response.read(16384))
        reader.grant(library_id)
        return result
    except (OSError, ValueError, KeyError, TypeError):
        raise ToolError('sync_service_unavailable: 请打开 Noteford 后重试；请求未确认，不代表同步成功。') from None


class SyncRequests:
    def __init__(self, api, config_path):
        self.api, self.config_path = api, config_path
        self.lock = threading.Lock()
        self.running = set()

    def submit(self, payload):
        from .mcp_server import LocalIndexReader
        from .mac_ai_access import MacAiReader
        library = payload.get('library_id')
        if not isinstance(library, str) or not library:
            raise ValueError('library_required')
        if payload.get('mode') == 'direct':
            reader = MacAiReader(self.config_path, library)
        elif payload.get('mode') == 'grant' and isinstance(payload.get('client_id'), str):
            reader = LocalIndexReader(self.config_path, payload['client_id'])
        else:
            raise ValueError('invalid_sync_authorization')
        from .mcp_connection_state import require_connected
        generation = payload.get('connection_generation')
        if generation is not None:
            if not isinstance(generation, str) or not generation:
                raise ValueError('invalid_connection_generation')
            require_connected(self.config_path, generation)
        grant = reader.grant(library)
        if grant.get('allow_sync') is not True:
            raise PermissionError('sync_not_authorized')
        # Resolve only the authorized library, never whichever account is currently selected.
        core = self.api._core(library)
        with self.lock:
            if library in self.running or core.refresh_lock.locked():
                return {'accepted': False, 'state': 'busy', 'library_id': library}
            self.running.add(library)
        def run():
            try:
                if generation is not None:
                    require_connected(self.config_path, generation)
                if reader.grant(library).get('allow_sync') is not True:
                    return
                core.refresh(trigger='ai_request')
            except Exception:
                # The normal synchronizer persists failure receipts; never log private paths.
                pass
            finally:
                with self.lock:
                    self.running.discard(library)
        try:
            threading.Thread(target=run, name='noteford-ai-sync', daemon=True).start()
        except Exception:
            with self.lock:
                self.running.discard(library)
            raise
        return {'accepted': True, 'state': 'requested', 'library_id': library,
                'requested_at': datetime.now(timezone.utc).isoformat(),
                'note': '请求已提交，不代表同步完成；使用 get_status 查看回执。关注和同步范围不变。'}
