"""Account-scoped pause/generation; never changes an access grant."""
import json
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path


def connection_state(config: Path) -> tuple[bool, str]:
    from .libraries import load_root_config
    from .workspace_access import require_active
    root = Path(load_root_config(config)['project_root'])
    require_active(root)
    try:
        value = json.loads((root / 'data/mcp_connection.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return True, 'initial'
    if (not isinstance(value, dict) or value.get('version') != 1
            or not isinstance(value.get('enabled'), bool)
            or not isinstance(value.get('generation'), str) or not value['generation']):
        raise ValueError('mcp_connection_invalid')
    return value['enabled'], value['generation']


_expected_generation = ContextVar('mcp_expected_generation', default=None)


@contextmanager
def request_generation(generation):
    token = _expected_generation.set(generation)
    try:
        yield
    finally:
        _expected_generation.reset(token)


def expected_generation():
    return _expected_generation.get()


def require_connected(config: Path, expected=None):
    enabled, generation = connection_state(config)
    expected = expected if expected is not None else expected_generation()
    if not enabled or (expected is not None and generation != expected):
        raise ValueError('mcp_connection_changed')
    return generation
