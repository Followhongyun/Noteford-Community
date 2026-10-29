import asyncio
import json
import sys
from pathlib import Path

import pytest
from noteford import skill_connection
from noteford.mcp_bridge import Bridge
from noteford.mcp_connection_state import connection_state


@pytest.fixture
def bridge_env(tmp_path, monkeypatch):
    config = tmp_path / 'config.local.json'
    config.write_text(json.dumps({'project_root': str(tmp_path)}))
    worker = tmp_path / 'worker.py'
    worker.write_text('''import json, os, sys, time
from pathlib import Path
json.loads(sys.stdin.read())
root = Path(__file__).parent
with (root / 'calls').open('a') as f: f.write(str(os.getpid())+'\\n')
if (root/'delay').exists(): time.sleep(0.3)
print(json.dumps({'tools':[{'name':'get_status','inputSchema':{'type':'object'}}]} if sys.argv[sys.argv.index('--action')+1] == 'tools' else {'content':[{'type':'text','text':(root/'version').read_text()}]}))
''')
    (tmp_path / 'version').write_text('old service')
    record = dict(command=sys.executable, args=[str(worker)], config=str(config), pid=1, instance='one')
    monkeypatch.setattr(skill_connection, 'discover', lambda _: dict(record))
    bridge = Bridge('Test AI')
    def state(enabled, generation):
        (tmp_path / 'data').mkdir(exist_ok=True)
        (tmp_path / 'data/mcp_connection.json').write_text(json.dumps(dict(version=1, enabled=enabled, generation=generation)))
    return bridge, record, state, tmp_path


def test_same_bridge_disconnect_reconnect_new_runtime_no_replay(bridge_env):
    bridge, record, state, root = bridge_env
    async def exercise():
        assert (await bridge.request('call', 'get_status'))['content'][0]['text'] == 'old service'
        state(False, 'pause')
        with pytest.raises(ValueError, match='disconnected'):
            await bridge.request('call', 'get_status')
        assert len((root/'calls').read_text().splitlines()) == 1
        state(True, 'resume')
        (root/'version').write_text('new service')
        record['instance'] = 'new app'
        assert (await bridge.request('call', 'get_status'))['content'][0]['text'] == 'new service'
        pids = (root/'calls').read_text().splitlines()
        assert len(pids) == 2 and pids[0] != pids[1]
    asyncio.run(exercise())


def test_inflight_result_discarded_no_repeat_after_generation_change(bridge_env):
    bridge, record, state, root = bridge_env
    (root/'delay').touch()
    async def exercise():
        task = asyncio.create_task(bridge.request('call', 'request_sync'))
        while not (root/'calls').exists(): await asyncio.sleep(.01)
        state(True, 'reconnect')
        with pytest.raises(ValueError, match='changed'): await task
        assert len((root/'calls').read_text().splitlines()) == 1
        assert bridge.active == 0
    asyncio.run(exercise())


def test_bridge_never_adopts_another_account(bridge_env):
    bridge, record, state, root = bridge_env
    async def exercise():
        await bridge.request('call', 'get_status')
        record['config'] = str(root/'other/config.local.json')
        with pytest.raises(ValueError, match='account_changed'): await bridge.request('call', 'list_libraries')
        assert len((root/'calls').read_text().splitlines()) == 1
    asyncio.run(exercise())


def test_corrupt_gate_fails_closed_and_queue_is_bounded(bridge_env):
    bridge, record, state, root = bridge_env
    state(True, 'one')
    (root/'data/mcp_connection.json').write_text('null')
    with pytest.raises(ValueError): connection_state(Path(record['config']))
    bridge.active = 4
    with pytest.raises(ValueError, match='busy'): asyncio.run(bridge.request('call', 'get_status'))
    assert not (root/'calls').exists()


