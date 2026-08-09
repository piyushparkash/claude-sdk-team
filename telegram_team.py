"""
Bridge: Telegram chat <-> claude-agent-sdk team (lead + researcher/coder/reviewer).

Every message is posted with a "Role: text" prefix so a single Telegram
chat reads like a group chat between Lead and teammates. The lead can also
hire/fire teammates at runtime via two in-process tools (add_teammate /
remove_teammate) — the roster change takes effect on the next message, via
a reconnect that resumes the same session so context isn't lost.

Run: python telegram_team.py
Then message your bot on Telegram.
"""

import asyncio
import os
import re

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from claude_agent_sdk import (
    AgentDefinition,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    AssistantMessage,
    UserMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    tool,
    create_sdk_mcp_server,
)

from team import LEAD_PROMPT, CHAT_STYLE, ONE_QUESTION_RULE

load_dotenv()

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DELEGATE_TOOL = "Agent"  # this SDK build's Task-delegation tool is named "Agent"
DEFAULT_EMOJI = "🤖"

# Roster starts empty — lead hires whatever roles a task actually needs via
# add_teammate, rather than three fixed roles sitting idle for every chat.
_team: dict[str, AgentDefinition] = {}
_role_name: dict[str, str] = {}
# Telegram has no per-message text-color API for bots -> emoji is the closest
# real substitute for "color-coding" each speaker. Kept stable per role.
_role_emoji: dict[str, str] = {"Lead": "🧑‍💼"}


def tag(role: str) -> str:
    return f"{_role_emoji.get(role, DEFAULT_EMOJI)} {role}"


# --- hire / fire tools, run in-process, mutate the roster above -----------

@tool(
    "add_teammate",
    "Hire a new teammate role the user asked for (e.g. a documentation writer). "
    "Takes effect starting the next message — say so in your reply.",
    {"role_key": str, "display_name": str, "emoji": str, "description": str, "prompt": str},
)
async def add_teammate(args: dict) -> dict:
    role_key = args["role_key"].strip().lower()
    _team[role_key] = AgentDefinition(
        description=args["description"],
        # every hire gets the same chat-length + ask-one-question rules as the built-in roles
        prompt=args["prompt"] + " " + CHAT_STYLE + " " + ONE_QUESTION_RULE,
        tools=["Read", "Grep", "Glob", "WebSearch", "WebFetch"],
        model="sonnet",
    )
    _role_name[role_key] = args["display_name"]
    _role_emoji[args["display_name"]] = args.get("emoji") or DEFAULT_EMOJI
    _state["pending_reconnect"] = True
    return {"content": [{"type": "text", "text": f"Hired {args['display_name']} ({role_key})."}]}


@tool(
    "remove_teammate",
    "Fire a teammate role the user asked to remove. Takes effect starting the "
    "next message — say so in your reply.",
    {"role_key": str},
)
async def remove_teammate(args: dict) -> dict:
    role_key = args["role_key"].strip().lower()
    if role_key not in _team:
        return {"content": [{"type": "text", "text": f"No such teammate: {role_key}"}], "is_error": True}
    display = _role_name.pop(role_key, role_key.capitalize())
    _role_emoji.pop(display, None)
    del _team[role_key]
    _state["pending_reconnect"] = True
    return {"content": [{"type": "text", "text": f"Fired {display} ({role_key})."}]}


_admin_server = create_sdk_mcp_server(
    name="team-admin", version="1.0.0", tools=[add_teammate, remove_teammate]
)

