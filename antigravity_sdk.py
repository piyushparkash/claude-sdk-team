"""
Antigravity Agent SDK (Python)
A production-ready Python SDK mirroring the Claude Agent SDK architecture.
Spawns and orchestrates the local `agy` (Antigravity CLI) engine via stdio/NDJSON streaming.
Zero API keys required — utilizes your active local Antigravity CLI session.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    AsyncGenerator,
    Callable,
    Dict,
    List,
    Optional,
    Union,
)


class EventType(str, Enum):
    INIT = "init"
    TEXT_DELTA = "text_delta"
    THOUGHT_DELTA = "thought_delta"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    STEP_DONE = "step_done"
    RESULT = "result"
    ERROR = "error"


@dataclass
class UsageStats:
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "UsageStats":
        if not d:
            return cls()
        return cls(
            input_tokens=d.get("input_tokens", 0),
            output_tokens=d.get("output_tokens", 0),
            thinking_tokens=d.get("thinking_tokens", 0),
            total_tokens=d.get("total_tokens", 0),
        )


@dataclass
class AgentEvent:
    """Represents a discrete event emitted by the Antigravity agent during execution."""
    type: EventType
    conversation_id: Optional[str] = None
    text: Optional[str] = None
    thought: Optional[str] = None
    tool_name: Optional[str] = None
    tool_args: Optional[Dict[str, Any]] = None
    tool_result: Optional[Any] = None
    usage: Optional[UsageStats] = None
    raw_payload: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_done(self) -> bool:
        return self.type in (EventType.RESULT, EventType.ERROR)


class AgentResponse:
    """Encapsulates the final response from an Agent prompt execution."""

    def __init__(
        self,
        text: str,
        conversation_id: str,
        status: str,
        duration_seconds: float = 0.0,
        usage: Optional[UsageStats] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
    ):
        self.text = text
        self.conversation_id = conversation_id
        self.status = status
        self.duration_seconds = duration_seconds
        self.usage = usage or UsageStats()
        self.tool_calls = tool_calls or []

    def __str__(self) -> str:
        return self.text

    def __repr__(self) -> str:
        return (
            f"AgentResponse(conversation_id='{self.conversation_id}', "
            f"status='{self.status}', tokens={self.usage.total_tokens})"
        )


class AntigravityAgent:
    """
    High-level Agent client that communicates with the local Antigravity CLI runtime.
    
    Features:
    - Zero API key required (piggybacks on your local CLI authentication).
    - Stateful multi-turn conversations & session resumption via conversation_id.
    - Real-time NDJSON event streaming (text, thoughts, tool executions).
    - Auto permission approval or custom inspection callbacks.
    """

    def __init__(
        self,
        cwd: Optional[Union[str, Path]] = None,
        conversation_id: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        auto_approve_permissions: bool = True,
        timeout_seconds: int = 300,
        on_tool_call: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ):
        self.cwd = str(cwd or os.getcwd())
        self.conversation_id = conversation_id
        self.model = model
        self.effort = effort  # "low", "medium", "high"
        self.auto_approve = auto_approve_permissions
        self.timeout = timeout_seconds
        self.on_tool_call = on_tool_call
        self._agy_bin = self._find_executable()

    def _find_executable(self) -> str:
        bin_path = shutil.which("agy") or shutil.which("agy.exe")
        if bin_path:
            return bin_path
        # Windows fallback path
        fallback = os.path.expanduser(r"~\AppData\Local\Programs\antigravity-cli\bin\agy.exe")
        if os.path.exists(fallback):
            return fallback
        return "agy"

    async def stream(
        self,
        prompt: str,
        conversation_id: Optional[str] = None,
        continue_conversation: bool = True,
    ) -> AsyncGenerator[AgentEvent, None]:
        """
        Streams agent execution events in real time.
        
        Yields:
            AgentEvent objects representing tokens, tool calls, and lifecycle events.
        """
        cmd = [
            self._agy_bin,
            "-p", prompt,
            "--output-format", "stream-json",
            "--print-timeout", f"{self.timeout}s",
        ]

        if self.auto_approve:
            cmd.append("--dangerously-skip-permissions")

        if self.model:
            cmd.extend(["--model", self.model])

        if self.effort:
            cmd.extend(["--effort", self.effort])

        active_conv = conversation_id or (self.conversation_id if continue_conversation else None)
        if active_conv:
            cmd.extend(["--conversation", active_conv])
        elif continue_conversation and self.conversation_id is not None:
            cmd.append("--continue")

        # Launch the local CLI process
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
        )

        try:
            assert process.stdout is not None
            while True:
                line = await process.stdout.readline()
                if not line:
                    break

                raw_line = line.decode("utf-8", errors="replace").strip()
                if not raw_line:
                    continue

                try:
                    payload = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                event_name = payload.get("event")

                # 1. Initialization Event
                if event_name == "init":
                    conv_id = payload.get("conversation_id")
                    if conv_id:
                        self.conversation_id = conv_id
                    yield AgentEvent(
                        type=EventType.INIT,
                        conversation_id=conv_id,
                        raw_payload=payload,
                    )

                # 2. Step Update Event
                elif event_name == "step_update":
                    step = payload.get("step_update", {})
                    step_type = step.get("step_type", "")
                    text_delta = step.get("text_delta")
                    state = step.get("state")

                    # Text delta
                    if text_delta:
                        yield AgentEvent(
                            type=EventType.TEXT_DELTA,
                            conversation_id=self.conversation_id,
                            text=text_delta,
                            raw_payload=payload,
                        )

                    # Tool call
                    tool_info = step.get("tool_call")
                    if tool_info:
                        t_name = tool_info.get("name")
                        t_args = tool_info.get("args", {})
                        if self.on_tool_call and callable(self.on_tool_call):
                            self.on_tool_call(t_name, t_args)

                        yield AgentEvent(
                            type=EventType.TOOL_CALL,
                            conversation_id=self.conversation_id,
                            tool_name=t_name,
                            tool_args=t_args,
                            raw_payload=payload,
                        )

                    if state == "DONE":
                        usage = UsageStats.from_dict(step.get("usage"))
                        yield AgentEvent(
                            type=EventType.STEP_DONE,
                            conversation_id=self.conversation_id,
                            usage=usage,
                            raw_payload=payload,
                        )

                # 3. Final Result Event
                elif event_name == "result":
                    res = payload.get("result", {})
                    conv_id = res.get("conversation_id") or self.conversation_id
                    if conv_id:
                        self.conversation_id = conv_id

                    usage = UsageStats.from_dict(res.get("usage"))
                    yield AgentEvent(
                        type=EventType.RESULT,
                        conversation_id=conv_id,
                        text=res.get("response", ""),
                        usage=usage,
                        raw_payload=payload,
                    )

        finally:
            if process.returncode is None:
                try:
                    await process.wait()
                except Exception:
                    pass

    async def chat(
        self,
        prompt: str,
        conversation_id: Optional[str] = None,
        continue_conversation: bool = True,
    ) -> AgentResponse:
        """
        Executes a prompt, streams internally, and returns a rich AgentResponse object.
        """
        text_chunks = []
        final_response_text = ""
        status = "SUCCESS"
        usage = UsageStats()
        duration = 0.0
        tool_calls = []

        async for event in self.stream(
            prompt,
            conversation_id=conversation_id,
            continue_conversation=continue_conversation,
        ):
            if event.type == EventType.TEXT_DELTA and event.text:
                text_chunks.append(event.text)
            elif event.type == EventType.TOOL_CALL:
                tool_calls.append({"name": event.tool_name, "args": event.tool_args})
            elif event.type == EventType.RESULT:
                final_response_text = event.text or "".join(text_chunks)
                status = event.raw_payload.get("result", {}).get("status", "SUCCESS")
                duration = event.raw_payload.get("result", {}).get("duration_seconds", 0.0)
                if event.usage:
                    usage = event.usage

        if not final_response_text:
            final_response_text = "".join(text_chunks)

        return AgentResponse(
            text=final_response_text,
            conversation_id=self.conversation_id or "",
            status=status,
            duration_seconds=duration,
            usage=usage,
            tool_calls=tool_calls,
        )

    # Alias for run()
    run = chat


# Convenience entrypoint
def create_agent(**kwargs) -> AntigravityAgent:
    """Factory helper to create an AntigravityAgent instance."""
    return AntigravityAgent(**kwargs)
