import asyncio
import json
from pathlib import Path

import pytest

from test_local_pairing import env, call as paired_call
from noteford import skill_connection as skill


@pytest.mark.parametrize('signature_ok,caller', [(True, 'codex'), (False, 'noteford-service')])
def test_moved_signed_bridge_preserves_real_caller(monkeypatch, signature_ok, caller):
    from types import SimpleNamespace
    old = '/backup/replaced-Noteford.app/Contents/Resources/service/noteford-service'
    own = '/Applications/Noteford.app/Contents/Resources/service/noteford-service'
    bridge = SimpleNamespace(pid=123, exe=lambda: old, create_time=lambda: 1, is_running=lambda: True)
    codex = SimpleNamespace(exe=lambda: '/Applications/Codex.app/Contents/Resources/codex')
    monkeypatch.setattr(skill.sys, 'platform', 'darwin')
    monkeypatch.setattr(skill.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(skill.sys, 'executable', own)
    monkeypatch.setattr(skill.psutil, 'Process', lambda pid=None: bridge if pid else SimpleNamespace(parents=lambda: [bridge, codex]))
    def verify(args, **kwargs):
        assert args[0] == '/usr/bin/codesign' and args[-1] == '123'
        assert 'identifier "noteford-service"' in args[-2]
        assert 'QD3H9VHB23' in args[-2] and 'anchor apple generic' in args[-2]
        assert '1.2.840.113635.100.6.1.13' in args[-2]
        assert kwargs['timeout'] == 3
        return SimpleNamespace(returncode=0 if signature_ok else 1)
    monkeypatch.setattr(skill.subprocess, 'run', verify)
    assert Path(skill.client_program()).name == caller


@pytest.mark.parametrize('name', ['node', 'python3', 'noteford-service-fake'])
def test_interpreters_and_other_names_are_not_skipped(monkeypatch, name):
    from types import SimpleNamespace
    monkeypatch.setattr(skill.sys, 'platform', 'darwin')
    monkeypatch.setattr(skill.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(skill.sys, 'executable', '/Applications/Noteford.app/service/noteford-service')
    monkeypatch.setattr(skill.psutil, 'Process', lambda: SimpleNamespace(parents=lambda: [SimpleNamespace(exe=lambda: '/tmp/' + name)]))
    monkeypatch.setattr(skill.subprocess, 'run', lambda *a, **k: pytest.fail('must not validate arbitrary programs'))
    assert skill.client_program() == str(Path('/tmp', name).resolve())


@pytest.mark.parametrize('failure', ['timeout', 'exit', 'reused', 'oserror'])
def test_dynamic_service_verification_fails_closed(monkeypatch, failure):
    from types import SimpleNamespace
    monkeypatch.setattr(skill.sys, 'platform', 'darwin')
    monkeypatch.setattr(skill.sys, 'frozen', True, raising=False)
    process = SimpleNamespace(pid=123, create_time=lambda: 1, is_running=lambda: failure != 'exit')
    monkeypatch.setattr(skill.psutil, 'Process', lambda _: SimpleNamespace(create_time=lambda: 2 if failure == 'reused' else 1))
    def verify(*args, **kwargs):
        if failure == 'timeout':
            raise skill.subprocess.TimeoutExpired('codesign', 3)
        if failure == 'oserror':
            raise OSError('unavailable')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(skill.subprocess, 'run', verify)
    assert not skill._signed_service_ancestor(process, Path('/backup/noteford-service'))


def test_fresh_sessions_require_approval_then_reuse_and_respect_revocation(env, monkeypatch):
    pairing = env['pairing']
    monkeypatch.setattr(skill, 'open_pairing', lambda _: pairing)
    record = {'config': str(env['config'])}
    program = 'C:/Apps/Codex/codex.exe'
    def invoke(action='connect', tool=None, arguments=None):
        return asyncio.run(skill.call(record, 'Codex', program, action, tool, arguments or {}))
    first = invoke()
    assert first['structuredContent']['paired'] is False
    assert 'nfp_' not in json.dumps(first)
    request = pairing.requests()[0]
    pairing.review(request['request_id'])
    approved = pairing.approve(request['request_id'], 'g1')
    assert invoke()['structuredContent']['paired'] is True
    result = invoke('call', 'read_messages', {'library_id': env['lib'].id, 'conversation_id': 'group@chatroom'})
    assert not result.get('isError') and 'LOCAL-SECRET-TEXT' in json.dumps(result)
    other = asyncio.run(skill.call(record, 'Codex', 'C:/Other/ai.exe', 'connect', None, {}))
    assert other['structuredContent']['paired'] is False
    pairing.revoke(approved['binding_id'])
    denied = invoke('call', 'read_messages', {'library_id': env['lib'].id, 'conversation_id': 'group@chatroom'})
    assert denied['isError'] and 'LOCAL-SECRET-TEXT' not in json.dumps(denied)


def test_denied_profile_does_not_repeat_prompt(env, monkeypatch):
    pairing = env['pairing']
    token = pairing.auto_token('Codex', 'C:/Apps/Codex/codex.exe')
    paired_call(env, token, tool='request_pairing')
    request = pairing.requests()[0]
    pairing.review(request['request_id'])
    pairing.deny(request['request_id'])
    assert pairing.auto_token('Codex', 'C:/Apps/Codex/codex.exe') == token
    paired_call(env, token, tool='request_pairing')
    assert pairing.requests() == []
    assert pairing.bindings() == []


@pytest.mark.parametrize('platform', ['win32', 'darwin'])
def test_discovery_rejects_stopped_or_replaced_application(tmp_path, monkeypatch, platform):
    from noteford import platform_seam
    accepts = platform_seam.service_command_acceptable
    monkeypatch.setattr(platform_seam, 'service_command_acceptable',
                        lambda app, command, **kwargs: accepts(app, command, platform=platform, **kwargs))
    app = tmp_path / ('Noteford.app/Contents/MacOS/Noteford' if platform == 'darwin' else 'Noteford.exe')
    service = tmp_path / ('Noteford.app/Contents/Resources/service/noteford-service' if platform == 'darwin' else 'resources/service/noteford-service.exe')
    service.parent.mkdir(parents=True)
    service.touch()
    config = tmp_path / 'config.local.json'
    config.write_text('{}')
    manifest = tmp_path / 'connection.json'
    record = dict(version=1, product='Noteford', pid=123, appExecutable=str(app), command=str(service), config=str(config))
    manifest.write_text(json.dumps(record))
    class Process:
        def is_running(self): return True
        def exe(self): return str(app)
    monkeypatch.setattr(skill.psutil, 'Process', lambda _: Process())
    assert skill.discover(manifest)['pid'] == 123
    record['command'] = str(config)
    manifest.write_text(json.dumps(record))
    with pytest.raises(ValueError, match='discovery_invalid'):
        skill.discover(manifest)
    monkeypatch.setattr(Process, 'is_running', lambda _: False)
    with pytest.raises(ValueError, match='app_not_running'):
        skill.discover(manifest)


def test_discovery_accepts_python_module_and_rejects_a_bare_interpreter(tmp_path, monkeypatch):
    app = tmp_path / 'Noteford.app' / 'Contents' / 'MacOS' / 'Noteford'
    app.parent.mkdir(parents=True)
    app.write_bytes(b'')
    python = tmp_path / 'python3'
    python.write_bytes(b'')
    config = tmp_path / 'config.local.json'
    config.write_text('{}', encoding='utf-8')
    manifest = tmp_path / 'connection.json'
    record = {
        'version': 1,
        'product': 'Noteford',
        'pid': 7,
        'appExecutable': str(app),
        'command': str(python),
        'args': ['-m', 'noteford.service'],
        'config': str(config),
    }
    manifest.write_text(json.dumps(record), encoding='utf-8')

    class Process:
        def is_running(self):
            return True

        def exe(self):
            return str(app)

    monkeypatch.setattr(skill.psutil, 'Process', lambda _: Process())
    assert skill.discover(manifest)['command'] == str(python)
    record['args'] = []
    manifest.write_text(json.dumps(record), encoding='utf-8')
    with pytest.raises(ValueError, match='discovery_invalid'):
        skill.discover(manifest)


def test_metadata_listing_never_opens_or_creates_pairing(env, monkeypatch):
    monkeypatch.setattr(skill, 'open_pairing', lambda *_: pytest.fail('metadata must not access pairing'))
    from noteford import mcp_instructions
    monkeypatch.setattr(mcp_instructions, 'INSTRUCTIONS', 'current worker guide')
    result = asyncio.run(skill.call({'config': str(env['config'])}, 'Codex', '/fixture', 'tools', None, {}))
    assert result['_meta']['noteford']['instructions'] == 'current worker guide'
    assert 'request_pairing' in [tool['name'] for tool in result['tools']]
    assert env['pairing'].requests() == []
