"""Real stdio protocol tests; synthetic service code only, never daily data."""
import asyncio
import json
import os
from pathlib import Path
import sys

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.types import ServerNotification


@pytest.fixture
def refresh_process(tmp_path):
    root = tmp_path
    (root/'data').mkdir()
    config = root/'config.local.json'
    config.write_text(json.dumps({'project_root': str(root)}))
    worker = root/'worker.py'
    def version(n):
        worker.write_text('''import json,sys
from pathlib import Path
root=Path(__file__).parent
json.loads(sys.stdin.read())
with (root/'calls').open('a') as f: f.write('call\\n')
action=sys.argv[sys.argv.index('--action')+1]
''' + f"print(json.dumps({{'tools':[{{'name':'version_{n}','inputSchema':{{'type':'object'}}}}], '_meta':{{'noteford':{{'instructions':'guide version {n}'}}}}}} if action=='tools' else {{'content':[{{'type':'text','text':'version_{n}'}}]}}))\n")
    version(1)
    discovery = root/'discovery.json'
    record = dict(command=sys.executable, args=[str(worker)], config=str(config), pid=os.getpid(), instance='one')
    discovery.write_text(json.dumps(record))
    launcher = root/'bridge.py'
    launcher.write_text('''import asyncio,json,sys
from pathlib import Path
from noteford import skill_connection
from noteford.mcp_bridge import Bridge,create_server
from mcp.server.stdio import stdio_server
# Isolate discovery trust from the schema hot-swap test; production discovery
# and authorization are exercised by test_mcp_bridge's real service fixture.
path=Path(sys.argv[1])
def discover(_):
    if not path.exists(): raise ValueError('app_not_running')
    return json.loads(path.read_text())
skill_connection.discover=discover
async def main():
    bridge=Bridge('Refresh fixture',path)
    bridge.refresh.interval=.05
    server=create_server(bridge)
    async with stdio_server() as (read,write):
        await server.run(read,write,server.create_initialization_options())
asyncio.run(main())
''')
    params=StdioServerParameters(command=sys.executable,args=[str(launcher),str(discovery)],
        env={**os.environ,'PYTHONPATH':str(Path(__file__).parents[1]/'src'),'PYTHONDONTWRITEBYTECODE':'1'})
    def gate(enabled, generation):
        (root/'data/mcp_connection.json').write_text(json.dumps(dict(version=1,enabled=enabled,generation=generation)))
    return params, root, version, gate, record


@pytest.mark.parametrize('mode', ['auto','legacy'])
def test_same_stdio_schema_refresh_recovery_and_idle_no_workers(refresh_process, mode):
    import psutil
    params, root, version, gate, record=refresh_process
    events=asyncio.Queue()
    async def handler(message):
        if isinstance(message,ServerNotification) and message.method=='notifications/tools/list_changed':
            await events.put(message)
    def pids():
        return [p.pid for p in psutil.Process().children(recursive=True)
                if str(root/'bridge.py') in p.cmdline()]
    async def exercise():
        async with Client(params,mode=mode,message_handler=handler,read_timeout_seconds=5) as client:
            original=pids();assert len(original)==1
            assert (client.session.protocol_version == '2026-07-28') == (mode == 'auto')
            assert client.server_capabilities.tools.list_changed is True
            assert [t.name for t in (await client.list_tools()).tools]==['call_current_tool','refresh_mcp','version_1']
            async def steps(next_event):
                count=(root/'calls').read_text()
                await asyncio.sleep(.18)
                assert (root/'calls').read_text()==count  # idle never launches a worker
                version(2)  # replace actual worker code, including its schema
                await next_event()
                assert [t.name for t in (await client.list_tools()).tools]==['call_current_tool','refresh_mcp','version_2']
                assert (await client.call_tool('version_2',{})).content[0].text=='version_2'
                gate(False,'paused')
                await next_event()
                result=await client.call_tool('version_2',{})
                assert result.is_error and 'mcp_disconnected' in result.content[0].text
                gate(True,'resumed')
                await next_event()
                assert not (await client.call_tool('version_2',{})).is_error
                discovery=root/'discovery.json'
                discovery.unlink()
                await next_event()
                unavailable = await client.list_tools()
                assert [t.name for t in unavailable.tools] == ['call_current_tool','refresh_mcp']
                assert 'app_not_running' in str(unavailable.meta)
                result=await client.call_tool('version_2',{})
                assert result.is_error and 'app_not_running' in result.content[0].text
                record['instance']='replacement'
                discovery.write_text(json.dumps(record))
                await next_event()
                assert not (await client.call_tool('version_2',{})).is_error
                assert pids()==original
            if mode=='legacy':
                await steps(lambda: asyncio.wait_for(events.get(),3))
            else:
                # No unsolicited modern change notifications before opt-in.
                assert events.empty()
                async with client.listen(tools_list_changed=True) as sub:
                    assert sub.honored.tools_list_changed
                    await steps(lambda: asyncio.wait_for(anext(sub),3))
        assert not pids()
    asyncio.run(exercise())