def test_mcp_protocol_connection_survives_error_and_resume(bridge_env):
    from mcp import Client
    from noteford.mcp_bridge import create_server
    bridge, _, state, root = bridge_env
    async def exercise():
        async with Client(create_server(bridge)) as client:
            assert not (await client.call_tool('get_status', {})).is_error
            state(False, 'stop')
            failed = await client.call_tool('get_status', {})
            assert failed.is_error and '未自动重放' in failed.content[0].text
            state(True, 'start')
            assert not (await client.call_tool('get_status', {})).is_error
    asyncio.run(exercise())


@pytest.mark.skipif(sys.platform != 'darwin', reason='Mac native direct access fixture')
def test_real_stdio_same_transport_with_fresh_production_workers(direct, tmp_path):
    import os
    import psutil
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters
    config, library, index, policy = direct
    discovery = tmp_path/'connection.json'
    record = dict(version=1, product='Noteford', pid=os.getpid(),
                  appExecutable=psutil.Process().exe(), command=sys.executable,
                  args=['-m', 'noteford.service'], config=str(config), instance='first')
    discovery.write_text(json.dumps(record))
    gate = config.parent/'data/mcp_connection.json'
    def state(enabled, generation):
        gate.write_text(json.dumps(dict(version=1, enabled=enabled, generation=generation)))
    async def exercise():
        params = StdioServerParameters(command=sys.executable,
            args=['-m', 'noteford.service', '--mcp-auto', '--client-name', 'Fixture AI', '--discovery', str(discovery)],
            env={**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1]/'src'), 'PYTHONDONTWRITEBYTECODE': '1'})
        def bridge_pids():
            return [p.pid for p in psutil.Process().children(recursive=True)
                    if '--mcp-auto' in p.cmdline() and 'Fixture AI' in p.cmdline()]
        async with Client(params, read_timeout_seconds=30) as client:
            original_pids = bridge_pids()
            assert len(original_pids) == 1
            refreshed = await client.call_tool('refresh_mcp', {})
            assert not refreshed.is_error and refreshed.structured_content['client_reload_verified'] is False
            wrapped = await client.call_tool('call_current_tool', {'name': 'get_status', 'arguments': {'library_id': library}})
            assert not wrapped.is_error
            invalid = await client.call_tool('call_current_tool', {'name': 'get_status', 'arguments': {'library_id': []}})
            assert invalid.is_error
            tools = await client.list_tools()
            assert 'get_status' in [t.name for t in tools.tools]
            result = await client.call_tool('get_status', {'library_id': library})
            assert not result.is_error, result
            state(False, 'paused')
            assert (await client.call_tool('get_status', {'library_id': library})).is_error
            # Listing tool metadata still works while access is paused.
            assert (await client.list_tools()).tools
            state(True, 'resumed')
            record['instance'] = 'replacement-app'
            discovery.write_text(json.dumps(record))
            assert not (await client.call_tool('get_status', {'library_id': library})).is_error
            assert bridge_pids() == original_pids
            # Existing grant is checked in the new worker, never implicitly restored.
            policy.write_text(json.dumps(dict(version=1, enabled=False, library_id=library)))
            # Avoid switching to paired auth in this test (which could use native keychain).
            state(False, 'revoked')
            assert (await client.call_tool('get_status', {'library_id': library})).is_error
        assert bridge_pids() == []
    asyncio.run(exercise())


from test_mac_ai_access import direct
from test_mcp_official import sample


def test_cancel_cleans_only_owned_worker(bridge_env):
    import psutil
    bridge, _, _, root = bridge_env
    (root/'delay').touch()
    async def exercise():
        task = asyncio.create_task(bridge.request('call', 'get_status'))
        while not (root/'calls').exists(): await asyncio.sleep(.01)
        pid = int((root/'calls').read_text().strip())
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert bridge.active == 0
        assert not psutil.pid_exists(pid)
    asyncio.run(exercise())


def test_worker_rejects_generation_changed_between_preflight_and_call(direct, monkeypatch):
    config, library, _, _ = direct
    gate = config.parent/'data/mcp_connection.json'
    gate.write_text(json.dumps(dict(version=1, enabled=True, generation='new')))
    monkeypatch.setattr(skill_connection, 'connected_server', lambda *a: pytest.fail('must not enter tool'))
    with pytest.raises(ValueError, match='changed'):
        asyncio.run(skill_connection.call({'config': str(config)}, 'AI', '/fixture', 'call', 'request_sync',
                                          {'library_id': library}, expected_generation='old'))


