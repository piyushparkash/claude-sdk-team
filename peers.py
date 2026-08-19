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
import shutil
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


def _antigravity_binary() -> str | None:
    """Locate the local `agy` (Antigravity CLI) binary, if this device has
    one. Mirrors AntigravityAgent's own lookup (shutil.which + the Windows
    install path) so the availability check doesn't require constructing
    an AntigravityAgent instance first. A device with no antigravity_sdk.py
    or no `agy` binary (e.g. a phone) just always falls through to Claude --
    no special-casing needed there beyond this returning None."""
    import os

    found = shutil.which("agy") or shutil.which("agy.exe")
    if found:
        return found
    fallback = os.path.expanduser(r"~\AppData\Local\Programs\antigravity-cli\bin\agy.exe")
    return fallback if os.path.exists(fallback) else None


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
    """One independent, persistent teammate. Its own ClaudeSDKClient (or, if
    allowed and available, the local Antigravity CLI engine instead), own
    memory across the whole chat (never reconnected), no delegation
    machinery -- just a participant in the shared channel.

    Engine choice: antigravity gets preference when `allow_antigravity` is
    set and this device actually has the local `agy` CLI -- otherwise, or
    the moment antigravity fails once, this peer sticks to Claude for the
    rest of its life (no per-turn re-guessing; a broken local engine isn't
    worth retrying every message). A device with no `agy` binary (any
    phone today) transparently always falls through to Claude -- nothing
    device-specific needed here beyond that check.
    """

    def __init__(self, role_key: str, system_prompt: str, tools: list[str], cwd: str,
                 mcp_servers: dict | None = None, mcp_tool_names: list[str] | None = None,
                 allow_antigravity: bool = True):
        self.role_key = role_key
        self.system_prompt = system_prompt
        # `tools` is a HARD restriction on which built-in tools exist at all
        # (ClaudeAgentOptions.tools) -- `allowed_tools` (below) only controls
        # permission *prompting* and is a no-op once bypassPermissions is
        # set, so it alone doesn't stop a peer from using tools outside its
        # role. Confirmed live: lead (meant to have zero tools) used Read
        # directly and wrote+reviewed code itself, no hire, no cross-talk,
        # because `tools=` wasn't being set and bypassPermissions made the
        # allowed_tools restriction meaningless. Antigravity's CLI has no
        # equivalent hard tool restriction exposed -- callers that need
        # this guarantee (the lead) must pass allow_antigravity=False.
        self.tools = tools
        self.cwd = cwd
        self.mcp_servers = mcp_servers or {}
        self.mcp_tool_names = mcp_tool_names or []
        self.client: ClaudeSDKClient | None = None

        self.allow_antigravity = allow_antigravity
        self._antigravity = None  # AntigravityAgent instance, once decided
        self._antigravity_tried = False
        self._engine = "claude"  # flips to "antigravity" only on a working first call

    async def _ensure_claude(self) -> ClaudeSDKClient:
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

    async def _say_claude(self, incoming: str, on_note: NoteFn) -> str | None:
        client = await self._ensure_claude()
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

    async def _say_antigravity(self, incoming: str, on_note: NoteFn) -> str | None:
        from antigravity_sdk import AntigravityAgent, EventType

        if self._antigravity is None:
            self._antigravity = AntigravityAgent(cwd=self.cwd, auto_approve_permissions=True)
        agent = self._antigravity

        # No system_prompt hook in this SDK (unlike ClaudeAgentOptions) --
        # establish role/rules once by prefixing the very first message;
        # the CLI's own conversation continuation carries it after that,
        # same idea as ClaudeSDKClient's persistent connection just via a
        # different mechanism (conversation_id instead of an open socket).
        first_turn = agent.conversation_id is None
        prompt = f"{self.system_prompt}\n\n---\n\n{incoming}" if first_turn else incoming

        parts = []
        async for event in agent.stream(prompt):
            if event.type == EventType.TEXT_DELTA and event.text:
                parts.append(event.text)
            elif event.type == EventType.TOOL_CALL and on_note is not None:
                args_preview = ", ".join(f"{k}={v!r}" for k, v in (event.tool_args or {}).items())
                await on_note(f"({event.tool_name}: {args_preview[:80]})")
            elif event.type == EventType.RESULT and event.text:
                parts = [event.text]  # authoritative final text, same precedence as AgentResponse
            elif event.type == EventType.ERROR:
                raise RuntimeError(event.raw_payload.get("error") or "antigravity error event")
        text = "".join(parts).strip()
        return _clean_reply(text)

    async def say(self, incoming: str, on_note: NoteFn = None) -> str | None:
        """One turn: deliver `incoming`, return the reply text, or None if
        the peer PASSed. Streams interim tool-use activity via on_note."""
        if self.allow_antigravity and not self._antigravity_tried and _antigravity_binary():
            self._antigravity_tried = True
            try:
                result = await self._say_antigravity(incoming, on_note)
                self._engine = "antigravity"
                return result
            except Exception as exc:
                print(f"[{self.role_key}] antigravity engine failed ({exc}), "
                      f"falling back to Claude for this peer from now on")
                self._engine = "claude"
                # Falls through to the Claude path below for this turn too --
                # the human's message still deserves a real answer now, not
                # just a silent skip because the preferred engine hiccuped.
        return await self._say_claude(incoming, on_note)

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
