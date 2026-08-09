# Claude SDK Team POC

Lead agent delegates to teammates via `claude_agent_sdk.AgentDefinition` and
the built-in `Agent` tool (this SDK build's delegation tool — despite the
name, it's not literally called `"Task"`). No manual routing code; the SDK
handles spawn/dispatch.

Two entry points:
- `team.py` — plain CLI, one-shot, fixed 3-role team (researcher/coder/reviewer).
- `telegram_team.py` — Telegram bridge, persistent session, dynamic hire/fire.

## Setup

```
pip install -r requirements.txt
```

Needs Claude auth (Pro/Max login via `claude auth login`, or `ANTHROPIC_API_KEY`
for pay-per-token API billing instead).

## `team.py` — plain CLI

```
python team.py "Summarize what this repo does, then write a hello.py, then review it"
```

- `TEAM` dict of `AgentDefinition` (description, prompt, allowed tools, model).
- `ClaudeAgentOptions(agents=TEAM, allowed_tools=["Agent"])` — lead can only
  call `Agent`, forcing delegation instead of doing the work itself.
- `LEAD_PROMPT` encodes the default pipeline: for any code-producing task,
  coder always writes it and reviewer always checks it, unprompted — research
  is the only optional step. This is a team by default, not something the
  user has to spell out each time.
- `CHAT_STYLE` / `ONE_QUESTION_RULE`: every role writes short chat-style
  replies, and if a teammate needs input, it asks one question and stops
  rather than dumping a list.

## `telegram_team.py` — Telegram bridge

Relays the same mechanics into a Telegram chat, one message per role
(bold, emoji-tagged: **🧑‍💼 Lead:**, **🔍 Researcher:**, etc.), so a single
1:1 chat reads like a group chat.

Run:
```
python telegram_team.py
```
Then message your bot on Telegram with a task.

Key differences from `team.py`:
- **Session persistence** — one `ClaudeSDKClient` stays connected across all
  messages in the chat (not reconnected per message), so the team remembers
  earlier turns. Context auto-compacts same as interactive Claude Code; no
  config needed.
- **Empty roster at start** — no researcher/coder/reviewer exists until the
  lead hires them. For every task the lead is instructed to hire whichever
  specialist actually fits (the usual coder/reviewer pair for code, or
  something task-specific like an "advisor" for a non-coding question)
  before delegating.
- **Hire/fire at runtime** — say "add a documentation guy" or "fire the
  reviewer"; lead calls in-process `add_teammate`/`remove_teammate` MCP
  tools (`mcp__team-admin__...`), which mutate the roster and trigger a
  reconnect that **resumes the same session** so context survives. Takes
  effect starting the next message, not the one that requested it.
- **Long replies auto-split** — Telegram's 4096-char limit is handled by
  chunking on paragraph/line breaks; only the first chunk gets the bold
  name prefix.
- **Markdown conversion** — Claude writes GFM (`**bold**`, `# headers`);
  Telegram's legacy Markdown mode only understands single `*bold*`. Rewritten
  before sending, with a plain-text fallback if an entity is unbalanced.
- Token lives in `.env` (`TELEGRAM_BOT_TOKEN`, gitignored, never commit it).
  First message locks the bot to that chat (a POC guard, not real auth).

### Known limitation: dynamic hiring isn't 100% reliable

The "hire whoever fits before delegating" rule is prompt-engineered, not
enforced by the SDK — there's no mechanism found so far to *block* the
lead from calling a built-in default agent type instead of a hired
teammate. In testing this works most of the time (verified: an
underspecified finance question correctly triggers hiring an "advisor" and
asking one clarifying question), but occasionally, especially for
coding-shaped tasks, the lead skips hiring and grabs a built-in generic
agent directly, bypassing the roster. Accepted trade-off for staying fully
dynamic; if it becomes a problem, pre-seeding coder+reviewer at startup
(keeping the rest hire-on-demand) is the fallback discussed but not applied.

## Next steps to explore

- Investigate whether the SDK can restrict the `Agent` tool's subagent_type
  enum to only currently-hired teammates (would fully close the gap above).
- Add MCP tools per role beyond the built-ins (Read/Grep/Bash/WebSearch/...).
- Try `permissionMode` per-agent (e.g. reviewer as `plan`-only) in `AgentDefinition`.
- Log full `receive_response()` stream to see subagent-level `SubagentStart`/`SubagentStop` hooks.
