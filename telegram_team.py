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
import ctypes
import json
import os
import re
import shlex
import shutil
import sys
import zlib
from difflib import SequenceMatcher

# Windows console defaults to a legacy codepage (cp1252/cp437) for stdout,
# which can't encode emoji like the DEFAULT_EMOJI teammate marker below --
# confirmed live: 'charmap' codec can't encode character '\U0001f916'
# (the robot emoji) crashing any print() of a teammate's name/emoji prefix.
# reconfigure() is a no-op on platforms where stdout is already UTF-8.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    # Redirecting stdout to a log file (nohup ... > telegram_team.log) makes
    # Python switch from line-buffered to fully-buffered -- confirmed live:
    # every print() (including this file's own "[chat N] ===" diagnostics)
    # sat in memory, invisible in the log file, until either the buffer
    # filled or the process exited. line_buffering=True flushes per line
    # instead, so `tail`/live log checks actually reflect what's happening.
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

from dotenv import load_dotenv
from telegram import ReplyParameters, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from claude_agent_sdk import tool, create_sdk_mcp_server

import beacon
import closing_reviewers_store
import scheduler
import teammates_store
from discovery import discover_projects, load_device_config, load_mcp_server
from peers import AgentPeer, RemotePeer, safe_default_allow_antigravity
from prompts import GROUPCHAT_RULES, LEAD_GROUPCHAT_PROMPT, make_peer_prompt as _make_peer_prompt

load_dotenv()

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECTS_DIR = os.environ.get("PROJECTS_DIR", os.path.join(PROJECT_DIR, "projects"))
DEVICES_PATH = os.environ.get("DEVICES_PATH", os.path.join(PROJECT_DIR, "devices.json"))
SHARED_SECRET = os.environ.get("TEAM_SHARED_SECRET")
ALLOWED_CHATS_PATH = os.environ.get("ALLOWED_CHATS_PATH", os.path.join(PROJECT_DIR, "allowed_chats.json"))
# Files the human sends (photo/document/video/audio/voice) get pulled down
# here, one folder per chat, and kept -- never auto-deleted -- so a peer's
# Read tool (absolute path, works regardless of that peer's own cwd) can
# open them any time later in the same chat's history, not just the turn
# they arrived on.
FILES_DIR = os.environ.get("FILES_DIR", os.path.join(PROJECT_DIR, "chat_files"))
DEFAULT_EMOJI = "🤖"
LEAD_KEY = "lead"
# One peer's turn (Claude query()/receive_response(), or an antigravity
# stream()) has no timeout of its own -- confirmed live 2026-08-27, a stuck
# turn hung ~48min with zero log output, indistinguishable from "genuinely
# slow" until manually killed. Generous enough for real slow ops (device
# automation, herdr sessions -- see LONG_RUNNING_OPS in prompts.py) while
# still turning a true hang into a loud, reported failure instead of an
# open-ended silent stall.
TURN_TIMEOUT_SECONDS = float(os.environ.get("TURN_TIMEOUT_SECONDS", "600"))
# How many consecutive rounds lead may be the ONLY real speaker (everyone
# else PASSing) before the loop pauses itself -- confirmed live 2026-08-27,
# lead babysitting a live herdr op posted a new, genuinely different status
# line every round for 20+ rounds straight, each one a full paid API call
# at ~6-8s cadence, nobody else ever contributing. See the loop's own
# comment where this is used.
LEAD_SOLO_ROUND_CAP = int(os.environ.get("LEAD_SOLO_ROUND_CAP", "6"))


def _log(msg: str) -> None:
    """print() with a wall-clock prefix -- confirmed live (2026-08-27) that
    without this, diagnosing "did messages actually get delayed, or did the
    human just not check Telegram during a long stretch of real work" was
    impossible from the log alone: every line has ordering but no way to
    tell how much real time passed between them. Local time (matches how a
    human reads "10:37" against their own clock), HH:MM:SS is enough
    precision for this -- it's for eyeballing gaps, not profiling."""
    from datetime import datetime
    print(f"{datetime.now():%H:%M:%S} {msg}")

# Telegram's Bot API has no text-color markup at all (neither Markdown nor
# HTML parse mode exposes it) -- a colored-circle swatch per role is the
# closest real substitute, stacked in front of each peer's own emoji+name
# so a fast scroll through a busy multi-peer discussion reads at a glance
# instead of everyone blurring into the same "🤖 Name:" shape. Deterministic
# (crc32, not Python's randomized str hash() -- that reseeds every process
# restart and would reshuffle every peer's color on every bot restart) so a
# given role_key always gets the same color across restarts. Lead reserves
# its own fixed swatch (not drawn from the pool) since it's the one voice
# that's always present and should never visually double up with a peer.
_LEAD_SWATCH = "⚪"
_PEER_SWATCHES = ["🔴", "🟠", "🟡", "🟢", "🔵", "🟣", "🟤"]


