"""Mac public AI launcher: preserve protocol bytes and use the bundled service."""
import json
import os
from pathlib import Path
import subprocess

import pytest


ENTRY = Path(__file__).resolve().parents[1] / "desktop/mcp-plugin/cli/noteford-ai"
pytestmark = pytest.mark.skipif(os.name == "nt", reason="macOS POSIX launcher")


@pytest.fixture
def app(tmp_path):
    bundle = tmp_path / "带 空格的 Noteford 开源版.app"
    service = bundle / "Contents/Resources/service/noteford-service"
    service.parent.mkdir(parents=True)
    service.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\ncat\n', encoding="utf-8")
    service.chmod(0o755)
    return {**os.environ, "NOTEFORD_APP_PATH": str(bundle)}


@pytest.mark.parametrize("action", ["connect", "tools", "call", "mcp"])
def test_transports_forward_arguments_and_stdin_without_protocol_noise(app, action):
    payload = '{"query":"中文 \\"quoted\\"", "limit":1}\n'
    args = ["--client-name", "Example AI"]
    if action == "call":
        args += ["--tool", "search_messages"]
    result = subprocess.run(["/bin/sh", str(ENTRY), action, *args],
                            env=app, input=payload, text=True, capture_output=True, check=True)
    expected = ["--mcp-auto"] if action == "mcp" else ["--mcp-skill", "--action", action]
    assert result.stdout == "\n".join([*expected, *args]) + "\n" + payload
    assert result.stderr == ""


def test_missing_app_and_invalid_action_are_machine_readable(tmp_path):
    env = {**os.environ, "NOTEFORD_APP_PATH": str(tmp_path / "absent.app")}
    for action, error in [("call", "app_missing"), ("bad", "unknown_action")]:
        result = subprocess.run(["/bin/sh", str(ENTRY), action], env=env,
                                input="{}", text=True, capture_output=True)
        assert result.returncode == 2 and result.stdout == ""
        assert json.loads(result.stderr)["error"] == error


def test_help_does_not_require_app(tmp_path):
    result = subprocess.run(["/bin/sh", str(ENTRY), "--help"],
                            env={**os.environ, "NOTEFORD_APP_PATH": str(tmp_path)},
                            text=True, capture_output=True, check=True)
    assert "standard MCP over stdio" in result.stdout
    assert "isError" in result.stdout
