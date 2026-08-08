# Claude SDK Team POC

Lead agent + 3 teammates (`researcher`, `coder`, `reviewer`) via `claude_agent_sdk.AgentDefinition`.
Lead delegates subtasks through built-in `Task` tool, no manual routing code.

## Setup

```
pip install -r requirements.txt
```

Needs `ANTHROPIC_API_KEY` env var set (or Claude Code CLI login already configured — SDK uses same auth).

## Run

```
python team.py "Summarize what this repo does, then write a hello.py, then review it"
```

## How it works

- `team.py` defines `TEAM`: dict of `AgentDefinition` (description, prompt, allowed tools, model).
- `ClaudeAgentOptions(agents=TEAM, allowed_tools=["Task"])` — lead can only call `Task`, forcing delegation.
- Streams lead's text + each `Task` delegation as it happens, prints final cost/turns.

## Next steps to explore

- Add more roles (tester, docs writer) or MCP tools per role.
- Swap `allowed_tools=["Task"]` to let lead also act directly, compare behavior.
- Try `permissionMode` per-agent (e.g. reviewer as `plan`-only) in `AgentDefinition`.
- Log full `receive_response()` stream to see subagent-level `SubagentStart`/`SubagentStop` hooks.
