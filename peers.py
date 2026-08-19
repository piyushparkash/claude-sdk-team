"""
Peer execution: the AgentPeer class (independent, persistent teammate) plus
a RemotePeer that presents the exact same interface but executes on another
device over the LAN via backend_server.py's HTTP/SSE endpoint.

From ChatSession's round-robin loop's point of view a RemotePeer is
indistinguishable from a local AgentPeer -- both are just `say(incoming,
on_note) -> str | None` plus `disconnect()`.
"""

from __future__ import annotations

import json
from typing import Awaitable, Callable

import httpx

import beacon
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

NoteFn = Callable[[str], Awaitable[None]] | None


def _describe_tool_use(block: ToolUseBlock) -> str | None:
    """Turn a peer's own tool call into a short 'what I'm doing right now'
    line, so the chat shows activity while it works instead of long silence
    before a final reply. Returns None for tools not worth narrating."""
    inp = block.input or {}
    if block.name == "Read":
        return f"(reading `{inp.get('file_path', '?')}`)"
    if block.name == "Write":
        return f"(writing `{inp.get('file_path', '?')}`)"
    if block.name == "Edit":
        return f"(editing `{inp.get('file_path', '?')}`)"
    if block.name == "Grep":
        return f"(searching code for '{inp.get('pattern', '?')}')"
    if block.name == "Glob":
        return f"(listing files matching '{inp.get('pattern', '?')}')"
    if block.name == "WebSearch":
        return f"(searching the web: '{inp.get('query', '?')}')"
    if block.name == "WebFetch":
        return f"(reading {inp.get('url', '?')})"
    if block.name == "Bash":
        cmd = (inp.get("command") or "?").strip()
        return f"(running: `{cmd[:60]}{'…' if len(cmd) > 60 else ''}`)"
    return None


class AgentPeer:
    """One independent, persistent teammate. Its own ClaudeSDKClient, its
    own memory across the whole chat (never reconnected), no delegation
    machinery -- just a participant in the shared channel."""

    def __init__(self, role_key: str, system_prompt: str, tools: list[str], cwd: str,
                 mcp_servers: dict | None = None, mcp_tool_names: list[str] | None = None):
        self.role_key = role_key
        self.system_prompt = system_prompt
        # `tools` is a HARD restriction on which built-in tools exist at all
        # (ClaudeAgentOptions.tools) -- `allowed_tools` (below) only controls
        # permission *prompting* and is a no-op once bypassPermissions is
        # set, so it alone doesn't stop a peer from using tools outside its
        # role. Confirmed live: lead (meant to have zero tools) used Read
        # directly and wrote+reviewed code itself, no hire, no cross-talk,
        # because `tools=` wasn't being set and bypassPermissions made the
        # allowed_tools restriction meaningless.
        self.tools = tools
        self.cwd = cwd
        self.mcp_servers = mcp_servers or {}
        self.mcp_tool_names = mcp_tool_names or []
        self.client: ClaudeSDKClient | None = None

    async def _ensure(self) -> ClaudeSDKClient:
        if self.client is None:
            options = ClaudeAgentOptions(
                system_prompt=self.system_prompt,
                tools=self.tools,
                allowed_tools=self.tools + self.mcp_tool_names,
                mcp_servers=self.mcp_servers,
                permission_mode="bypassPermissions",
                cwd=self.cwd,
            )
            client = ClaudeSDKClient(options=options)
            await client.connect()
            self.client = client
        return self.client

    async def say(self, incoming: str, on_note: NoteFn = None) -> str | None:
        """One turn: deliver `incoming`, return the reply text, or None if
        the peer PASSed. Streams interim tool-use activity via on_note."""
        client = await self._ensure()
        await client.query(incoming)
        parts = []
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage) and message.parent_tool_use_id is None:
                for block in message.content:
                    if isinstance(block, TextBlock):
                        parts.append(block.text)
                    elif isinstance(block, ToolUseBlock) and on_note is not None:
                        note = _describe_tool_use(block)
                        if note:
                            await on_note(note)
            elif isinstance(message, ResultMessage):
                pass
        text = "\n".join(p for p in parts if p.strip()).strip()
        return _clean_reply(text)

    async def disconnect(self) -> None:
        if self.client is not None:
            await self.client.disconnect()
            self.client = None


def _clean_reply(text: str) -> str | None:
    import re
    if not text or text.upper() == "PASS":
        return None
    # Defensive cleanup: prompt says PASS must be standalone, but if a
    # peer tacks a trailing "PASS" line onto real content anyway, strip
    # it rather than leaking the literal word into the chat.
    text = re.sub(r"\n+PASS\s*$", "", text, flags=re.IGNORECASE).strip()
    return text or None


class RemotePeer:
    """Same interface as AgentPeer, but the actual turn runs on another
    device's backend_server.py over the LAN. No LAN address is stored --
    every call resolves the device's current IP live via the discovery
    beacon (beacon.py), so a router restart / DHCP change can't break it.
    An unreachable or non-responding device is treated like a PASS for
    that turn -- don't stall the whole discussion on one offline machine."""

    def __init__(self, role_key: str, device_id: str, shared_secret: str | None = None,
                 timeout: float = 120.0, discover_timeout: float = 2.0):
        self.role_key = role_key
        self.device_id = device_id
        self.shared_secret = shared_secret
        self.timeout = timeout
        self.discover_timeout = discover_timeout

    async def say(self, incoming: str, on_note: NoteFn = None) -> str | None:
        base_url = await beacon.discover(self.device_id, timeout=self.discover_timeout)
        if base_url is None:
            print(f"[remote:{self.role_key}] device '{self.device_id}' didn't answer the beacon, treating as PASS")
            return None
        headers = {"X-Team-Secret": self.shared_secret} if self.shared_secret else {}
        url = f"{base_url}/peer/{self.role_key}/message"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream("POST", url, json={"text": incoming}, headers=headers) as resp:
                    if resp.status_code != 200:
                        print(f"[remote:{self.role_key}] HTTP {resp.status_code}, treating as PASS")
                        return None
                    reply: str | None = None
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        event = json.loads(line[len("data: "):])
                        if event["type"] == "note" and on_note is not None:
                            await on_note(event["text"])
                        elif event["type"] == "reply":
                            reply = event["text"]
                    return reply
        except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadError) as exc:
            print(f"[remote:{self.role_key}] unreachable ({exc}), treating as PASS")
            return None

    async def disconnect(self) -> None:
        pass  # nothing held open on this side -- the backend owns its own client
