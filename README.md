# Claude SDK Team POC

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

## Multi-device: `backend_server.py` + project auto-discovery

`telegram_team.py` stays the only thing that talks to Telegram (the
"orchestrator") — it still owns every `ChatSession`, the round-robin
transcript, and turn-taking, exactly as above. What's new is where a peer's
turn actually *executes*:

- **`peers.py`** holds `AgentPeer` (today's local `ClaudeSDKClient` peer,
  unchanged) and `RemotePeer` — same `say(incoming, on_note)` interface, but
  it calls another device's `backend_server.py` over HTTP/SSE instead. From
  the round-robin loop's point of view the two are interchangeable; a remote
  peer is just another entry in `ChatSession.peers`.
- **`backend_server.py`** runs on a remote device (a second laptop, a phone
  via Termux). It hosts whichever `AgentPeer`s are assigned to that device
  and exposes `GET /health` / `POST /peer/{role_key}/message` (SSE) for the
  orchestrator to call. Auth is a shared-secret header
  (`TEAM_SHARED_SECRET` env var on both sides) — plain LAN transport, no
  Tailscale/tunnel needed unless a device leaves the home network.
- **An unreachable remote peer is treated as a PASS for that turn** — the
  discussion just continues rather than hanging on one offline device.
  Verified live: killing the backend process mid-test made `RemotePeer.say()`
  return `None` cleanly instead of blocking.

### Project auto-discovery

- **`discovery.py`**: `discover_projects(root)` walks one level under a
  projects directory, reads `project.json` out of any subfolder that has
  one, and returns a manifest per project — folders without a manifest are
  silently skipped. `load_devices(path)` reads `devices.json` (see
  `devices.json.example`) for the device address table and which device
  each project key is assigned to.
- **`projects/<key>/project.json`** is the standard structure — see
  `projects/README.md` for the schema. Deliberately device-agnostic: the
  same manifest file is meant to reach every device identically (e.g. via
  Syncthing mirroring the whole `projects/` root), so device ownership lives
  in the separate `devices.json` instead, not inside a file that's identical
  everywhere.
- **Rescanned before every discussion, not just at startup** —
  `ChatSession._rescan_projects()` runs at the top of
  `handle_human_message()`. Drop a new project folder in, message the bot,
  and it's usable in that same conversation — no restart. A freshly
  discovered project peer gets the same "can't be closed out before its
  debut turn" guard (`newly_hired`) that a manual hire already gets.
- Distributing the actual project folders to every device is a deployment
  concern, not code: point Syncthing (or `git pull`) at the shared
  `projects/` root so laptop2 and both phones end up with the same tree at
  the same relative path.

## Running a backend on an Android phone (Termux)

Two real phones (a OnePlus on Android 15, a Samsung on Android 12) are live
backends today. Getting there needed more than `pip install` — worth
documenting since none of it is obvious going in:

- **Rust-based deps have no Android wheel on PyPI.** `pydantic-core`,
  `rpds-py`, `cryptography` all pip-build from source on Termux, needing
  `pkg install rust` first and `ANDROID_API_LEVEL=<api>` set (35 for
  Android 15, 31 for Android 12 — matches the device's own API level).
  `pkg install python-cryptography` gets a prebuilt `cryptography` on some
  setups; when its own postinstall still falls back to a pip source build,
  it needs the same `ANDROID_API_LEVEL` + `RUSTFLAGS` treatment as below.
- **Extensions built this way fail at import with `dlopen: cannot locate
  symbol "PyExc_Warning"`.** Android's Bionic linker doesn't resolve a
  shared library's undefined symbols against the main executable the way
  Linux does, so a PyO3 extension needs to link `libpython` explicitly:
  `RUSTFLAGS='-C link-arg=-lpython3.XX -C link-arg=-L<path to libpython.so
  dir>'` on the `pip install --no-binary <pkg>` command. Get the exact
  version pip resolves to from whatever's already installed (`pip show
  pydantic` to find the `pydantic-core` version it actually wants) — an
  unpinned rebuild can drift to a newer `pydantic-core` than the installed
  `pydantic` supports and fail at import with a version-mismatch error.
- **Claude Code has no Android build at all** — not an auth issue, a
  packaging one. `claude_agent_sdk` has exactly one transport
  (`subprocess_cli.py`) and always spawns the `claude` binary via
  `shutil.which("claude")`; the npm package's postinstall only has
  binaries for `linux-arm64`/`-musl`, `darwin-*`, `win32-*` — no
  `android` target. Fix: `pkg install proot-distro && proot-distro install
  ubuntu` (a real glibc chroot, reports as plain Linux to npm's platform
  check), install node + `@anthropic-ai/claude-code` *inside* that chroot,
  then put a tiny wrapper script at Termux's own
  `$PREFIX/bin/claude` so `shutil.which("claude")` on the Termux side
  resolves to it:
  ```sh
  #!/data/data/com.termux/files/usr/bin/bash
  exec proot-distro login ubuntu -u claude -- claude "$@"
  ```
  (`backend_server.py`/`peers.py` themselves stay on Termux's native
  Python — only the `claude` binary itself needs to run inside the chroot.)
- **`--dangerously-skip-permissions` (our `bypassPermissions` mode) refuses
  to run as root** — and proot's default identity inside the chroot *is*
  root. Fix: `useradd -m -s /bin/bash claude` inside the chroot once, and
  point the wrapper's `proot-distro login` at `-u claude` (as above) instead
  of the default root login. Each user has its own `~/.claude/` config, so
  this new user needs its own one-time interactive `claude` → `/login`.
- **Android kills backgrounded processes** — `termux-wake-lock` (from the
  `termux-api` package) before any long-running install/server keeps the
  CPU from sleeping mid-build and stops Doze from killing `sshd`/
  `backend_server.py` between messages. Still worth the Termux:Boot addon
  + a battery-optimization exemption for real unattended uptime; a phone
  that gets its screen off for hours can still lose its SSH session even
  with the wake-lock held, and needs Termux reopened by hand to come back
  (confirmed live — one of the two phones dropped mid-session this way).

## Next steps to explore

- Give non-lead peers their own light admin power (e.g. hiring a specialist
  they realize is needed) instead of routing all hires through the lead.
- Persist `ChatSession` state (peers, transcript) across bot restarts —
  currently in-memory only, lost on process restart.
- Tune `MAX_MESSAGES_PER_DISCUSSION` based on real usage once cost patterns
  are visible.
- Termux:Boot + battery-optimization exemption on both phones, so
  `backend_server.py` survives a real device reboot/deep-sleep unattended
  instead of needing Termux reopened by hand.
- Syncthing (or `git pull`) to actually distribute `projects/` to the
  phones — verified today by `scp`, not yet wired up for real.
