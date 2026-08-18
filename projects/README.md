# Projects root

Each subfolder here that has a `project.json` becomes an available peer,
picked up automatically -- no code change, no restart needed (rescanned
before every discussion). Folders without a manifest are ignored, so a
work-in-progress project is safe to leave half-built.

## `project.json`

```json
{
  "key": "finance",
  "name": "Finance Advisor",
  "emoji": "💰",
  "description": "Tracks family budget, flags overspending, answers money questions.",
  "tools": ["Read", "Write", "Edit", "Grep", "Glob"],
  "prompt": "You help the family track spending and budget. Be specific with numbers."
}
```

- `key`, `name`, `description` are required. `key` must be unique across
  every project folder.
- `tools` (optional) is a hard restriction on which built-in tools this
  peer gets -- defaults to `["Read", "Grep", "Glob", "WebSearch", "WebFetch"]`
  (read-only) if omitted. Add `Write`/`Edit`/`Bash` only for a project that
  actually needs to change files or run commands.
- `prompt` (optional) is the project-specific instructions layered under
  the shared group-chat rules (see `_make_peer_prompt` in `telegram_team.py`).
- Which physical device runs this peer is NOT set here -- see
  `devices.json.example` in the repo root. The manifest is meant to be
  identical on every device's copy (e.g. via Syncthing), so it can't
  sensibly also say which device owns it.
