"""
Bridge: Telegram chat <-> claude-agent-sdk team, real peer-to-peer group chat.

Every teammate (including the lead) is its own independent, persistent
ClaudeSDKClient -- not a one-shot Agent-tool delegation. All of them share
one round-robin broadcast: a new message (from the human, or from any
teammate) gets delivered to every other peer in turn; each peer either
posts a real reply or PASSes if it has nothing to add. The lead is a peer
too -- it can speak in the channel like anyone else -- but it alone holds
report_to_human, the tool that actually closes out a discussion and sends
the reply the human sees as "the answer". Free-for-all turn-taking, lead
decides when to wrap up (both by explicit choice per a design discussion) --
the one exception is an internal MAX_MESSAGES crash-guard (not a design
opinion, just there so a stuck discussion can't loop forever on the API
bill).

Multi-user: each Telegram chat gets its own isolated ChatSession keyed by
chat_id -- own roster, own peers, own discussion state.

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

from claude_agent_sdk import tool, create_sdk_mcp_server

from discovery import discover_projects, load_assignments
from peers import AgentPeer, RemotePeer
from prompts import GROUPCHAT_RULES, LEAD_GROUPCHAT_PROMPT, make_peer_prompt as _make_peer_prompt

load_dotenv()

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECTS_DIR = os.environ.get("PROJECTS_DIR", os.path.join(PROJECT_DIR, "projects"))
DEVICES_PATH = os.environ.get("DEVICES_PATH", os.path.join(PROJECT_DIR, "devices.json"))
SHARED_SECRET = os.environ.get("TEAM_SHARED_SECRET")
DEFAULT_EMOJI = "🤖"
LEAD_KEY = "lead"

# Internal crash-guard only -- the design choice was free-for-all turn-taking
# with the lead deciding when to close a discussion, no fixed round cap. This
# is just a floor so a discussion that never converges can't loop forever.
MAX_MESSAGES_PER_DISCUSSION = 40

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


# Prompt-only enforcement of "max 3 sentences per message" wasn't reliably
# followed for information-dense answers (confirmed live: a 700-char,
# 4-point financial breakdown despite the limit). Enforced here instead by
# splitting into multiple short bursts -- like a person sending several
# quick texts in a row -- rather than truncating/losing content. Code
# blocks are kept atomic (never split, don't count toward the sentence cap).
MAX_SENTENCES_PER_BURST = 3
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
# Lookbehind requires a non-digit before the punctuation so "1." / "2." list
# markers (and mid-number periods like "8.2%", already protected by the
# no-whitespace-after check) don't get mistaken for sentence boundaries.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[^\d\s][.!?])\s+(?=[A-Z\"'(])")


def _group_prose(prose: str) -> list[str]:
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(prose) if s.strip()]
    if not sentences:
        return [prose] if prose.strip() else []
    return [
        " ".join(sentences[i:i + MAX_SENTENCES_PER_BURST])
        for i in range(0, len(sentences), MAX_SENTENCES_PER_BURST)
    ]


def _split_into_bursts(text: str) -> list[str]:
    """Alternates prose (grouped into <=3-sentence bursts) and code fences
    (kept whole), preserving order, as a flat list of separate messages."""
    bursts, pos = [], 0
    for m in _CODE_FENCE_RE.finditer(text):
        bursts.extend(_group_prose(text[pos:m.start()]))
        bursts.append(m.group(0))
        pos = m.end()
    bursts.extend(_group_prose(text[pos:]))
    return bursts or [text]


class ChatSession:
    """Everything scoped to one Telegram chat: the live roster of peers
    (lead always present), emoji/name tags, and the round-robin discussion
    engine."""

    def __init__(self, chat_id: int) -> None:
        self.chat_id = chat_id
        self.bot = None  # set on first message (python-telegram-bot's Bot instance)

        self.role_name: dict[str, str] = {LEAD_KEY: "Lead"}
        self.role_desc: dict[str, str] = {LEAD_KEY: "Manager -- coordinates, decides, reports back."}
        self.role_emoji: dict[str, str] = {LEAD_KEY: "🧑‍💼"}
        self.peers: dict[str, AgentPeer] = {}

        # discussion state, reset per human message
        self.wrap_up = False
        self.final_summary: str | None = None
        # role_keys hired but not yet given a turn -- if wrap_up is set
        # while this is non-empty, the close is rejected (see
        # handle_human_message). The prompt tells the lead not to close in
        # the same turn as a hire, but that wasn't reliably followed, so
        # this is enforced structurally instead of hoped for.
        self.newly_hired: set[str] = set()
        # role_keys that came from project auto-discovery, not a manual hire
        # -- lets the rescan drop a peer whose project folder disappeared
        # without ever touching lead or a manually hired teammate.
        self.discovered_keys: set[str] = set()

        self.admin_server = create_sdk_mcp_server(
            name="team-admin", version="1.0.0", tools=self._make_admin_tools()
        )
        self.peers[LEAD_KEY] = AgentPeer(
            role_key=LEAD_KEY,
            system_prompt=LEAD_GROUPCHAT_PROMPT,
            tools=[],  # no Read/Write/Bash/web -- coordinates, doesn't execute
            cwd=PROJECT_DIR,
            mcp_tool_names=[
                "mcp__team-admin__add_teammate",
                "mcp__team-admin__remove_teammate",
                "mcp__team-admin__report_to_human",
            ],
            mcp_servers={"team-admin": self.admin_server},
        )

    def tag(self, role_key: str) -> str:
        name = self.role_name.get(role_key, role_key.capitalize())
        emoji = self.role_emoji.get(role_key, DEFAULT_EMOJI)
        return f"{emoji} {name}"

    async def send(self, role_key: str, text: str) -> None:
        text = text.strip()
        if not text or self.bot is None:
            return
        prefix = self.tag(role_key)
        # Each burst is its own separate Telegram message (multi-bubble chat
        # feel); _chunk() inside is just the char-limit safety net, rarely
        # triggered now that bursts are already short.
        for burst in _split_into_bursts(text):
            for i, part in enumerate(_chunk(burst)):
                await self.bot.send_chat_action(chat_id=self.chat_id, action="typing")
                await asyncio.sleep(0.3)
                head = f"*{prefix}:* " if i == 0 else ""
                body = f"{head}{_to_telegram_markdown(part)}"
                try:
                    await self.bot.send_message(chat_id=self.chat_id, text=body, parse_mode=ParseMode.MARKDOWN)
                except BadRequest:
                    raw_head = f"{prefix}: " if i == 0 else ""
                    await self.bot.send_message(chat_id=self.chat_id, text=f"{raw_head}{part}")

    # --- hire / fire / report_to_human, run in-process, bound to this session ---

    def _make_admin_tools(self) -> list:
        @tool(
            "add_teammate",
            "Hire a new peer into the group chat (e.g. a travel planner, a "
            "finance advisor). Live immediately -- can be addressed the same turn.",
            {"role_key": str, "display_name": str, "emoji": str, "description": str, "prompt": str},
        )
        async def add_teammate(args: dict) -> dict:
            role_key = args["role_key"].strip().lower()
            self.role_name[role_key] = args["display_name"]
            self.role_desc[role_key] = args["description"]
            self.role_emoji[role_key] = args.get("emoji") or DEFAULT_EMOJI
            self.peers[role_key] = AgentPeer(
                role_key=role_key,
                system_prompt=_make_peer_prompt(role_key, args["description"], args["prompt"]),
                tools=["Read", "Grep", "Glob", "WebSearch", "WebFetch"],
                cwd=PROJECT_DIR,
            )
            self.newly_hired.add(role_key)
            return {"content": [{"type": "text", "text": f"Hired {args['display_name']} ({role_key}), live now."}]}

        @tool(
            "remove_teammate",
            "Fire a peer from the group chat. Cannot remove 'lead'.",
            {"role_key": str},
        )
        async def remove_teammate(args: dict) -> dict:
            role_key = args["role_key"].strip().lower()
            if role_key == LEAD_KEY:
                return {"content": [{"type": "text", "text": "Can't fire the lead."}], "is_error": True}
            peer = self.peers.pop(role_key, None)
            if peer is None:
                return {"content": [{"type": "text", "text": f"No such teammate: {role_key}"}], "is_error": True}
            await peer.disconnect()
            display = self.role_name.pop(role_key, role_key.capitalize())
            self.role_desc.pop(role_key, None)
            self.role_emoji.pop(role_key, None)
            return {"content": [{"type": "text", "text": f"Fired {display} ({role_key})."}]}

        @tool(
            "report_to_human",
            "Close out this discussion and send the human your summary -- the "
            "only thing they actually read as 'the answer'. Call this when the "
            "team has covered what's needed.",
            {"summary": str},
        )
        async def report_to_human(args: dict) -> dict:
            self.wrap_up = True
            self.final_summary = args["summary"]
            return {"content": [{"type": "text", "text": "Reported to human, discussion closing."}]}

        return [add_teammate, remove_teammate, report_to_human]

    # --- project auto-discovery --------------------------------------------

    async def _rescan_projects(self) -> None:
        """Pick up new project folders (and drop ones that disappeared)
        before every discussion -- a new project.json becomes usable on the
        very next message, no restart needed. Never touches lead or a
        manually hired teammate, only role_keys this method itself added."""
        manifests = discover_projects(PROJECTS_DIR)
        assignments = load_assignments(DEVICES_PATH)

        for key, manifest in manifests.items():
            if key in self.peers:
                continue  # already live -- don't reconnect mid-conversation
            device_id = assignments.get(key, "local")
            self.role_name[key] = manifest.name
            self.role_desc[key] = manifest.description
            self.role_emoji[key] = manifest.emoji or DEFAULT_EMOJI
            if device_id and device_id != "local":
                # No LAN address stored -- RemotePeer resolves device_id's
                # current IP live via the discovery beacon on every call.
                self.peers[key] = RemotePeer(role_key=key, device_id=device_id, shared_secret=SHARED_SECRET)
            else:
                self.peers[key] = AgentPeer(
                    role_key=key,
                    system_prompt=_make_peer_prompt(key, manifest.description, manifest.prompt),
                    tools=manifest.tools,
                    cwd=manifest.cwd,
                )
            self.discovered_keys.add(key)
            # Same debut-turn guard as a manual hire -- don't let the lead
            # close the discussion before a freshly discovered peer speaks.
            self.newly_hired.add(key)

        gone = self.discovered_keys - set(manifests)
        for key in gone:
            peer = self.peers.pop(key, None)
            if peer is not None:
                await peer.disconnect()
            self.role_name.pop(key, None)
            self.role_desc.pop(key, None)
            self.role_emoji.pop(key, None)
            self.discovered_keys.discard(key)
            self.newly_hired.discard(key)

    # --- round-robin discussion engine -----------------------------------

    async def handle_human_message(self, text: str) -> None:
        self.wrap_up = False
        self.final_summary = None
        self.newly_hired.clear()
        await self._rescan_projects()  # may repopulate newly_hired with fresh discoveries
        transcript: list[tuple[str, str]] = [("human", f"(from the human) {text}")]
        last_seen: dict[str, int] = {}
        posted = 0

        def delta_for(role_key: str) -> str:
            seen = last_seen.get(role_key, 0)
            new = transcript[seen:]
            last_seen[role_key] = len(transcript)
            return "\n".join(f"{self.tag(r)}: {t}" for r, t in new)

        def reject_premature_wrapup() -> None:
            # A hire that hasn't had its debut turn yet was still pending
            # when report_to_human fired -- refuse the close and let the
            # round continue so it actually gets to speak.
            if self.wrap_up and self.newly_hired:
                self.wrap_up = False
                self.final_summary = None

        while posted < MAX_MESSAGES_PER_DISCUSSION and not self.wrap_up:
            round_had_speech = False
            # Lead goes LAST every round, not first. It's always inserted
            # into self.peers before any discovered/hired teammate, so
            # dict order alone would have it speak first every time --
            # confirmed live: lead pre-empted a domain question with its
            # own generic guess, and the actual expert peer (already
            # correct in isolation) then saw the question as "already
            # answered" and deferred instead of restating it. Peers should
            # get first crack; lead reacts/coordinates after hearing them.
            ordered_keys = [k for k in self.peers if k != LEAD_KEY] + (
                [LEAD_KEY] if LEAD_KEY in self.peers else []
            )
            for role_key in ordered_keys:
                if posted >= MAX_MESSAGES_PER_DISCUSSION or self.wrap_up:
                    break
                delta = delta_for(role_key)
                if not delta.strip():
                    continue
                peer = self.peers.get(role_key)
                if peer is None:
                    continue
                self.newly_hired.discard(role_key)  # about to get its debut turn
                reply = await peer.say(delta, on_note=lambda note, rk=role_key: self.send(rk, note))
                reject_premature_wrapup()
                if self.wrap_up:
                    break
                if reply:
                    transcript.append((role_key, reply))
                    posted += 1
                    round_had_speech = True
                    await self.send(role_key, reply)

            if self.wrap_up:
                break

            if not round_had_speech:
                # Everyone passed -- force the lead to explicitly decide
                # instead of looping silently (its own prompt forbids
                # PASSing here, so this is the guaranteed termination path
                # short of the crash-guard).
                lead = self.peers.get(LEAD_KEY)
                delta = delta_for(LEAD_KEY) or "(no new messages)"
                nudge = delta + "\n\n(No one has anything to add. Decide now: call report_to_human, or say what happens next.)"
                reply = await lead.say(nudge, on_note=lambda note: self.send(LEAD_KEY, note)) if lead else None
                reject_premature_wrapup()
                if self.wrap_up:
                    break
                if reply:
                    transcript.append((LEAD_KEY, reply))
                    posted += 1
                    await self.send(LEAD_KEY, reply)
                else:
                    break  # lead had nothing and didn't close it either -- stop safely

        if self.wrap_up and self.final_summary:
            # Backstop for the "lead already said this in the channel, now
            # repeats it in report_to_human" case -- prompt asks it not to,
            # but don't rely on that alone: skip an exact-duplicate resend.
            last_lead_msg = next((t for r, t in reversed(transcript) if r == LEAD_KEY), None)
            is_duplicate = last_lead_msg is not None and (
                last_lead_msg.strip().lower() == self.final_summary.strip().lower()
            )
            if not is_duplicate:
                await self.send(LEAD_KEY, self.final_summary)
        elif posted >= MAX_MESSAGES_PER_DISCUSSION:
            await self.send(LEAD_KEY, "(discussion hit the safety cap without wrapping up -- ask again to continue)")

    async def disconnect(self) -> None:
        for peer in self.peers.values():
            await peer.disconnect()


# One ChatSession per Telegram chat_id -- fully isolated rosters/discussions.
_sessions: dict[int, ChatSession] = {}


def get_session(chat_id: int) -> tuple[ChatSession, bool]:
    """Returns (session, created_now)."""
    session = _sessions.get(chat_id)
    if session is None:
        session = ChatSession(chat_id)
        _sessions[chat_id] = session
        return session, True
    return session, False


def _roster_message(session: ChatSession) -> str:
    lines = [f"*{session.tag('lead')}:* hey, I'm your lead for this chat.", ""]
    lines.append(f"*{session.tag('lead')}* — {session.role_desc['lead']}")
    others = [k for k in session.peers if k != LEAD_KEY]
    if others:
        lines.append("")
        lines.append("Current team:")
        for role_key in others:
            lines.append(f"*{session.tag(role_key)}* — {session.role_desc.get(role_key, '')}")
    else:
        lines.append("No teammates hired yet — I'll hire whoever a task needs as it comes in.")
    lines.append("")
    lines.append("Send me a task to start. Everyone chats live in here -- you'll see the team")
    lines.append("talk it out; my summary is the actual answer.")
    return "\n".join(lines)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    session, created_now = get_session(chat_id)
    session.bot = context.bot

    if created_now:
        await session._rescan_projects()  # so the roster message lists discovered projects too
        try:
            await context.bot.send_message(
                chat_id=chat_id, text=_roster_message(session), parse_mode=ParseMode.MARKDOWN
            )
        except BadRequest:
            await context.bot.send_message(chat_id=chat_id, text=_roster_message(session))

    try:
        await session.handle_human_message(update.message.text)
    except Exception as exc:  # surface errors into the chat instead of dying silently
        await context.bot.send_message(chat_id=chat_id, text=f"*{session.tag('lead')}:* (error) {exc}",
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