def test_modern_changes_require_opt_in_and_filter(refresh_process):
    params, root, version, _, _ = refresh_process
    unsolicited = []
    async def handler(message):
        if isinstance(message, ServerNotification) and message.method == 'notifications/tools/list_changed':
            unsolicited.append(message)
    async def exercise():
        async with Client(params, message_handler=handler, read_timeout_seconds=5) as client:
            await client.list_tools()
            version(2)
            await asyncio.sleep(.18)
            assert not unsolicited
            async with client.listen(prompts_list_changed=True) as sub:
                version(3)
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(anext(sub), .18)
            assert [t.name for t in (await client.list_tools()).tools] == ['call_current_tool','refresh_mcp','version_3']
    asyncio.run(exercise())


@pytest.mark.parametrize('mode', ['auto', 'legacy'])
def test_refresh_controls_work_with_cached_host_catalog(refresh_process, mode):
    params, root, version, gate, record = refresh_process
    async def exercise():
        async with Client(params, mode=mode, read_timeout_seconds=10) as client:
            old = await client.list_tools()
            assert 'version_1' in [t.name for t in old.tools]
            first = await client.call_tool('refresh_mcp', {})
            assert not first.is_error
            before = first.structured_content
            version(2)
            # Host intentionally never calls tools/list again. The two stable
            # entries still expose the new catalog and dispatch the new tool.
            refreshed = await client.call_tool('refresh_mcp', {})
            after = refreshed.structured_content
            assert after['instructions'] == 'guide version 2'
            assert before['instructions'] == 'guide version 1'
            assert after['instructions_version'] != before['instructions_version']
            assert after['runtime_version'] != before['runtime_version']
            assert after['catalog_version'] != before['catalog_version']
            assert after['bridge_version'] == before['bridge_version'] == '2'
            assert after['client_reload_verified'] is False
            assert 'version_2' in [tool['name'] for tool in after['tools']]
            called = await client.call_tool('call_current_tool', {'name':'version_2','arguments':{}})
            assert not called.is_error and called.content[0].text == 'version_2'
            for name in ['refresh_mcp','call_current_tool','missing']:
                assert (await client.call_tool('call_current_tool', {'name':name,'arguments':{}})).is_error
            assert (await client.call_tool('refresh_mcp', {'enable':True})).is_error
            gate(False,'paused')
            gate_file = root/'data/mcp_connection.json'
            saved = gate_file.read_bytes()
            paused = await client.call_tool('refresh_mcp', {})
            assert not paused.is_error and paused.structured_content['connection_enabled'] is False
            assert gate_file.read_bytes() == saved
            assert (await client.call_tool('call_current_tool', {'name':'version_2','arguments':{}})).is_error
            gate(True,'resumed')
            assert not (await client.call_tool('call_current_tool', {'name':'version_2','arguments':{}})).is_error
    asyncio.run(exercise())
