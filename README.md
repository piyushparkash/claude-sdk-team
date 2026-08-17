# Claude SDK Team POC

![CI](https://github.com/piyushparkash/claude-sdk-team/actions/workflows/ci.yml/badge.svg)
![License](https://img.shields.io/github/license/piyushparkash/claude-sdk-team)

Two entry points, two different architectures:
- `team.py` — plain CLI, one-shot, fixed 3-role team, lead delegates via
  the built-in `Agent` tool (subagent spawn/dispatch).
- `telegram_team.py` — Telegram bridge, real peer-to-peer group chat: every
  teammate (including the lead) is its own independent, persistent session,
  not a one-shot delegation.

## Setup

```
pip install -r requirements.txt
```

Needs Claude auth (Pro/Max login via `claude auth login`, or `ANTHROPIC_API_KEY`
for pay-per-token API billing instead).

## `team.py` — plain CLI, Agent-tool delegation

```
python team.py "Summarize what this repo does, then write a hello.py, then review it"
```

- `TEAM` dict of `AgentDefinition` (description, prompt, allowed tools, model).
- `ClaudeAgentOptions(agents=TEAM, allowed_tools=["Agent"])` — lead can only
  call `Agent`, forcing delegation instead of doing the work itself. `Agent`
  is this SDK build's Task-delegation tool — despite the name, it's not
  literally called `"Task"`.
- `LEAD_PROMPT` encodes the default pipeline: for any code-producing task,
  coder always writes it and reviewer always checks it, unprompted — research
  is the only optional step. This is a team by default, not something the
  user has to spell out each time.
- `CHAT_STYLE` (hard cap: 3-5 sentences, ~80 words, max 3 bullets, code
  exempt) / `ONE_QUESTION_RULE`: short chat-style replies, one clarifying
  question at a time.
- `permission_mode="bypassPermissions"` at the top level — `acceptEdits`
  only auto-approves file-edit tools; researcher's WebSearch/WebFetch would
  otherwise hit an unanswerable permission prompt (no interactive approver
  exists here) and get silently denied.

Each subagent here is spawned fresh per `Agent` call and doesn't persist or
talk to other subagents directly — the lead is the only line of
communication, one delegation at a time.

## `telegram_team.py` — real peer-to-peer group chat

```
python telegram_team.py
```
Then message your bot on Telegram with a task.

### Architecture: independent peers, not delegation

The `Agent`-tool model above has a hard ceiling: subagents are spawned
fresh per call, can't remember earlier turns, and can't talk to each other
— all coordination has to go through the lead re-delegating with pasted-in
context. `telegram_team.py` doesn't use the `Agent` tool at all. Instead:

- **Every teammate — lead included — is its own independent `ClaudeSDKClient`**
  (`AgentPeer`), connected once and never reconnected, so each one has real
  memory across the whole conversation.
- **A round-robin broadcast** is the only coordination mechanism. A new
  message (from the human, or from any peer) is delivered to every other
  peer in turn; each peer either posts a real reply or replies with the
  literal word `PASS` if it has nothing to add. Peers actually respond to
  *each other* — a reviewer's finding goes straight to the coder, a finance
  advisor's numbers get referenced directly by a travel planner — the lead
  doesn't relay any of it.
- **The lead is a peer, not just a router.** It can speak in the channel
  (or PASS) like anyone else. What makes it the lead is one exclusive tool,
  `report_to_human`: calling it is the only thing that actually closes a
  discussion and sends you a reply. Everything else posted in the channel
  is the team's live working discussion — visible to you, but not the
  answer.
- **Free-for-all turn-taking, lead decides when to wrap up** — no fixed
  round limit, no forced moderator queue. The lead has to *decide* to close
  it out, not just let it go quiet: if a full round-robin pass produces
  zero replies, the lead is forced to speak (its prompt forbids PASSing at
  that exact moment) — either call `report_to_human` or say what happens
  next. This is the guaranteed termination path.
- **One internal crash-guard, not a design cap**: `MAX_MESSAGES_PER_DISCUSSION
  = 40` in `telegram_team.py`. This isn't the "free-for-all" choice being
  walked back — it's a floor so a discussion that somehow never converges
  can't loop forever on the API bill. Should essentially never be hit in
  normal use.
- **Hiring is instant, no reconnect needed.** Since peers are independent
  clients (not entries in a shared `agents=` snapshot baked into one CLI
  session at connect time), a newly hired teammate can be addressed the
  same turn it's hired — the whole "hired but not live until next message"
  problem from the `Agent`-tool model doesn't exist here.
- **"Thinking" one-liners** — a peer's own tool calls
  (Read/Grep/WebSearch/Bash/...) stream out live as short notes —
  `🔍 Researcher: (reading team.py)` — while it works, not just at the end.

### Multi-user

Each Telegram chat gets its own isolated `ChatSession` keyed by `chat_id`:
own roster of peers, own discussion state. Two people can message the bot
independently with zero crossover.

### Other details

- **Hire/fire at runtime** — say "add a travel planner" or "fire the
  reviewer"; the lead calls in-process `add_teammate`/`remove_teammate`
  MCP tools, live immediately. `remove_teammate` refuses to fire the lead.
- **Long replies auto-split** — Telegram's 4096-char limit is handled by
  chunking on paragraph/line breaks; only the first chunk gets the bold
  name prefix.
- **Markdown conversion** — Claude writes GFM (`**bold**`, `# headers`);
  Telegram's legacy Markdown mode only understands single `*bold*`. Rewritten
  before sending, with a plain-text fallback if an entity is unbalanced.
- `permission_mode="bypassPermissions"` per peer (WebSearch/WebFetch/Bash
  would otherwise get silently denied — no interactive approver exists in
  this bridge).
- Token lives in `.env` (`TELEGRAM_BOT_TOKEN`, gitignored, never commit it).
  Any chat that messages the bot gets its own session — no lock/allowlist
  (a POC simplification, not real auth; add one before exposing this beyond
  a couple of trusted chats).

### Superseded: the Agent-tool-based version of this bridge

An earlier version of `telegram_team.py` used the same `Agent`-tool
delegation model as `team.py`, with a single lead client and hire/fire
mutating an `agents=` dict that required a reconnect to take effect. That
approach needed several targeted fixes along the way — worth noting since
they explain *why* the peer-based rewrite happened, not just that it did:

- A `PreToolUse` hook was needed to force `run_in_background=False` on every
  delegation and to deny same-turn hire-then-delegate attempts (the model
  was observed retrying a not-yet-live hire up to ~7 times before giving up).
- A persistent `receive_messages()` pump (instead of a fresh
  `receive_response()` per Telegram message) was needed because
  `receive_response()` stops at each turn's `ResultMessage` — anything
  delivered after that boundary just sat queued until the next message's
  `query()` resumed consumption, which looked exactly like the bot silently
  hanging.
- Dynamic hiring was found to be unreliable — the lead would sometimes skip
  hiring and grab a built-in generic agent instead of a hired teammate, with
  no SDK mechanism found to block that.

The peer-based architecture doesn't have most of these failure modes by
construction: there's no delegation to background, no "hired but not live
yet" window, and no built-in generic agent to fall back to instead of a
real teammate.

## Next steps to explore

- Give non-lead peers their own light admin power (e.g. hiring a specialist
  they realize is needed) instead of routing all hires through the lead.
- Persist `ChatSession` state (peers, transcript) across bot restarts —
  currently in-memory only, lost on process restart.
- Tune `MAX_MESSAGES_PER_DISCUSSION` based on real usage once cost patterns
  are visible.
