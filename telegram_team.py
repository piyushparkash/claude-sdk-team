"""
Bridge: Telegram chat <-> claude-agent-sdk team (lead + on-demand teammates).

Every message is posted with a "Role: text" prefix so a single Telegram
chat reads like a group chat between Lead and teammates. The lead can also
hire/fire teammates at runtime via two in-process tools (add_teammate /
remove_teammate) — the roster change takes effect on the next message, via
a reconnect that resumes the same session so context isn't lost.

Multi-user: each Telegram chat gets its own isolated ChatSession (own
roster, own client/session_id) keyed by chat_id — one user's team, hires,
and conversation history never leak into another's.

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
    HookMatcher,
)

from team import LEAD_PROMPT, CHAT_STYLE, ONE_QUESTION_RULE

load_dotenv()

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DELEGATE_TOOL = "Agent"  # this SDK build's Task-delegation tool is named "Agent"
DEFAULT_EMOJI = "🤖"

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


class ChatSession:
    """Everything scoped to one Telegram chat: its own roster, emoji/name
    tags, live client + session_id, and the hire/fire tools + PreToolUse
    hook bound to *this* session's state (closures, not module globals) so
    two chats never see or affect each other's team."""

    def __init__(self) -> None:
        # Roster starts empty — lead hires whatever a task actually needs.
        self.team: dict[str, AgentDefinition] = {}
        self.role_name: dict[str, str] = {}
        # Telegram has no per-message text-color API for bots -> emoji is
        # the closest real substitute for "color-coding" each speaker.
        self.role_emoji: dict[str, str] = {"Lead": "🧑‍💼"}

        self.client: ClaudeSDKClient | None = None
        self.session_id: str | None = None
        self.pending_reconnect = False
        self.freshly_hired: set[str] = set()  # hired this turn, not yet live in the CLI session
        self.turn_lock = asyncio.Lock()  # serialize this chat's turns

        self.admin_server = create_sdk_mcp_server(
            name="team-admin", version="1.0.0", tools=self._make_admin_tools()
        )

    def tag(self, role: str) -> str:
        return f"{self.role_emoji.get(role, DEFAULT_EMOJI)} {role}"

    # --- hire / fire tools, run in-process, bound to this session's roster ---

    def _make_admin_tools(self) -> list:
        @tool(
            "add_teammate",
            "Hire a new teammate role the user asked for (e.g. a documentation writer). "
            "Takes effect starting the next message — say so in your reply.",
            {"role_key": str, "display_name": str, "emoji": str, "description": str, "prompt": str},
        )
        async def add_teammate(args: dict) -> dict:
            role_key = args["role_key"].strip().lower()
            self.team[role_key] = AgentDefinition(
                description=args["description"],
                # every hire gets the same chat-length + ask-one-question rules as the built-in roles
                prompt=args["prompt"] + " " + CHAT_STYLE + " " + ONE_QUESTION_RULE,
                tools=["Read", "Grep", "Glob", "WebSearch", "WebFetch"],
                model="sonnet",
            )
            self.role_name[role_key] = args["display_name"]
            self.role_emoji[args["display_name"]] = args.get("emoji") or DEFAULT_EMOJI
            self.pending_reconnect = True
            self.freshly_hired.add(role_key)
            return {"content": [{"type": "text", "text": f"Hired {args['display_name']} ({role_key})."}]}

        @tool(
            "remove_teammate",
            "Fire a teammate role the user asked to remove. Takes effect starting the "
            "next message — say so in your reply.",
            {"role_key": str},
        )
        async def remove_teammate(args: dict) -> dict:
            role_key = args["role_key"].strip().lower()
            if role_key not in self.team:
                return {"content": [{"type": "text", "text": f"No such teammate: {role_key}"}], "is_error": True}
            display = self.role_name.pop(role_key, role_key.capitalize())
            self.role_emoji.pop(display, None)
            del self.team[role_key]
            self.pending_reconnect = True
            return {"content": [{"type": "text", "text": f"Fired {display} ({role_key})."}]}

        return [add_teammate, remove_teammate]

    async def _guard_delegation(self, hook_input, tool_use_id, context):
        """PreToolUse hook on the Agent tool — enforces two things the
        prompt only asks nicely for, because both failure modes look like
        "the bot hung" from the Telegram side:

        1. Never background a delegation. If it's ignored, the real result
           only shows up at the start of the *next* message's stream
           (nothing drains it in between) — looks like a silent hang.
        2. Never delegate to a role hired earlier this same turn — the CLI
           session's agent list is a snapshot from connect() time, so a
           same-turn hire isn't live yet. Without this, the model was
           observed retrying the same failing delegation ~7 times before
           giving up, burning a full round-trip each time.
        """
        tool_input = hook_input.get("tool_input", {})

        subagent_type = tool_input.get("subagent_type")
        if subagent_type in self.freshly_hired:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        f"'{subagent_type}' was just hired this turn and isn't live yet "
                        "(takes effect next message). Stop retrying — tell the user "
                        "you're waiting on them to send another message, then stop."
                    ),
                }
            }

        if tool_input.get("run_in_background"):
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "updatedInput": {**tool_input, "run_in_background": False},
                }
            }
        return {}

    def _build_options(self) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            system_prompt=LEAD_PROMPT + ROSTER_PROMPT_ADDENDUM,
            agents=self.team,
            mcp_servers={"team-admin": self.admin_server},
            allowed_tools=[
                DELEGATE_TOOL,
                "mcp__team-admin__add_teammate",
                "mcp__team-admin__remove_teammate",
            ],
            hooks={
                "PreToolUse": [
                    HookMatcher(matcher=DELEGATE_TOOL, hooks=[self._guard_delegation])
                ]
            },
            permission_mode="acceptEdits",
            cwd=PROJECT_DIR,
            resume=self.session_id,
        )

    async def get_client(self) -> ClaudeSDKClient:
        if self.client is None:
            client = ClaudeSDKClient(options=self._build_options())
            await client.connect()
            self.client = client
        return self.client

    async def reconnect_client(self) -> None:
        """Drop and rebuild the client (new roster), resuming the same session."""
        old = self.client
        self.client = None
        if old is not None:
            await old.disconnect()
        await self.get_client()

    async def disconnect(self) -> None:
        if self.client is not None:
            await self.client.disconnect()
            self.client = None


