#!/usr/bin/env python3
"""Open one Noteford MCP session from the public discovery record.

Windows keeps scripts/noteford.ps1. This entry is for macOS and any host
without PowerShell. It does not store tokens or read chat data.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def discovery_file(platform: str | None = None, home: Path | None = None, local_app_data: str | None = None) -> Path:
    override = os.environ.get("NOTEFORD_DISCOVERY")
    if override:
        return Path(override)
    platform = platform or sys.platform
    home = home or Path.home()
    if platform == "darwin":
        return home / "Library" / "Application Support" / "Noteford" / "connection.json"
    base = Path(local_app_data) if local_app_data else Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local")))
    return base / "Noteford" / "connection.json"


def _python_command(command: Path) -> bool:
    name = command.name.lower()
    return name in {"python", "python.exe", "python3", "python3.exe"} or name.startswith("python3.")


def command_acceptable(record: dict) -> bool:
    app = Path(record["appExecutable"])
    command = Path(record["command"])
    args = record.get("args") or []
    windows = app.parent / "resources" / "service" / "noteford-service.exe"
    mac = (
        app.parent / "resources" / "service" / "noteford-service",
        app.parent.parent / "Resources" / "service" / "noteford-service",
    )
    try:
        resolved = command.resolve()
    except OSError:
        return False
    if resolved == windows.resolve() or any(resolved == item.resolve() for item in mac):
        return command.is_file()
    return (
        isinstance(args, list)
        and list(map(str, args[:2])) == ["-m", "noteford.service"]
        and _python_command(command)
        and command.is_file()
    )


def load_record(path: Path, deadline_seconds: float = 7.0) -> dict:
    deadline = time.monotonic() + deadline_seconds
    last_error = "app_not_running"
    while True:
        try:
            record = json.loads(path.read_text(encoding="utf-8-sig"))
            if record.get("product") != "Noteford" or record.get("version") != 1:
                raise ValueError("discovery_invalid")
            if not command_acceptable(record):
                raise ValueError("discovery_invalid")
            return record
        except (OSError, ValueError, json.JSONDecodeError, KeyError) as exc:
            last_error = "discovery_invalid" if str(exc) == "discovery_invalid" else "app_not_running"
        if time.monotonic() >= deadline:
            raise SystemExit(last_error)
        time.sleep(0.25)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", choices=["connect", "tools", "call", "serve"], default="connect")
    parser.add_argument("--client-name", required=True)
    parser.add_argument("--tool")
    parser.add_argument("--arguments-file", type=Path)
    parser.add_argument("--image-directory", type=Path)
    parser.add_argument("--discovery", type=Path)
    args = parser.parse_args(argv)
    path = args.discovery or discovery_file()
    try:
        record = load_record(path)
    except SystemExit as exc:
        print("请打开 Noteford 后重试。连接信息恢复后可直接重试，无需重新授权。", file=sys.stderr)
        return 2 if str(exc) else 2
    mode = "--mcp-auto" if args.action == "serve" else "--mcp-skill"
    service_args = [*(record.get("args") or []), mode, "--client-name", args.client_name, "--discovery", str(path)]
    if args.image_directory:
        service_args += ["--image-directory", str(args.image_directory)]
    if args.action != "serve":
        service_args += ["--action", args.action]
    stdin_payload = None
    if args.action == "call":
        if not args.tool or not args.arguments_file:
            print("call 需要 --tool 和 UTF-8 JSON --arguments-file。", file=sys.stderr)
            return 2
        stdin_payload = args.arguments_file.read_text(encoding="utf-8")
        json.loads(stdin_payload)
        service_args += ["--tool", args.tool]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    completed = subprocess.run(
        [record["command"], *service_args],
        input=stdin_payload,
        text=True,
        env=env,
    )
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