ROSTER_PROMPT_ADDENDUM = (
    " You start every new session with ZERO teammates hired — no researcher, "
    "coder, reviewer, or anyone else exists until you hire them. This is a "
    "hard rule, not a suggestion: you never answer a task yourself and you "
    "never delegate to a generic/default agent type. For every task, first "
    "check whether an already-hired teammate fits; if not, your very first "
    "tool call this turn MUST be add_teammate for whatever specialist the "
    "task needs, before anything else — including before saying anything "
    "else to the user. Only after that hire lands (next message) do you "
    "delegate to it. Reuse the standard researcher/coder/reviewer trio (with "
    "the duties described above) for coding-shaped tasks, or hire something "
    "more specific if that fits better (e.g. an advisor for a life/finance "
    "decision, a data-analyst for a spreadsheet question) — pick whatever "
    "specialist actually matches the task, don't force it into "
    "researcher/coder/reviewer if it doesn't fit. "
    "Reuse a teammate you've already hired for follow-up tasks instead of "
    "hiring a duplicate. Call add_teammate with a short role_key (lowercase, "
    "no spaces), a display_name, one emoji, a description, and a system "
    "prompt for that role. If asked to remove/fire a teammate, call "
    "remove_teammate with its role_key. Both take effect starting the next "
    "message, not this one — tell the user that, and don't try to delegate "
    "to a role in the same turn you just hired it."
)

# Locks the bot to the first chat that messages it (simple single-user POC guard).
# `client` is kept open across messages so the team remembers prior turns
# (session persistence) instead of starting fresh every message. Reconnecting
# (e.g. after a roster change) resumes the same session_id so context survives.
_state = {"chat_id": None, "client": None, "session_id": None, "pending_reconnect": False}
_turn_lock = asyncio.Lock()  # serialize turns; SDK client handles one query at a time


def _build_options() -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=LEAD_PROMPT + ROSTER_PROMPT_ADDENDUM,
        agents=_team,
        mcp_servers={"team-admin": _admin_server},
        allowed_tools=[
            DELEGATE_TOOL,
            "mcp__team-admin__add_teammate",
            "mcp__team-admin__remove_teammate",
        ],
        permission_mode="acceptEdits",
        cwd=PROJECT_DIR,
        resume=_state["session_id"],
    )


async def get_client() -> ClaudeSDKClient:
    if _state["client"] is None:
        client = ClaudeSDKClient(options=_build_options())
        await client.connect()
        _state["client"] = client
    return _state["client"]


async def reconnect_client() -> None:
    """Drop and rebuild the client (new roster), resuming the same session."""
    old = _state["client"]
    _state["client"] = None
    if old is not None:
        await old.disconnect()
    await get_client()


def _to_telegram_markdown(text: str) -> str:
    """Claude writes GFM (**bold**, # headers, - lists); Telegram's legacy
    Markdown mode only understands single *bold*/_italic_/`code`. Rewrite
    the common bits so replies render instead of showing raw asterisks."""
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text, flags=re.DOTALL)  # **bold** -> *bold*
    text = re.sub(r"^#{1,6}\s*(.+)$", r"*\1*", text, flags=re.MULTILINE)  # # Header -> *Header*
    return text


TELEGRAM_LIMIT = 4096
CHUNK_SIZE = 3500  # margin below the limit for the bold prefix + markdown escaping


def _chunk(text: str, size: int = CHUNK_SIZE) -> list[str]:
    """Split on paragraph/line breaks where possible so code fences and
    sentences don't get cut mid-way more than necessary."""
    if len(text) <= size:
        return [text]
    chunks = []
    while text:
        if len(text) <= size:
            chunks.append(text)
            break
        cut = text.rfind("\n\n", 0, size)
        if cut == -1:
            cut = text.rfind("\n", 0, size)
        if cut == -1:
            cut = size
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


async def send(bot, chat_id: str, role: str, text: str) -> None:
    text = text.strip()
    if not text:
        return
    prefix = tag(role)
    parts = _chunk(text)
    for i, part in enumerate(parts):
        await bot.send_chat_action(chat_id=chat_id, action="typing")
        await asyncio.sleep(0.5)  # feels like someone typing, and avoids flooding
        head = f"*{prefix}:* " if i == 0 else ""  # continuation chunks skip the name
        body = f"{head}{_to_telegram_markdown(part)}"
        try:
            await bot.send_message(chat_id=chat_id, text=body, parse_mode=ParseMode.MARKDOWN)
        except BadRequest:
            # Unbalanced markdown entity (e.g. a stray "*" in code) -> send raw, never drop the message.
            raw_head = f"{prefix}: " if i == 0 else ""
            await bot.send_message(chat_id=chat_id, text=f"{raw_head}{part}")