# One ChatSession per Telegram chat_id — fully isolated rosters/sessions.
_sessions: dict[int, ChatSession] = {}


def get_session(chat_id: int) -> tuple[ChatSession, bool]:
    """Returns (session, created_now)."""
    session = _sessions.get(chat_id)
    if session is None:
        session = ChatSession()
        _sessions[chat_id] = session
        return session, True
    return session, False


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


async def send(bot, chat_id: int, session: ChatSession, role: str, text: str) -> None:
    text = text.strip()
    if not text:
        return
    prefix = session.tag(role)
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


async def run_team(bot, chat_id: int, session: ChatSession, task: str) -> None:
    client = await session.get_client()
    pending: dict[str, str] = {}  # tool_use_id -> subagent_type, this turn
    session.freshly_hired.clear()  # any hire from a prior turn is live by now

    async with session.turn_lock:
        await client.query(task)

        async for message in client.receive_response():
            if isinstance(message, AssistantMessage) and message.parent_tool_use_id is None:
                session.session_id = message.session_id or session.session_id
                for block in message.content:
                    if isinstance(block, TextBlock):
                        await send(bot, chat_id, session, "Lead", block.text)
                    elif isinstance(block, ToolUseBlock) and block.name == DELEGATE_TOOL:
                        role = session.role_name.get(block.input.get("subagent_type", ""), "Agent")
                        desc = block.input.get("description", "a subtask")
                        pending[block.id] = role
                        await send(bot, chat_id, session, "Lead", f"(delegating to {role}) {desc}")

            elif (
                isinstance(message, UserMessage)
                and message.parent_tool_use_id is None
                and isinstance(message.content, list)
            ):
                for block in message.content:
                    if isinstance(block, ToolResultBlock) and block.tool_use_id in pending:
                        role = pending.pop(block.tool_use_id)
                        await send(bot, chat_id, session, role, _extract_result_text(block.content))

            elif isinstance(message, ResultMessage):
                session.session_id = message.session_id or session.session_id

    if session.pending_reconnect:
        session.pending_reconnect = False
        await session.reconnect_client()


def _roster_message(session: ChatSession) -> str:
    lines = [f"*{session.tag('Lead')}:* hey, I'm your lead for this chat.", ""]
    lines.append(f"*{session.tag('Lead')}* — breaks down your task, hires + delegates, synthesizes the final answer.")
    if session.team:
        lines.append("")
        lines.append("Current team:")
        for role_key, agent in session.team.items():
            lines.append(f"*{session.tag(session.role_name[role_key])}* — {agent.description}")
    else:
        lines.append("No teammates hired yet — I'll hire whoever a task needs as it comes in.")
    lines.append("")
    lines.append("Send me a task to start, or say \"add/fire a <role>\" to shape the team yourself.")
    return "\n".join(lines)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    session, created_now = get_session(chat_id)

    if created_now:
        try:
            await context.bot.send_message(
                chat_id=chat_id, text=_roster_message(session), parse_mode=ParseMode.MARKDOWN
            )
        except BadRequest:
            await context.bot.send_message(chat_id=chat_id, text=_roster_message(session))

    task = update.message.text
    try:
        await run_team(context.bot, chat_id, session, task)
    except Exception as exc:  # surface errors into the chat instead of dying silently
        await context.bot.send_message(chat_id=chat_id, text=f"*{session.tag('Lead')}:* (error) {exc}",
                                        parse_mode=ParseMode.MARKDOWN)


async def shutdown_client(app: Application) -> None:
    for session in _sessions.values():
        await session.disconnect()


def main() -> None:
    app = Application.builder().token(TOKEN).post_shutdown(shutdown_client).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot running. Message it on Telegram. Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
