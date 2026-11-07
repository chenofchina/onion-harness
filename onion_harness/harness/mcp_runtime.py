"""Bridge generated MCP tools into the executor tool surface.

Each enabled tool is a separate Python process speaking JSON-RPC over stdio. We
spawn lazily, keep one process per tool, and expose one :class:`OnionTool` per
generated script. Hot reload tears down and rebuilds the whole set.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from onion_harness.core.tool import OnionTool
from onion_harness.harness.mcp_factory import McpStore

logger = logging.getLogger("onion.mcp")

CALL_TIMEOUT = 60.0


class McpToolClient:
    """One generated tool, one lazily started subprocess."""

    def __init__(self, name: str, title: str, description: str, script: Path, schema: Dict[str, Any]) -> None:
        self.name = name
        self.title = title
        self.description = description
        self.script = script
        self.schema = schema
        self._process: Optional[asyncio.subprocess.Process] = None
        self._lock = asyncio.Lock()

    async def _ensure(self) -> asyncio.subprocess.Process:
        if self._process is not None and self._process.returncode is None:
            return self._process
        self._process = await asyncio.create_subprocess_exec(
            sys.executable or "python",
            str(self.script),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(self.script.parent),
        )
        await self._call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        return self._process

    async def _call(self, request: Dict[str, Any]) -> Dict[str, Any]:
        process = self._process
        if process is None or process.stdin is None or process.stdout is None:
            raise RuntimeError("mcp process is not running")
        process.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
        await process.stdin.drain()
        while True:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=CALL_TIMEOUT)
            if not line:
                raise RuntimeError("mcp process closed the stream")
            try:
                response = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if response.get("id") == request.get("id"):
                return response

    async def invoke(self, arguments: Dict[str, Any]) -> str:
        async with self._lock:
            try:
                await self._ensure()
            except Exception as exc:  # noqa: BLE001
                return f"MCP tool '{self.name}' could not start: {exc}"
            try:
                response = await self._call(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": self.name, "arguments": arguments or {}},
                    }
                )
            except asyncio.TimeoutError:
                await self.close()
                return f"MCP tool '{self.name}' timed out after {CALL_TIMEOUT:g}s"
            except Exception as exc:  # noqa: BLE001
                await self.close()
                return f"MCP tool '{self.name}' failed: {exc}"

            if "error" in response:
                error = response["error"]
                return f"MCP error: {error.get('message') or error}"

            result = response.get("result") or {}
            content = result.get("content") or []
            texts = [item.get("text", "") for item in content if isinstance(item, dict)]
            return "\n".join(texts).strip() or "(no output)"

    async def close(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.returncode is not None:
            return
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=5)
        except Exception:  # noqa: BLE001
            try:
                process.kill()
            except Exception:  # noqa: BLE001
                pass

    def as_tool(self) -> OnionTool:
        async def _handler(args: Dict[str, Any]) -> str:
            return await self.invoke(args)

        return OnionTool(
            name=self.name,
            description=f"[MCP] {self.description}",
            input_schema=self.schema,
            handler=_handler,
            metadata={
                "kind": "mcp",
                "label": self.title,
                "icon": "mcp",
                "generated": True,
            },
        )


class McpRuntime:
    """Owns the live set of generated MCP tools."""

    def __init__(self, store: McpStore) -> None:
        self.store = store
        self._clients: Dict[str, McpToolClient] = {}

    def load(self) -> List[OnionTool]:
        """(Re)build the tool set from disk. Safe to call for hot reload."""
        self._clients = {}
        tools: List[OnionTool] = []
        for entry in self.store.enabled_entries():
            name = entry.get("name")
            if not name:
                continue
            script = self.store.tool_dir(name) / "tool.py"
            if not script.is_file():
                continue
            client = McpToolClient(
                name=name,
                title=entry.get("title") or name,
                description=entry.get("description") or "",
                script=script,
                schema=entry.get("parameters") or {"type": "object", "properties": {}},
            )
            self._clients[name] = client
            tools.append(client.as_tool())
        return tools

    async def aclose(self) -> None:
        for client in list(self._clients.values()):
            await client.close()
        self._clients = {}

    def describe(self) -> List[Dict[str, Any]]:
        return self.store.load_index()


__all__ = ["McpRuntime", "McpToolClient"]