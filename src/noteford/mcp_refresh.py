"""Bounded, metadata-only invalidation for one stdio bridge."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from mcp.server.lowlevel.server import NotificationOptions, Server
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler, ToolsListChanged
from mcp_types.version import MODERN_PROTOCOL_VERSIONS


def stamp(path: Path):
    try:
        value = path.stat()
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns
    except OSError:
        return None


class Refresh:
    def __init__(self, discovery: Path, interval: float = 2.0):
        self.interval = interval
        self.discovery = discovery
        self.paths = {discovery}
        self.previous = {discovery: stamp(discovery)}
        self.bus = InMemorySubscriptionBus()
        self.listen = ListenHandler(self.bus, max_subscriptions=4, max_buffered_events=8)
        self.legacy = None

    def track(self, paths):
        self.paths = {self.discovery, *paths}
        self.previous = {path: self.previous[path] if path in self.previous else stamp(path)
                         for path in self.paths}

    def context(self, context):
        if context.protocol_version not in MODERN_PROTOCOL_VERSIONS:
            self.legacy = context.session

    async def notify(self):
        await self.bus.publish(ToolsListChanged())
        if self.legacy is not None:
            try:
                await asyncio.wait_for(self.legacy.send_tool_list_changed(), 2)
            except Exception:
                self.legacy = None

    async def watch(self):
        while True:
            await asyncio.sleep(self.interval)
            current = {path: stamp(path) for path in self.paths}
            if current == self.previous:
                continue
            self.previous = current
            # No worker, decryption, index reads, or tool replay on an idle tick.
            await self.notify()

    @asynccontextmanager
    async def lifespan(self, _server):
        task = asyncio.create_task(self.watch())
        try:
            yield {}
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            self.listen.close()
            self.legacy = None


class RefreshServer(Server):
    def create_initialization_options(self, notification_options=None, **kwargs):
        options = notification_options or NotificationOptions()
        options.tools_changed = True
        return super().create_initialization_options(notification_options=options, **kwargs)