# Housekeeping text the Agent tool emits, never meant for a user-facing reply
# (normally suppressed by LEAD_PROMPT forbidding run_in_background; filtered
# here too as a defensive fallback in case a background dispatch slips through).
_INTERNAL_MARKERS = ("agentid:", "async agent launched", "output_file:")


def _extract_result_text(content) -> str:
    """A subagent's Agent-tool result is a list of text blocks: its real
    reply, plus internal housekeeping blocks we don't want surfaced in chat."""
    if isinstance(content, str):
        content = [{"text": content}]
    parts = []
    for block in content or []:
        text = block.get("text", "") if isinstance(block, dict) else str(block)
        if any(marker in text.lower() for marker in _INTERNAL_MARKERS):
            continue
        parts.append(text)
    text = "\n".join(p for p in parts if p.strip())
    return text or "(picked up the task, working on it)"


async def run_team(bot, chat_id: str, task: str) -> None:
    client = await get_client()
    pending: dict[str, str] = {}  # tool_use_id -> subagent_type, this turn

    async with _turn_lock:
        await client.query(task)

        async for message in client.receive_response():
            if isinstance(message, AssistantMessage) and message.parent_tool_use_id is None:
                _state["session_id"] = message.session_id or _state["session_id"]
                for block in message.content:
                    if isinstance(block, TextBlock):
                        await send(bot, chat_id, "Lead", block.text)
                    elif isinstance(block, ToolUseBlock) and block.name == DELEGATE_TOOL:
                        role = _role_name.get(block.input.get("subagent_type", ""), "Agent")
                        desc = block.input.get("description", "a subtask")
                        pending[block.id] = role
                        await send(bot, chat_id, "Lead", f"(delegating to {role}) {desc}")

            elif (
                isinstance(message, UserMessage)
                and message.parent_tool_use_id is None
                and isinstance(message.content, list)
            ):
                for block in message.content:
                    if isinstance(block, ToolResultBlock) and block.tool_use_id in pending:
                        role = pending.pop(block.tool_use_id)
                        await send(bot, chat_id, role, _extract_result_text(block.content))

            elif isinstance(message, ResultMessage):
                _state["session_id"] = message.session_id or _state["session_id"]

    if _state["pending_reconnect"]:
        _state["pending_reconnect"] = False
        await reconnect_client()


def _roster_message() -> str:
    lines = [f"*{tag('Lead')}:* this chat is locked to me.", ""]
    lines.append(f"*{tag('Lead')}* — breaks down your task, hires + delegates, synthesizes the final answer.")
    if _team:
        lines.append("")
        lines.append("Current team:")
        for role_key, agent in _team.items():
            lines.append(f"*{tag(_role_name[role_key])}* — {agent.description}")
    else:
        lines.append("No teammates hired yet — I'll hire whoever a task needs as it comes in.")
    lines.append("")
    lines.append("Send me a task to start, or say \"add/fire a <role>\" to shape the team yourself.")
    return "\n".join(lines)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id

    if _state["chat_id"] is None:
        _state["chat_id"] = chat_id
        try:
            await context.bot.send_message(
                chat_id=chat_id, text=_roster_message(), parse_mode=ParseMode.MARKDOWN
            )
        except BadRequest:
            await context.bot.send_message(chat_id=chat_id, text=_roster_message())
    elif chat_id != _state["chat_id"]:
        return  # ignore any other chat (POC guard, not real auth)

    task = update.message.text
    try:
        await run_team(context.bot, chat_id, task)
    except Exception as exc:  # surface errors into the chat instead of dying silently
        await context.bot.send_message(chat_id=chat_id, text=f"*{tag('Lead')}:* (error) {exc}",
                                        parse_mode=ParseMode.MARKDOWN)


async def shutdown_client(app: Application) -> None:
    client = _state["client"]
    if client is not None:
        await client.disconnect()


def main() -> None:
    app = Application.builder().token(TOKEN).post_shutdown(shutdown_client).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot running. Message it on Telegram. Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