def test_generation_rechecked_at_tool_authorization(direct, monkeypatch):
    from noteford import mcp_sync
    config, library, _, _ = direct
    gate = config.parent/'data/mcp_connection.json'
    gate.write_text(json.dumps(dict(version=1, enabled=True, generation='old')))
    original = skill_connection.connected_server
    def create_then_reconnect(*args):
        server = original(*args)
        gate.write_text(json.dumps(dict(version=1, enabled=False, generation='pause')))
        gate.write_text(json.dumps(dict(version=1, enabled=True, generation='new')))
        return server
    monkeypatch.setattr(skill_connection, 'connected_server', create_then_reconnect)
    monkeypatch.setattr(mcp_sync, 'build_opener', lambda *a: pytest.fail('old generation must not send sync'))
    with pytest.raises(ValueError, match='changed'):
        asyncio.run(skill_connection.call({'config': str(config)}, 'AI', '/fixture', 'call', 'request_sync',
                                          {'library_id': library}, expected_generation='old'))


def test_sync_service_rejects_old_generation_before_submit_and_queued_work(sample, monkeypatch):
    import threading
    from types import SimpleNamespace
    from noteford import mcp_sync
    from test_mcp_expanded import full_grant
    config, library, _, policy = sample
    full_grant(policy, library, allow_sync=True)
    gate = config.parent/'data/mcp_connection.json'
    def generation(value):
        gate.write_text(json.dumps(dict(version=1, enabled=True, generation=value)))
    generation('current')
    calls, pending = [], []
    core = SimpleNamespace(refresh_lock=threading.Lock(), refresh=lambda **kw: calls.append(kw))
    jobs = mcp_sync.SyncRequests(SimpleNamespace(_core=lambda _: core), config)
    payload = dict(library_id=library, mode='grant', client_id='test', connection_generation='old')
    with pytest.raises(ValueError, match='changed'): jobs.submit(payload)
    assert not jobs.running and not calls
    class DeferredThread:
        def __init__(self, *, target, **kwargs): self.target = target
        def start(self): pending.append(self.target)
    monkeypatch.setattr(mcp_sync.threading, 'Thread', DeferredThread)
    payload['connection_generation'] = 'current'
    assert jobs.submit(payload)['accepted']
    generation('reconnected')
    pending.pop()()
    assert not jobs.running and not calls


def test_inflight_result_discarded_on_service_file_replacement(bridge_env):
    bridge, record, state, root = bridge_env
    (root/'delay').touch()
    async def exercise():
        task = asyncio.create_task(bridge.request('call', 'request_sync'))
        while not (root/'calls').exists(): await asyncio.sleep(.01)
        worker = root/'worker.py'
        replacement = root/'next.py'
        replacement.write_text(worker.read_text() + '\n# service replacement\n')
        replacement.replace(worker)
        with pytest.raises(ValueError, match='changed'): await task
        assert len((root/'calls').read_text().splitlines()) == 1
        assert bridge.active == 0
    asyncio.run(exercise())


def test_pairing_error_is_preserved_and_account_change_is_explicit(bridge_env):
    from mcp import Client
    from noteford.mcp_bridge import create_server
    bridge, record, state, root = bridge_env
    worker = root/'worker.py'
    worker.write_text("import json,sys;sys.stdin.read();print(json.dumps({'isError':True,'content':[{'type':'text','text':'pairing_required: 等待用户确认'}]}))")
    async def exercise():
        async with Client(create_server(bridge)) as client:
            result = await client.call_tool('list_libraries', {})
            assert result.is_error and 'pairing_required' in result.content[0].text
            record['config'] = str(root/'other/config.local.json')
            result = await client.call_tool('list_libraries', {})
            assert result.is_error and 'mcp_account_changed' in result.content[0].text
    asyncio.run(exercise())
