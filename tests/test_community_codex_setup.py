import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def setup(monkeypatch):
    path = Path(__file__).parents[1] / 'desktop/mcp-plugin/skills/noteford-community/scripts/setup_codex.py'
    monkeypatch.setitem(sys.modules, 'noteford', SimpleNamespace(discovery_file=lambda: None, load_record=lambda _: None))
    spec = importlib.util.spec_from_file_location('community_setup', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def runner(items):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if args[2] == 'add':
            items.append({'name': args[3], 'enabled': True, 'transport': {'type': 'stdio', 'command': args[5], 'args': args[6:]}})
        return SimpleNamespace(stdout=json.dumps(items))
    return run, calls


def test_add_preserves_other_servers_and_repeat_is_noop(setup):
    items = [{'name': 'member', 'enabled': True, 'transport': {'type': 'stdio', 'command': '/private/service', 'args': []}}]
    original = json.loads(json.dumps(items))
    run, calls = runner(items)
    record = {'command': '/Applications/Noteford Community.app/service/noteford-service'}
    assert setup.configure('codex', record, run) == 'configured'
    assert items[:1] == original
    assert setup.configure('codex', record, run) == 'already_configured'
    assert sum(c[2] == 'add' for c in calls) == 1


@pytest.mark.parametrize('enabled,command', [(False, '/app/noteford-service'), (True, '/other/noteford-service')])
def test_conflict_or_disabled_never_overwritten(setup, enabled, command):
    run, calls = runner([{'name': setup.NAME, 'enabled': enabled, 'transport': {'type': 'stdio', 'command': command, 'args': ['--mcp-auto', '--client-name', 'Codex']}}])
    with pytest.raises(RuntimeError):
        setup.configure('codex', {'command': '/app/noteford-service'}, run)
    assert all(c[2] != 'add' for c in calls)


def test_development_runtime_not_registered(setup):
    with pytest.raises(RuntimeError):
        setup.configure('codex', {'command': '/usr/bin/python3', 'args': ['-m', 'noteford.service']}, lambda *a, **k: pytest.fail('must not invoke CLI'))