def _swatch_for(role_key: str) -> str:
    if role_key == LEAD_KEY:
        return _LEAD_SWATCH
    return _PEER_SWATCHES[zlib.crc32(role_key.encode()) % len(_PEER_SWATCHES)]

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
        # concurrent_updates=True lets DIFFERENT chats run in parallel, but
        # this session's own instance state (wrap_up, active_keys, peers
        # dict mutated by tools) is not safe for two overlapping discussions
        # in the SAME chat -- a second message arriving before the first's
        # round-robin finishes would race on that shared state. This lock
        # just serializes handle_human_message within one chat; it does not
        # limit cross-chat concurrency at all.
        self._lock = asyncio.Lock()
        # Telegram message_id this discussion is replying to, so a burst
        # shows up threaded under the message that triggered it -- matters
        # once multiple chats (or a scheduled fire) can be in flight and
        # replies would otherwise be ambiguous about which ask they answer.
        # None for a scheduled fire (no real message to anchor to).
        self._reply_to_message_id: int | None = None

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
        # Which peers are actually part of the CURRENT discussion -- reset
        # to just {lead} at the start of every human message. Broadcasting
        # every message to literally every discovered project peer (Go
        # daemons, ADB scrapers, unrelated pipelines) doesn't scale past a
        # handful of peers: most just burn a full Claude turn to say PASS.
        # Lead sees the human message privately first and invites whoever's
        # actually relevant via invite_peers -- only those get looped in.
        self.active_keys: set[str] = set()
        # role_keys hired via add_teammate (persisted to teammates.json) --
        # distinct from discovered_keys (project auto-discovery) so a
        # restart-restore never gets pruned by the discovered-keys sweep,
        # and remove_teammate knows what to drop from the persisted store.
        self.manually_hired_keys: set[str] = set()
        # Round-robin cursor for auto-placement across devices.json's
        # roster -- advances only past devices that actually got picked
        # (not past ones skipped for being offline), so repeat auto-
        # placements spread out across the roster instead of always
        # retrying from the front.
        self._roster_cursor = 0
        # role_keys that must weigh in before report_to_human is allowed to
        # close -- see closing_reviewers_store and the report_to_human tool
        # below. Loaded once here; set_closing_reviewer updates this set
        # live too, no restart needed to take effect.
        self.closing_reviewer_keys: set[str] = closing_reviewers_store.list_for_chat(chat_id)
        # Which role_keys have actually posted a real (non-PASS) message
        # in the CURRENT discussion -- reset at the top of every human
        # message, alongside active_keys. report_to_human checks this
        # (not the local `transcript` list, which lives inside
        # _handle_human_message_locked and isn't reachable from a tool
        # closure defined once in __init__) to know which closing
        # reviewers still haven't actually spoken yet.
        self._spoken_this_discussion: set[str] = set()

        self.admin_server = create_sdk_mcp_server(
            name="team-admin", version="1.0.0", tools=self._make_admin_tools()
        )
        self.peers[LEAD_KEY] = AgentPeer(
            role_key=LEAD_KEY,
            system_prompt=LEAD_GROUPCHAT_PROMPT,
            tools=[],  # no Read/Write/Bash/web -- coordinates, doesn't execute
            cwd=PROJECT_DIR,
            session_key=f"{self.chat_id}:{LEAD_KEY}",
            # Antigravity has no equivalent hard tool restriction to enforce
            # this -- the lead's zero-tools guarantee only holds under
            # Claude's ClaudeAgentOptions.tools, so it never gets to try
            # antigravity at all, not even as a first attempt.
            allow_antigravity=False,
            mcp_tool_names=[
                "mcp__team-admin__add_teammate",
                "mcp__team-admin__invite_peers",
                "mcp__team-admin__remove_teammate",
                "mcp__team-admin__report_to_human",
                "mcp__team-admin__schedule_task",
                "mcp__team-admin__list_schedules",
                "mcp__team-admin__cancel_schedule",
                "mcp__team-admin__herdr",
                "mcp__team-admin__grant_tool_access",
                "mcp__team-admin__set_closing_reviewer",
                "mcp__team-admin__compact_peer",
                "mcp__team-admin__restart_self",
            ],
            mcp_servers={"team-admin": self.admin_server},
        )

        self._restore_teammates()

    def _restore_teammates(self) -> None:
        """Recreates every manually-hired teammate persisted for this chat
        (see add_teammate) -- runs once per ChatSession, so a bot restart
        (which wipes the old in-memory ChatSession entirely) gets them back
        the moment this chat's first message after restart creates a fresh
        one. AgentPeer's constructor does no I/O (connects lazily on first
        turn), so this is safe to do synchronously here."""
        for entry in teammates_store.load_teammates(self.chat_id):
            role_key = entry["role_key"]
            self.role_name[role_key] = entry["display_name"]
            self.role_desc[role_key] = entry["description"]
            self.role_emoji[role_key] = entry["emoji"]
            restored_tools = ["Read", "Grep", "Glob", "WebSearch", "WebFetch"]
            self.peers[role_key] = AgentPeer(
                role_key=role_key,
                system_prompt=entry["system_prompt"],
                tools=restored_tools,
                cwd=PROJECT_DIR,
                session_key=f"{self.chat_id}:{role_key}",
                # Matches add_teammate's own construction -- deliberately
                # True, see that call site's comment for the full reasoning
                # (decided 2026-08-26: no working per-peer antigravity
                # restriction exists at all, so this is an accepted,
                # conscious grant, made to shift usage off Claude's spend).
                allow_antigravity=True,
            )
            self.manually_hired_keys.add(role_key)
        if self.manually_hired_keys:
            _log(f"[chat {self.chat_id}] restored hired teammates: {sorted(self.manually_hired_keys)}")

    def tag(self, role_key: str) -> str:
        name = self.role_name.get(role_key, role_key.capitalize())
        emoji = self.role_emoji.get(role_key, DEFAULT_EMOJI)
        swatch = _swatch_for(role_key)
        return f"{swatch}{emoji} {name}"

    async def send(self, role_key: str, text: str) -> None:
        text = text.strip()
        if not text or self.bot is None:
            return
        prefix = self.tag(role_key)
        # Each burst is its own separate Telegram message (multi-bubble chat
        # feel); _chunk() inside is just the char-limit safety net, rarely
        # triggered now that bursts are already short.
        reply_params = (
            ReplyParameters(message_id=self._reply_to_message_id, allow_sending_without_reply=True)
            if self._reply_to_message_id is not None else None
        )
        # BUG (confirmed live 2026-08-27): this whole method runs inside
        # ChatSession._lock (called from _handle_human_message_locked,
        # itself inside `async with self._lock:` in handle_human_message),
        # and none of these Telegram calls had a timeout -- a real, this
        # session's recurring httpx.ReadError/NetworkError hiccup landing
        # HERE (as opposed to inside get_updates' own polling loop, which
        # already retries/recovers fine) hung forever, holding the lock
        # for good. Every later message to this chat then reacted 👀 (that
        # happens before the lock is even touched) but never got a real
        # reply -- looked alive, was actually wedged permanently. A
        # timeout here still lets the exception propagate up through the
        # existing `async with self._lock:` (releases it, exception isn't
        # swallowed) into handle_message's own try/except, which reports a
        # real error to the human instead of hanging silently forever.
        for burst in _split_into_bursts(text):
            for i, part in enumerate(_chunk(burst)):
                await asyncio.wait_for(
                    self.bot.send_chat_action(chat_id=self.chat_id, action="typing"), timeout=20.0
                )
                await asyncio.sleep(0.3)
                head = f"*{prefix}:* " if i == 0 else ""
                body = f"{head}{_to_telegram_markdown(part)}"
                try:
                    await asyncio.wait_for(
                        self.bot.send_message(chat_id=self.chat_id, text=body, parse_mode=ParseMode.MARKDOWN,
                                               reply_parameters=reply_params),
                        timeout=20.0,
                    )
                except BadRequest:
                    raw_head = f"{prefix}: " if i == 0 else ""
                    await asyncio.wait_for(
                        self.bot.send_message(chat_id=self.chat_id, text=f"{raw_head}{part}",
                                               reply_parameters=reply_params),
                        timeout=20.0,
                    )

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
            display_name = args["display_name"]
            description = args["description"]
            emoji = args.get("emoji") or DEFAULT_EMOJI
            system_prompt = _make_peer_prompt(role_key, description, args["prompt"])
            self.role_name[role_key] = display_name
            self.role_desc[role_key] = description
            self.role_emoji[role_key] = emoji
            default_tools = ["Read", "Grep", "Glob", "WebSearch", "WebFetch"]
            self.peers[role_key] = AgentPeer(
                role_key=role_key,
                system_prompt=system_prompt,
                tools=default_tools,
                cwd=PROJECT_DIR,
                session_key=f"{self.chat_id}:{role_key}",
                # Deliberately True, not safe_default_allow_antigravity(...)
                # (decided 2026-08-26, reversing an earlier same-day fix):
                # antigravity has no tools= restriction at all (see
                # AgentPeer docstring / peers.safe_default_allow_antigravity),
                # so this IS a conscious grant of full local access to a
                # chat-only persona that was never asked to touch files --
                # accepted deliberately because (a) tested exhaustively and
                # confirmed no per-peer/headless restriction mechanism
                # exists on antigravity's side at all (settings.json is
                # global-only and ignored in headless mode per filed
                # upstream bug #548; hooks.json's PreToolUse is bypassed by
                # a separate hard headless auto-deny gate; Windows-specific
                # permission-matching bugs #614/#742 make even the
                # supposedly-working paths unreliable), and (b) the actual
                # goal is shifting these chat personas' usage off Claude's
                # metered spend entirely onto antigravity's separate quota
                # -- these peers' own job descriptions never call for real
                # file/Bash use, so the realistic risk is low even though
                # the capability is real.
                allow_antigravity=True,
            )
            self.newly_hired.add(role_key)
            self.active_keys.add(role_key)
            self.manually_hired_keys.add(role_key)
            # Persisted so this hire survives a bot restart -- previously a
            # restart silently wiped every manual hire (confirmed live: a
            # schedule_task entry referencing a hired teammate by role_key
            # would fire into a chat where that role_key no longer resolved
            # to anyone). system_prompt is stored fully-composed so restore
            # doesn't depend on make_peer_prompt staying unchanged later.
            teammates_store.save_teammate(self.chat_id, role_key, display_name, emoji, description, system_prompt)
            return {"content": [{"type": "text", "text": f"Hired {args['display_name']} ({role_key}), live now."}]}

        @tool(
            "invite_peers",
            "Loop specific already-known peers (from the roster list you were "
            "given) into THIS discussion -- only invited peers see the "
            "conversation; everyone else stays out of it entirely, so pick "
            "whoever's actually relevant, not everyone. Safe to call more "
            "than once in a discussion if it turns out you need someone else "
            "too. No-op for a role_key that isn't a real known peer.",
            {"role_keys": list},
        )
        async def invite_peers(args: dict) -> dict:
            invited, unknown = [], []
            for role_key in args["role_keys"]:
                role_key = str(role_key).strip().lower()
                if role_key == LEAD_KEY:
                    continue
                if role_key not in self.peers:
                    unknown.append(role_key)
                    continue
                if role_key not in self.active_keys:
                    self.active_keys.add(role_key)
                    self.newly_hired.add(role_key)  # same debut-turn guard as a fresh hire
                invited.append(role_key)
            text = f"Invited: {invited or 'none'}."
            if unknown:
                text += f" Unknown role_keys (ignored): {unknown}."
            _log(f"[chat {self.chat_id}] lead invited: {invited}, unknown: {unknown}")
            return {"content": [{"type": "text", "text": text}]}

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
            self.active_keys.discard(role_key)
            self.manually_hired_keys.discard(role_key)
            teammates_store.remove_teammate(self.chat_id, role_key)
            return {"content": [{"type": "text", "text": f"Fired {display} ({role_key})."}]}

        @tool(
            "report_to_human",
            "Close out this discussion and send the human your summary -- the "
            "only thing they actually read as 'the answer'. Call this when the "
            "team has covered what's needed.",
            {"summary": str},
        )
        async def report_to_human(args: dict) -> dict:
            # Closing reviewers (see closing_reviewers_store /
            # set_closing_reviewer) must actually post a real message in
            # THIS discussion before it's allowed to close, even if the
            # human never explicitly invited them for this particular
            # question -- that's the whole point (e.g. scrutiny should
            # get a real look at every conclusion, not just ones it
            # happened to be invited to). Auto-invite anyone configured
            # but not yet spoken, and refuse to close this turn; the
            # round loop naturally gives them a turn next since they're
            # now in active_keys, so a normal retry of report_to_human
            # once they've weighed in goes through fine.
            unconsulted = {
                rk for rk in self.closing_reviewer_keys
                if rk in self.peers and rk not in self._spoken_this_discussion
            }
            if unconsulted:
                self.active_keys |= unconsulted
                _log(f"[chat {self.chat_id}] === report_to_human blocked, closing reviewers not yet consulted: {sorted(unconsulted)}")
                return {"content": [{"type": "text", "text": (
                    f"Hold off closing -- {', '.join(sorted(unconsulted))} "
                    "haven't weighed in yet and are configured as closing "
                    "reviewers for this chat. Post your proposed conclusion "
                    "as a normal channel message (not report_to_human) so "
                    "they can react to it, wait for their reply, then call "
                    "report_to_human again."
                )}], "is_error": True}
            self.wrap_up = True
            self.final_summary = args["summary"]
            _log(f"[chat {self.chat_id}] === report_to_human: {args['summary'][:300]!r}")
            return {"content": [{"type": "text", "text": "Reported to human, discussion closing."}]}

        @tool(
            "schedule_task",
            "Make a message fire automatically on a recurring schedule in "
            "THIS chat, as if the human had just sent it -- use this when "
            "asked to run/check/trigger something 'every day', 'daily at "
            "8am', 'every Monday', etc, instead of only doing it once. "
            "`cron` is a standard 5-field cron expression you write "
            "yourself in the server's local time (minute hour day month "
            "weekday, e.g. '0 8 * * *' for daily 8:00 AM, '30 21 * * *' "
            "for daily 9:30 PM, '0 9 * * 1' for every Monday 9 AM). "
            "`message` is the exact text that gets delivered as the human's "
            "message when it fires -- write it as a real instruction (e.g. "
            "'trigger today's newsjargon run'), since it goes straight into "
            "a fresh discussion round exactly like a real human message "
            "would. Survives bot restarts.",
            {"cron": str, "message": str},
        )
        async def schedule_task(args: dict) -> dict:
            try:
                entry = scheduler.add_schedule(self.chat_id, args["cron"], args["message"])
            except Exception as exc:
                return {"content": [{"type": "text", "text": f"Bad cron expression: {exc}"}], "is_error": True}
            _log(f"[chat {self.chat_id}] scheduled {entry['id']}: {entry['cron']!r} -> {entry['message']!r}")
            return {"content": [{"type": "text", "text": (
                f"Scheduled (id {entry['id']}): {entry['cron']} -> {entry['message']!r}. "
                f"Next run: {entry['next_run']}."
            )}]}

        @tool(
            "list_schedules",
            "List every recurring schedule currently active in this chat "
            "(id, cron expression, message, next run time).",
            {},
        )
        async def list_schedules(args: dict) -> dict:
            entries = scheduler.list_schedules(self.chat_id)
            if not entries:
                return {"content": [{"type": "text", "text": "No schedules in this chat."}]}
            lines = [
                f"- {e['id']}: {e['cron']} -> {e['message']!r} (next: {e['next_run']})"
                for e in entries
            ]
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        @tool(
            "cancel_schedule",
            "Cancel a recurring schedule in this chat by its id (from "
            "list_schedules).",
            {"schedule_id": str},
        )
        async def cancel_schedule(args: dict) -> dict:
            ok = scheduler.cancel_schedule(self.chat_id, args["schedule_id"].strip())
            text = f"Cancelled {args['schedule_id']}." if ok else f"No schedule with id {args['schedule_id']!r} in this chat."
            return {"content": [{"type": "text", "text": text}], **({} if ok else {"is_error": True})}

        @tool(
            "set_closing_reviewer",
            "Mark peer(s) as required 'closing reviewers' for this chat, or "
            "unmark them. A closing reviewer must actually post a real "
            "message in a discussion before report_to_human is allowed to "
            "close it -- enforced structurally, not just a request -- even "
            "if the human never explicitly invited them for that particular "
            "question. Use this when the human wants someone (e.g. a "
            "critique/QA peer, a memory-keeper) to always get a real look "
            "before anything closes, without needing to manually invite "
            "them every single time. Persists across restarts.",
            {"role_keys": list, "enabled": bool},
        )
        async def set_closing_reviewer(args: dict) -> dict:
            role_keys = [rk.strip().lower() for rk in args["role_keys"]]
            unknown = [rk for rk in role_keys if rk not in self.peers]
            if unknown:
                return {"content": [{"type": "text", "text": f"No such peer(s): {unknown}"}], "is_error": True}
            for rk in role_keys:
                closing_reviewers_store.set_reviewer(self.chat_id, rk, args["enabled"])
                if args["enabled"]:
                    self.closing_reviewer_keys.add(rk)
                else:
                    self.closing_reviewer_keys.discard(rk)
            verb = "now" if args["enabled"] else "no longer"
            return {"content": [{"type": "text", "text": f"{', '.join(role_keys)} {verb} required as closing reviewer(s)."}]}

        @tool(
            "compact_peer",
            "Compact a peer's own conversation memory (like the /compact "
            "command in Claude Code, or Antigravity's own /compact -- "
            "works on both engines, whichever that peer is actually on). "
            "Use this when a peer's own session has been running a long "
            "time across many discussions today and its context is "
            "getting large, including yourself (role_key='lead') if "
            "that's grown large. Not a real conversational turn -- "
            "doesn't post anything to the channel, just housekeeping. "
            "Only compact something you're actually done with for now, "
            "not a peer/yourself you're about to keep iterating with this "
            "same discussion.",
            {"role_key": str},
        )
        async def compact_peer(args: dict) -> dict:
            role_key = args["role_key"].strip().lower()
            peer = self.peers.get(role_key)
            if peer is None:
                return {"content": [{"type": "text", "text": f"No such peer: {role_key}"}], "is_error": True}
            if not isinstance(peer, AgentPeer):
                return {"content": [{"type": "text", "text": f"{role_key} runs on another device -- can't compact it from here yet."}], "is_error": True}
            result = await peer.compact()
            _log(f"[chat {self.chat_id}] === compact_peer {role_key}: {result[:200]!r}")
            return {"content": [{"type": "text", "text": result}]}

        @tool(
            "grant_tool_access",
            "Change what a peer is actually allowed to do -- widen OR "
            "narrow it. Use this when a peer explicitly says it needs a "
            "capability it doesn't have to do something you've judged is "
            "reasonable (e.g. it only has Read/Grep/Glob/WebSearch/"
            "WebFetch and now needs to actually write a file), or to pull "
            "back access no longer warranted. `tools` is the peer's "
            "COMPLETE new tool list (not additive -- include everything it "
            "should still have, from: Read, Write, Edit, Grep, Glob, Bash, "
            "WebSearch, WebFetch). `allow_antigravity` is a SEPARATE, "
            "bigger decision: the local Antigravity engine (agy) has NO "
            "tool-restriction mechanism at all, so granting it is "
            "equivalent to full unrestricted local file/Bash access "
            "regardless of what `tools` says -- only set it True when the "
            "peer genuinely needs that, not just because it asked for one "
            "extra tool. `reason` is a short note for the record (goes "
            "back to the peer and into the log), so grants stay "
            "auditable. Takes effect immediately (reconnects the peer, "
            "its conversation memory survives) but is session-only -- a "
            "bot restart resets a manually-hired peer back to its default "
            "tools, same as everything else about it. Only works on a "
            "peer running on THIS machine -- a peer auto-placed on another "
            "device isn't supported yet. Also how you force a peer BACK "
            "onto Antigravity after it fell back to Claude from a one-off "
            "failure (a peer sticks to whichever engine last worked, no "
            "per-turn re-guessing, by design) -- pass allow_antigravity=True "
            "again (even if it's already True) and this gives it a fresh "
            "shot at Antigravity on its next turn instead of staying stuck.",
            {"role_key": str, "tools": list, "allow_antigravity": bool, "reason": str},
        )
        async def grant_tool_access(args: dict) -> dict:
            role_key = args["role_key"].strip().lower()
            if role_key == LEAD_KEY:
                return {"content": [{"type": "text", "text": "Can't change the lead's own access -- it's fixed at zero tools by design."}], "is_error": True}
            peer = self.peers.get(role_key)
            if peer is None:
                return {"content": [{"type": "text", "text": f"No such peer: {role_key}"}], "is_error": True}
            if not isinstance(peer, AgentPeer):
                return {"content": [{"type": "text", "text": f"{role_key} runs on another device -- can't change its access from here yet."}], "is_error": True}
            known = {"Read", "Write", "Edit", "Grep", "Glob", "Bash", "WebSearch", "WebFetch"}
            bad = [t for t in args["tools"] if t not in known]
            if bad:
                return {"content": [{"type": "text", "text": f"Unknown tool name(s): {bad}. Valid: {sorted(known)}"}], "is_error": True}
            await peer.grant_tools(args["tools"], args["allow_antigravity"])
            _log(f"[chat {self.chat_id}] === grant_tool_access {role_key}: tools={args['tools']} "
                  f"allow_antigravity={args['allow_antigravity']} reason={args['reason']!r}")
            engine_note = " (this now means full unrestricted local access, not just the listed tools)" if args["allow_antigravity"] else ""
            return {"content": [{"type": "text", "text": (
                f"{role_key}'s access is now: {args['tools']}, antigravity engine "
                f"{'allowed' if args['allow_antigravity'] else 'not allowed'}{engine_note}. Reason: {args['reason']}"
            )}]}

        @tool(
            "herdr",
            "Run any Herdr CLI command to inspect or control coding-agent "
            "sessions (Claude Code, Antigravity/agy) open on THIS machine "
            "(laptop1) -- list, start new ones, resume/reattach old ones, "
            "close/kill ones no longer needed, manage the panes/tabs/"
            "workspaces they live in. `command` is everything that goes "
            "after `herdr` itself, e.g. 'agent list', 'agent start --pane "
            "w1:p2 --cmd claude' (or --cmd agy for an Antigravity session "
            "instead -- both engines are just different --cmd values to "
            "the same start command, herdr itself doesn't distinguish), "
            "'agent resume --pane w1:p2 --session-id <id>', 'pane close "
            "--pane w1:p2'. You don't know the exact "
            "flags up front -- discover them the same way the CLI teaches "
            "itself: run a bare group name ('agent', 'pane', 'workspace', "
            "'tab', 'worktree') to see its subcommands, or 'agent --help' / "
            "'pane --help' etc for full syntax, before guessing at a flag. "
            "Always run 'agent list' first to see current cwd/terminal_"
            "title/status before targeting a specific pane -- these are "
            "the human's REAL open coding sessions, not sandboxed test "
            "instances: closing/killing one discards whatever unsaved "
            "context that session had. State plainly which agent (cwd + "
            "title) you're about to close/kill before doing it, don't "
            "guess at a target from a vague human request.",
            {"command": str},
        )
        async def herdr(args: dict) -> dict:
            # Shells out to the real `herdr` CLI directly (already installed
            # and confirmed working live) rather than any third-party MCP
            # wrapper -- the ones found online (e.g. herdr-simple-mcp) were
            # 1-star/single-commit/abandoned, not worth trusting with
            # arbitrary socket access when the official CLI covers this
            # fine on its own. The CLI's own HERDR_ENV gate is a policy
            # check baked into the *skill* prompt for an agent running
            # inside a managed pane, not a technical requirement of the
            # binary itself -- confirmed live it answers from a plain shell
            # with no HERDR_ENV set. Neither shlex mode alone handles a
            # quoted Windows path correctly -- posix=True strips backslashes
            # as escape chars (D:\www\foo becomes D:wwwfoo, confirmed live),
            # posix=False preserves backslashes but leaves the surrounding
            # "..." quote characters IN the token instead of stripping them
            # (herdr would then see a cwd literally starting/ending with a
            # quote char and fail). Split with posix=False for the
            # backslash safety, then strip a single matching pair of quotes
            # off each token by hand.
            try:
                argv = shlex.split(args["command"], posix=False)
                argv = [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'" else t for t in argv]
            except ValueError as exc:
                return {"content": [{"type": "text", "text": f"Couldn't parse that command: {exc}"}], "is_error": True}
            proc = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    "herdr", *argv,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20.0)
            except (FileNotFoundError, asyncio.TimeoutError) as exc:
                # BUG (same class confirmed live 2026-08-28 in
                # antigravity_sdk.py's stream() -- see its comment): a
                # timed-out proc.communicate() never killed the underlying
                # process before, leaving it (and its stdout/stderr pipe
                # handles) abandoned. On Windows, ProactorEventLoop's
                # subprocess-completion waiting has a hard 63-handle cap;
                # enough of these across a day can degrade or stall the
                # SAME event loop's unrelated I/O, including PTB's own
                # get_updates long-poll -- exactly the "process alive, zero
                # logs, zero exceptions" hang seen twice today.
                if proc is not None and proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                return {"content": [{"type": "text", "text": f"Couldn't reach herdr on this machine: {exc}"}], "is_error": True}
            out = stdout.decode(errors="replace").strip()
            err = stderr.decode(errors="replace").strip()
            if proc.returncode != 0:
                return {"content": [{"type": "text", "text": f"herdr {args['command']!r} failed (exit {proc.returncode}): {(err or out)[:1200]}"}], "is_error": True}
            return {"content": [{"type": "text", "text": (out or err or "(no output)")[:3000]}]}

        @tool(
            "restart_self",
            "Restart the ENTIRE bot process (every chat, not just this "
            "one) to pick up fresh code from disk -- use when the human "
            "asks you to restart, e.g. after new code has been deployed. "
            "Every chat's session state (hires, schedules, closing "
            "reviewers, allowed chats) is already persisted to disk on "
            "every change, so none of that is lost; only in-flight/"
            "unsaved per-peer engine conversation state resets (each "
            "peer starts a fresh underlying session on its next turn). "
            "The actual process exit is delayed a few seconds so this "
            "reply reaches the human first; whatever launched this "
            "process (a restart-loop wrapper) is expected to bring it "
            "straight back up -- if it isn't running under one, this "
            "just kills the bot with nothing to restart it.",
            {"reason": str},
        )
        async def restart_self(args: dict) -> dict:
            _log(f"[chat {self.chat_id}] === restart_self requested: {args['reason']!r}")

            async def _delayed_exit() -> None:
                await asyncio.sleep(5.0)
                _log(f"[chat {self.chat_id}] restart_self: exiting now")
                os._exit(0)  # hard exit -- no cleanup needed, every store already writes through on change

            asyncio.create_task(_delayed_exit())
            return {"content": [{"type": "text", "text": (
                f"Restarting now (reason: {args['reason']}) -- back in a few "
                "seconds with fresh code, same chat history."
            )}]}

        tools = [add_teammate, invite_peers, remove_teammate, report_to_human,
                 schedule_task, list_schedules, cancel_schedule,
                 grant_tool_access, set_closing_reviewer, compact_peer, restart_self]
        # herdr controls coding sessions on THIS machine -- only meaningful
        # (and only actually reachable) where the herdr CLI itself is
        # installed. Multi-device posture: the orchestrator can run on a
        # machine that isn't the human's dev box (e.g. ASUS as lead while
        # herdr/coding sessions live on beast), so don't expose a tool
        # that would just fail every call there -- gate on shutil.which
        # instead of hardcoding a device name.
        if shutil.which("herdr"):
            tools.append(herdr)
        return tools

    # --- project auto-discovery --------------------------------------------

    async def _auto_place(self, roster: list[str]) -> str:
        """Round-robin across `roster` (phones-first order, per devices.json),
        skipping any device that doesn't answer the discovery beacon right
        now -- so an offline phone just gets passed over instead of eating
        a placement. Falls back to "local" only if nothing in the roster
        responds. This is what removes the need to hand-edit which project
        goes on which device: any device with Syncthing-synced project
        files can serve any project, so placement is just "which currently-
        live device haven't I used in a while," checked live, not
        hardcoded."""
        if not roster:
            return "local"
        n = len(roster)
        for i in range(n):
            candidate = roster[(self._roster_cursor + i) % n]
            if await beacon.discover(candidate, timeout=1.5):
                self._roster_cursor = (self._roster_cursor + i + 1) % n
                return candidate
        return "local"  # nothing in the roster answered -- laptop as last resort

    async def _rescan_projects(self) -> None:
        """Pick up new project folders (and drop ones that disappeared)
        before every discussion -- a new project.json becomes usable on the
        very next message, no restart needed. Never touches lead or a
        manually hired teammate, only role_keys this method itself added."""
        manifests = discover_projects(PROJECTS_DIR)
        roster, overrides = load_device_config(DEVICES_PATH)

        for key, manifest in manifests.items():
            if key in self.peers:
                continue  # already live -- don't reconnect mid-conversation
            device_id = overrides.get(key) or "local"
            if device_id == "local" and key not in overrides:
                # No explicit pin for this project -- auto-place it instead
                # of defaulting to local (that default only kicks in if
                # nothing in the roster is actually reachable right now).
                device_id = await self._auto_place(roster)
            self.role_name[key] = manifest.name
            self.role_desc[key] = manifest.description
            self.role_emoji[key] = manifest.emoji or DEFAULT_EMOJI
            if device_id and device_id != "local":
                # No LAN address stored -- RemotePeer resolves device_id's
                # current IP live via the discovery beacon on every call.
                self.peers[key] = RemotePeer(role_key=key, device_id=device_id, shared_secret=SHARED_SECRET)
            else:
                mcp_server, mcp_tool_names = load_mcp_server(manifest.cwd, manifest.mcp_entry)
                self.peers[key] = AgentPeer(
                    role_key=key,
                    system_prompt=_make_peer_prompt(key, manifest.description, manifest.prompt),
                    tools=manifest.tools,
                    cwd=manifest.cwd,
                    mcp_servers={manifest.key: mcp_server} if mcp_server else None,
                    mcp_tool_names=mcp_tool_names,
                    session_key=f"{self.chat_id}:{key}",
                    # See safe_default_allow_antigravity -- a project whose
                    # own manifest.tools is read-only shouldn't silently
                    # get full unrestricted access just because antigravity
                    # has no equivalent restriction mechanism.
                    allow_antigravity=safe_default_allow_antigravity(manifest.tools),
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

    async def handle_human_message(self, text: str, reply_to_message_id: int | None = None) -> None:
        async with self._lock:
            await self._handle_human_message_locked(text, reply_to_message_id)

    async def _handle_human_message_locked(self, text: str, reply_to_message_id: int | None) -> None:
        _log(f"[chat {self.chat_id}] === human: {text!r}")
        self._reply_to_message_id = reply_to_message_id
        self.wrap_up = False
        self.final_summary = None
        self.newly_hired.clear()
        self.active_keys = {LEAD_KEY}  # fresh routing every discussion -- see invite_peers
        self._spoken_this_discussion = set()
        await self._rescan_projects()  # may repopulate newly_hired with fresh discoveries

        # Lead can't see who exists unless told -- with routing, peers no
        # longer speak (and thus become visible) until invited, so the
        # roster has to be handed over explicitly on every discussion.
        roster_lines = [
            f"- {key}: {self.role_desc.get(key, '')}"
            for key in self.peers if key != LEAD_KEY
        ]
        roster_block = (
            "(Known peers you can invite with invite_peers -- only invited "
            "peers see this discussion, so pick whoever's actually "
            "relevant:\n" + "\n".join(roster_lines) + ")\n\n"
            if roster_lines else ""
        )
        transcript: list[tuple[str, str]] = [("human", f"(from the human) {text}")]
        last_seen: dict[str, int] = {}
        posted = 0

        def delta_for(role_key: str) -> str:
            seen = last_seen.get(role_key, 0)
            new = transcript[seen:]
            last_seen[role_key] = len(transcript)
            body = "\n".join(f"{self.tag(r)}: {t}" for r, t in new)
            # Roster is only actionable via invite_peers/add_teammate,
            # tools ONLY the lead has (see its mcp_tool_names) -- every
            # other peer was getting the full, ever-growing roster block
            # prefixed onto their first turn of every single discussion for
            # no reason (confirmed live: none of them ever call those
            # tools), pure wasted input tokens repeated across dozens of
            # discussions a day. Prepend it only for the lead, and only on
            # its actual first turn this discussion (seen == 0).
            if role_key == LEAD_KEY and seen == 0 and roster_block:
                body = roster_block + body
            return body

        last_forced_decide_reply: str | None = None

        def reject_premature_wrapup() -> None:
            # A hire that hasn't had its debut turn yet was still pending
            # when report_to_human fired -- refuse the close and let the
            # round continue so it actually gets to speak.
            if self.wrap_up and self.newly_hired:
                self.wrap_up = False
                self.final_summary = None

        consecutive_lead_solo_rounds = 0
        while posted < MAX_MESSAGES_PER_DISCUSSION and not self.wrap_up:
            round_had_speech = False
            non_lead_spoke_this_round = False
            # Lead goes LAST every round, not first. It's always inserted
            # into self.peers before any discovered/hired teammate, so
            # dict order alone would have it speak first every time --
            # confirmed live: lead pre-empted a domain question with its
            # own generic guess, and the actual expert peer (already
            # correct in isolation) then saw the question as "already
            # answered" and deferred instead of restating it. Peers should
            # get first crack; lead reacts/coordinates after hearing them.
            peer_keys = [k for k in self.peers if k != LEAD_KEY and k in self.active_keys]

            async def _run_peer_turn(role_key: str, delta: str):
                peer = self.peers.get(role_key)
                if peer is None:
                    return role_key, None, None
                self.newly_hired.discard(role_key)  # about to get its debut turn
                # Roster block (which keeps growing as peers get added)
                # sits at the FRONT of a first-turn delta, so a head-only
                # truncation increasingly hides the actual message content
                # (confirmed live: with 7 peers the roster alone exceeded a
                # 2000-char head slice, silently cutting off the human's
                # real ask every time). Show head + tail instead so the
                # end of the message -- where the real content usually is --
                # always survives truncation.
                _preview = delta if len(delta) <= 900 else f"{delta[:400]}...<{len(delta) - 900} chars>...{delta[-500:]}"
                _log(f"[chat {self.chat_id}] -> {role_key} (delta: {_preview!r})")
                try:
                    reply = await asyncio.wait_for(
                        peer.say(delta, on_note=lambda note, rk=role_key: self.send(rk, note)),
                        timeout=TURN_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    # Confirmed live 2026-08-27: a peer's own say() (Claude
                    # query()/receive_response() or antigravity stream()) has
                    # no timeout of its own -- unlike self.send()'s Telegram
                    # calls, a network hiccup or wedged CLI process here just
                    # hangs forever with zero further log output, no
                    # exception, nothing distinguishing it from "genuinely
                    # slow" from the log alone. Turning that silent hang into
                    # a loud, timestamped failure that still reports to the
                    # human once via the existing except-Exception path
                    # below, instead of a multi-hour silent stall.
                    _log(f"[chat {self.chat_id}] !!! {role_key} timed out after {TURN_TIMEOUT_SECONDS}s, aborting turn")
                    # BUG (confirmed live 2026-08-27): without this, the
                    # SAME wedged client got reused next turn and hung the
                    # full timeout AGAIN on a completely unrelated message
                    # -- see AgentPeer.reset_connection's docstring.
                    if isinstance(peer, AgentPeer):
                        await peer.reset_connection()
                    return role_key, None, RuntimeError(
                        f"{role_key} timed out after {TURN_TIMEOUT_SECONDS}s with no response"
                    )
                engine = getattr(peer, "_engine", "?")
                _log(f"[chat {self.chat_id}] <- {role_key} [{engine}]: {(reply[:300] if reply else 'PASS')!r}")
                return role_key, reply, None

            # Peers no longer wait behind each other -- confirmed live
            # 2026-09-04: a 24-peer roll-call took ~4 hours serially (each
            # antigravity call taking minutes under backend load), one
            # TURN_TIMEOUT_SECONDS-bounded call at a time with everyone else
            # blocked behind it. Firing every active peer's turn at once
            # instead bounds the whole round by its single slowest peer.
            # Trade-off: peers no longer see each other's replies from
            # WITHIN the same round (delta is a snapshot from before the
            # round started) -- only "peers speak before lead reacts"
            # (below) was ever load-bearing, not peer-to-peer intra-round
            # ordering, so this doesn't change what any peer can act on.
            if peer_keys:
                peer_deltas = {k: delta_for(k) for k in peer_keys}
                fireable = [k for k in peer_keys if peer_deltas[k].strip()]
                results = await asyncio.gather(*(_run_peer_turn(k, peer_deltas[k]) for k in fireable))
                first_error = None
                for role_key, reply, error in results:
                    if error is not None:
                        first_error = first_error or error
                        continue
                    if posted >= MAX_MESSAGES_PER_DISCUSSION or self.wrap_up:
                        continue
                    reject_premature_wrapup()
                    if self.wrap_up:
                        continue
                    if reply:
                        transcript.append((role_key, reply))
                        self._spoken_this_discussion.add(role_key)
                        posted += 1
                        round_had_speech = True
                        non_lead_spoke_this_round = True
                        await self.send(role_key, reply)
                # Surface the first timeout to the human same as before
                # (raising out of run() into handle_message's except-Exception
                # handler) only after every other concurrently-fired peer's
                # result has already been processed above -- one wedged peer
                # no longer silently swallows everyone else's replies too.
                if first_error is not None:
                    raise first_error

            if LEAD_KEY in self.peers and posted < MAX_MESSAGES_PER_DISCUSSION and not self.wrap_up:
                lead_delta = delta_for(LEAD_KEY)
                if lead_delta.strip():
                    role_key, reply, error = await _run_peer_turn(LEAD_KEY, lead_delta)
                    if error is not None:
                        raise error
                    reject_premature_wrapup()
                    if not self.wrap_up and reply:
                        transcript.append((role_key, reply))
                        self._spoken_this_discussion.add(role_key)
                        posted += 1
                        round_had_speech = True
                        await self.send(role_key, reply)

            if self.wrap_up:
                break

            # BUG (confirmed live 2026-08-27): lead babysitting a live
            # herdr op (SSH router debug, a build, etc) posted a genuinely
            # DIFFERENT status line every single round -- "gateway
            # reachable", "iw command missing", "3927 retries on wlan0" --
            # so the fuzzy-duplicate guard below (which only catches
            # REPEATED content) never triggers, yet each round is still a
            # full paid API call at ~6-8s cadence, every peer re-invoked
            # just to PASS, for as long as lead keeps finding something new
            # to report. The actual problem isn't duplicate content, it's
            # unthrottled cadence -- cap how many rounds in a row lead can
            # be the ONLY real speaker before pausing and letting the human
            # check back in, rather than self-triggering forever.
            if round_had_speech and not non_lead_spoke_this_round:
                consecutive_lead_solo_rounds += 1
                if consecutive_lead_solo_rounds >= LEAD_SOLO_ROUND_CAP:
                    note = (
                        f"(Paused after {LEAD_SOLO_ROUND_CAP} rounds of solo updates to avoid "
                        "spamming -- still working in the background, message me to check back in.)"
                    )
                    _log(f"[chat {self.chat_id}] lead solo-spoke {consecutive_lead_solo_rounds} rounds in a row -- pausing loop")
                    transcript.append((LEAD_KEY, note))
                    await self.send(LEAD_KEY, note)
                    break
            elif round_had_speech:
                consecutive_lead_solo_rounds = 0

            if not round_had_speech and self.newly_hired:
                # A peer was just hired/invited this round and hasn't had
                # its own turn yet -- ordered_keys is a snapshot taken at
                # the TOP of this round, before lead's own invite_peers
                # call updated active_keys, so the new peer simply wasn't
                # in this round's lineup. That's not "everyone passed" --
                # it's "the new hire hasn't spoken yet." Confirmed live:
                # without this check, this exact situation fell into the
                # force-lead-to-decide-NOW branch below, and lead (under
                # pressure to decide immediately) closed the discussion via
                # report_to_human right after inviting the Blinkit scraper
                # specialist -- it never got a turn, no scrape ever ran, and
                # the human got a hollow "waiting on their check" reply.
                # Just let the while loop continue -- next round's
                # ordered_keys recompute will correctly include them.
                continue

            if not round_had_speech:
                # Everyone passed -- force the lead to explicitly decide
                # instead of looping silently (its own prompt forbids
                # PASSing here, so this is the guaranteed termination path
                # short of the crash-guard).
                lead = self.peers.get(LEAD_KEY)
                delta = delta_for(LEAD_KEY) or "(no new messages)"
                nudge = delta + "\n\n(No one has anything to add. Decide now: call report_to_human, or say what happens next.)"
                if lead is None:
                    reply = None
                else:
                    try:
                        reply = await asyncio.wait_for(
                            lead.say(nudge, on_note=lambda note: self.send(LEAD_KEY, note)),
                            timeout=TURN_TIMEOUT_SECONDS,
                        )
                    except asyncio.TimeoutError:
                        _log(f"[chat {self.chat_id}] !!! lead timed out after {TURN_TIMEOUT_SECONDS}s (forced-decide nudge), aborting turn")
                        if isinstance(lead, AgentPeer):
                            await lead.reset_connection()
                        raise RuntimeError(f"lead timed out after {TURN_TIMEOUT_SECONDS}s with no response")
                reject_premature_wrapup()
                if self.wrap_up:
                    break
                if reply:
                    # BUG (confirmed live 2026-08-27): this branch's own
                    # prompt forbids PASS, so when lead is genuinely just
                    # waiting on something long-running (a herdr build,
                    # etc) with nothing new to say, it posts filler instead
                    # ("Build 1m19s, still churning") -- which counts as
                    # real speech, reopens another full round immediately,
                    # everyone else PASSes again, lead gets force-nudged
                    # again, and it repeats every ~8s (one full paid LLM
                    # call each time) for minutes on end -- 17-18 cycles
                    # seen live, zero actual progress made. Near-duplicate
                    # consecutive forced-decide replies (same fuzzy-match
                    # threshold as report_to_human's own dedup guard) mean
                    # "still waiting, nothing changed" -- stop spinning and
                    # let the human's next message resume it, instead of
                    # burning rounds on repeated status pings nobody asked
                    # for every few seconds.
                    if last_forced_decide_reply is not None and \
                            SequenceMatcher(None, last_forced_decide_reply, reply).ratio() > 0.7:
                        _log(f"[chat {self.chat_id}] forced-decide reply near-duplicate of last -- stopping instead of spinning: {reply[:200]!r}")
                        transcript.append((LEAD_KEY, reply))
                        await self.send(LEAD_KEY, reply)
                        break
                    last_forced_decide_reply = reply
                    transcript.append((LEAD_KEY, reply))
                    posted += 1
                    await self.send(LEAD_KEY, reply)
                else:
                    break  # lead had nothing and didn't close it either -- stop safely

        if self.wrap_up and self.final_summary:
            # Backstop for "someone already said this in the channel, now
            # report_to_human repeats it" -- prompt asks the lead not to,
            # but don't rely on that alone. Two things confirmed live
            # (2026-08-26): (1) every channel message, not just the final
            # summary, already gets sent to the human via self.send() at
            # line ~614 -- the human watches the whole discussion live, so
            # a peer's finding is never invisible to them. (2) the old
            # check only compared against the LEAD's own last message, and
            # only on an EXACT string match -- a peer's conclusion getting
            # paraphrased (not verbatim-repeated) by the lead sailed right
            # past it, producing two near-identical bubbles back to back.
            # Fuzzy-compare against the very last channel message overall
            # (any role) instead of an exact lead-only match.
            last_msg = transcript[-1][1] if transcript else None
            is_duplicate = last_msg is not None and (
                SequenceMatcher(None, last_msg.strip().lower(), self.final_summary.strip().lower()).ratio() > 0.7
            )
            if not is_duplicate:
                await self.send(LEAD_KEY, self.final_summary)
        elif posted >= MAX_MESSAGES_PER_DISCUSSION:
            await self.send(LEAD_KEY, "(discussion hit the safety cap without wrapping up -- ask again to continue)")

    async def disconnect(self) -> None:
        for peer in self.peers.values():
            await peer.disconnect()


def _is_allowed_chat(chat_id: int) -> bool:
    """Gate against a real gap confirmed live 2026-08-26: handle_message/
    handle_file_message previously took update.effective_chat.id with NO
    authorization check at all -- literally any stranger who found this
    bot's Telegram username got their own fresh ChatSession, and
    _rescan_projects() discovers the SAME global projects/ folder for
    them too (full roster, scraper data, and now the herdr tool -- real
    control over Claude Code sessions on this machine). Re-reads the file
    on every call (not cached) so adding a family member's chat_id takes
    effect on their very next message, no bot restart needed -- same
    pattern as devices.json/schedules.json being re-read live elsewhere
    in this file. Fails closed: a missing/corrupt file means nobody's
    allowed, not everybody."""
    try:
        with open(ALLOWED_CHATS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return chat_id in {entry["chat_id"] for entry in data.get("allowed", [])}
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return False


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


async def _react_eyes(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> None:
    """Reacts 👀 on the human's message the moment it's accepted -- a
    discussion can take a while (real API calls, a full round-robin) with
    no other feedback until the final reply, so there was previously no
    sign the bot had even seen the message at all in that gap. Best-effort:
    a reaction failing (message too old, deleted, etc) shouldn't block
    actually processing the message."""
    try:
        await asyncio.wait_for(
            context.bot.set_message_reaction(chat_id=chat_id, message_id=message_id, reaction="👀"),
            timeout=10.0,
        )
    except Exception:
        pass


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not _is_allowed_chat(chat_id):
        return  # unknown chat -- silently ignore, don't reveal the bot exists or is listening
    await _react_eyes(context, chat_id, update.message.message_id)
    session, created_now = get_session(chat_id)
    session.bot = context.bot

    if created_now:
        await session._rescan_projects()  # so the roster message lists discovered projects too
        roster_text = _roster_message(session)
        if len(roster_text) > 4000:
            roster_text = roster_text[:3990] + "\n... (roster truncated, too long for one message)"
        try:
            await context.bot.send_message(
                chat_id=chat_id, text=roster_text, parse_mode=ParseMode.MARKDOWN
            )
        except BadRequest:
            try:
                await context.bot.send_message(chat_id=chat_id, text=roster_text)
            except BadRequest:
                pass  # roster still too long even truncated -- don't block the reply below

    try:
        await session.handle_human_message(update.message.text, reply_to_message_id=update.message.message_id)
    except Exception as exc:  # surface errors into the chat instead of dying silently
        await context.bot.send_message(chat_id=chat_id, text=f"*{session.tag('lead')}:* (error) {exc}",
                                        parse_mode=ParseMode.MARKDOWN)


async def handle_file_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Photo/document/video/audio/voice messages -- previously invisible to
    the bot entirely (only filters.TEXT was registered, so a sent image or
    PDF got silently dropped, no roster reply, no error, nothing). Downloads
    the highest-resolution/original file to FILES_DIR, then feeds a
    synthetic human message describing it (path + caption) through the same
    handle_human_message round-robin a real text message gets, so peers can
    Read it if it's relevant to the discussion."""
    chat_id = update.effective_chat.id
    if not _is_allowed_chat(chat_id):
        return  # unknown chat -- silently ignore, same gate as handle_message
    await _react_eyes(context, chat_id, update.message.message_id)
    session, created_now = get_session(chat_id)
    session.bot = context.bot

    if created_now:
        await session._rescan_projects()
        roster_text = _roster_message(session)
        if len(roster_text) > 4000:
            roster_text = roster_text[:3990] + "\n... (roster truncated, too long for one message)"
        try:
            await context.bot.send_message(
                chat_id=chat_id, text=roster_text, parse_mode=ParseMode.MARKDOWN
            )
        except BadRequest:
            try:
                await context.bot.send_message(chat_id=chat_id, text=roster_text)
            except BadRequest:
                pass  # roster still too long even truncated -- don't block the reply below

    msg = update.message
    if msg.photo:
        tg_file = await msg.photo[-1].get_file()  # last = highest resolution
        suggested_name = f"photo_{tg_file.file_unique_id}.jpg"
        kind, folder = "photo", "photos"
    elif msg.document:
        tg_file = await msg.document.get_file()
        suggested_name = msg.document.file_name or f"file_{tg_file.file_unique_id}"
        kind, folder = "document", "documents"
    elif msg.video:
        tg_file = await msg.video.get_file()
        suggested_name = msg.video.file_name or f"video_{tg_file.file_unique_id}.mp4"
        kind, folder = "video", "video"
    elif msg.audio or msg.voice:
        media = msg.audio or msg.voice
        tg_file = await media.get_file()
        suggested_name = getattr(media, "file_name", None) or f"audio_{tg_file.file_unique_id}.ogg"
        kind, folder = "audio", "audio"
    else:
        return  # unhandled media type -- nothing to download

    # chat_files/<chat_id>/<kind>/<YYYY-MM-DD>/<name> -- kind and date
    # subfolders so a chat's files stay browsable (find all PDFs, or
    # everything from a given day) instead of one flat dump per chat.
    date_str = msg.date.strftime("%Y-%m-%d") if msg.date else "unknown-date"
    chat_dir = os.path.join(FILES_DIR, str(chat_id), folder, date_str)
    os.makedirs(chat_dir, exist_ok=True)
    dest_path = os.path.join(chat_dir, suggested_name)
    # A same-named file already on disk (re-send, or two files sharing a
    # name) shouldn't clobber what's there -- number it instead of overwriting.
    if os.path.exists(dest_path):
        base, ext = os.path.splitext(dest_path)
        n = 2
        while os.path.exists(f"{base}_{n}{ext}"):
            n += 1
        dest_path = f"{base}_{n}{ext}"

    try:
        await tg_file.download_to_drive(dest_path)
    except Exception as exc:
        await context.bot.send_message(chat_id=chat_id, text=f"*{session.tag('lead')}:* (error) couldn't download that {kind}: {exc}",
                                        parse_mode=ParseMode.MARKDOWN)
        return

    caption = msg.caption or ""
    # The file only lands on THIS machine's disk (wherever telegram_team.py
    # itself runs). A peer auto-placed on another device (RemotePeer -- its
    # turn actually executes on that device's own backend_server.py, see
    # peers.py) has no way to reach a local path here at all, Read tool or
    # not -- confirmed live, lead + two RemotePeer teammates all reported
    # "can't view it" for exactly this reason. Naming which peers can
    # actually open it (AgentPeer = runs right here) upfront saves everyone
    # a wasted round of "I can't read that" replies.
    local_capable = [k for k, p in session.peers.items() if isinstance(p, AgentPeer) and "Read" in getattr(p, "tools", [])]
    if local_capable:
        access_note = f"only these peers run on this machine and can Read it: {', '.join(local_capable)}"
    else:
        access_note = "no current peer runs on this machine with Read access -- none of them can open it"
    text = (
        f"(the human sent a {kind}, saved to `{dest_path}` on this machine"
        + (f", caption: {caption!r}" if caption else "")
        + f". {access_note}.) Read it if you're one of those peers and it's relevant to the discussion; "
        "otherwise say so rather than guessing at its contents."
    )
    try:
        await session.handle_human_message(text, reply_to_message_id=msg.message_id)
    except Exception as exc:
        await context.bot.send_message(chat_id=chat_id, text=f"*{session.tag('lead')}:* (error) {exc}",
                                        parse_mode=ParseMode.MARKDOWN)


async def shutdown_client(app: Application) -> None:
    for session in _sessions.values():
        await session.disconnect()


SCHEDULER_POLL_SECONDS = 60


async def run_scheduler_loop(app: Application) -> None:
    """Polls schedules.json once a minute and fires any entry that's due,
    injecting its message into that chat exactly like a real human message
    (full round-robin discussion, real reply posted back to Telegram).
    Runs for the life of the process -- started from post_init so it has a
    running event loop and a ready app.bot to hand sessions."""
    while True:
        try:
            for entry in scheduler.due_schedules():
                chat_id = entry["chat_id"]
                if not _is_allowed_chat(chat_id):
                    # Defense in depth: a chat_id removed from the allowlist
                    # after it created a schedule shouldn't keep firing --
                    # due_schedules() already advanced next_run as a side
                    # effect of returning this entry, so skipping the fire
                    # here just means it won't run, not that it'll retry.
                    _log(f"[chat {chat_id}] scheduled fire {entry['id']} skipped -- chat not on allowlist")
                    continue
                session, _ = get_session(chat_id)
                session.bot = app.bot
                _log(f"[chat {chat_id}] === scheduled fire {entry['id']}: {entry['message']!r}")
                try:
                    await session.handle_human_message(
                        "(This is an automated recurring trigger you scheduled earlier, not "
                        f"a live human message -- treat it the same as if they'd sent it right "
                        f"now.) {entry['message']}"
                    )
                except Exception as exc:
                    _log(f"[chat {chat_id}] scheduled fire {entry['id']} failed: {exc}")
        except Exception as exc:
            print(f"[scheduler] poll failed: {exc}")
        await asyncio.sleep(SCHEDULER_POLL_SECONDS)


def _handle_count() -> int | None:
    """Current process's open Windows handle count, via the same Win32 API
    Task Manager's 'Handles' column reads -- no psutil dependency needed
    for one counter. Returns None off-Windows or on any API failure.

    BUG (confirmed live 2026-08-29): this silently returned None on EVERY
    call for the first 14.5h of uptime -- zero '[diag]' lines ever logged,
    not even one, despite no exception either (a genuinely dead-silent
    failure, ironically the same flavor of bug this watcher exists to
    catch). Root cause: GetCurrentProcess() returns a 64-bit pseudo-handle
    (-1, i.e. 0xFFFFFFFFFFFFFFFF), but ctypes defaults an undeclared
    function's return type to c_int (32-bit signed) -- confirmed live,
    GetProcessHandleCount(that truncated value) then just fails (returns
    0/false) instead of raising, so the `if` guard below silently skipped
    the return. Declaring both functions' real Win32 signatures
    (HANDLE/void* + LPDWORD) makes ctypes marshal the handle correctly."""
    try:
        ctypes.windll.kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        ctypes.windll.kernel32.GetProcessHandleCount.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        h = ctypes.windll.kernel32.GetCurrentProcess()
        count = ctypes.c_ulong(0)
        if ctypes.windll.kernel32.GetProcessHandleCount(h, ctypes.byref(count)):
            return count.value
    except Exception:
        pass
    return None


HANDLE_LOG_INTERVAL_SECONDS = 600


async def run_handle_watch_loop() -> None:
    """Logs this process's own handle count periodically -- pure
    diagnostics, no action taken. Confirmed live 2026-08-28: the bot's
    Telegram polling went silently dead twice in one day (process alive,
    zero CPU, zero exceptions, zero new log lines) right after a day of
    subprocess turns that got cancelled by TURN_TIMEOUT_SECONDS without
    the underlying `agy`/`herdr` child process ever being killed (see the
    try/finally added to antigravity_sdk.py's stream() and the herdr tool
    in this file). Windows' ProactorEventLoop caps concurrent
    WaitForMultipleObjects wait handles at 63 -- enough leaked subprocess
    handles could plausibly stall the SAME event loop's unrelated I/O,
    including PTB's own get_updates long-poll. Both leaks are now fixed,
    but this stays running so a rising handle count over the day would be
    hard, direct evidence either confirming that theory (if the freeze
    recurs alongside a climbing count) or ruling it out (if the count
    stays flat and it still recurs) -- better than guessing again."""
    while True:
        count = _handle_count()
        if count is not None:
            _log(f"[diag] process handle count: {count}")
        await asyncio.sleep(HANDLE_LOG_INTERVAL_SECONDS)


async def _start_scheduler_loop(app: Application) -> None:
    asyncio.create_task(run_scheduler_loop(app))
    asyncio.create_task(run_handle_watch_loop())


async def _on_error(update: object, context) -> None:
    # No error handler was registered before this -- python-telegram-bot's
    # own network_retry_loop just dumped a raw traceback to stderr on every
    # Telegram long-poll hiccup (httpx.ReadError etc.) and silently resumed
    # once it recovered, with no timestamped line marking either the drop or
    # the recovery. This gives every such hiccup one clean, greppable line
    # instead of a wall of traceback or total silence.
    err = context.error
    _log(f"[polling] network hiccup, retrying: {err.__class__.__name__}: {err}")


def main() -> None:
    app = (
        Application.builder()
        .token(TOKEN)
        # Default is serial processing -- one update at a time, globally,
        # across every chat. Confirmed live: a single multi-minute
        # discussion (newsjargon's render-wait loop) blocked every other
        # chat from getting any response until it wrapped up. This lets
        # different chats run their round-robins in parallel; ChatSession's
        # own lock still serializes messages within the SAME chat, where
        # overlapping discussions would race on shared instance state.
        .concurrent_updates(True)
        .post_init(_start_scheduler_loop)
        .post_shutdown(shutdown_client)
        .build()
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.Document.ALL | filters.VIDEO | filters.AUDIO | filters.VOICE,
        handle_file_message,
    ))
    app.add_error_handler(_on_error)
    print("Bot running. Message it on Telegram. Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
