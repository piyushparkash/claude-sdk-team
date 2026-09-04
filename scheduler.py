"""
Dynamic chat-triggered scheduling: the lead can call schedule_task/
list_schedules/cancel_schedule mid-conversation (e.g. human says "run
newsjargon every day at 8am") and it persists across bot restarts.

Storage is one flat JSON file (schedules.json) -- a list of entries, each
{id, chat_id, cron, message, created_at, next_run}. `cron` is a standard
5-field cron expression (minute hour day month weekday); croniter computes
next_run from it, in the server's local timezone (matches how a human says
"8am" -- no explicit timezone handling needed for a single-machine, single-
household setup like this one).

The actual firing loop (run_scheduler_loop) lives in telegram_team.py's
event loop, polling once a minute -- coarse enough that pip croniter's own
resolution (minute-level) isn't wasted, fine enough that "8am" fires within
a minute of 8am.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta

from croniter import croniter

SCHEDULES_PATH = os.environ.get(
    "SCHEDULES_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "schedules.json"),
)


def _load() -> list[dict]:
    if not os.path.exists(SCHEDULES_PATH):
        return []
    try:
        with open(SCHEDULES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save(schedules: list[dict]) -> None:
    tmp = SCHEDULES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(schedules, f, indent=2)
    os.replace(tmp, SCHEDULES_PATH)  # atomic on both POSIX and Windows


def add_schedule(chat_id: int, cron: str, message: str) -> dict:
    """Validates the cron expression up front (raises ValueError on a bad
    one -- the lead's tool call surfaces that back as a normal error reply
    instead of silently storing something that will never fire)."""
    now = datetime.now()
    next_run = croniter(cron, now).get_next(datetime)  # raises on bad cron
    entry = {
        "id": uuid.uuid4().hex[:8],
        "chat_id": chat_id,
        "cron": cron,
        "message": message,
        "created_at": now.isoformat(),
        "next_run": next_run.isoformat(),
    }
    schedules = _load()
    schedules.append(entry)
    _save(schedules)
    return entry


def add_once(chat_id: int, delay_seconds: int, message: str) -> dict:
    """One-shot version of add_schedule -- fires exactly once after
    `delay_seconds`, then removes itself, instead of recurring on a cron.
    Meant for the lead's own check_back_later tool: it's mid-discussion,
    something (a herdr build, a long scrape, a render) needs more time than
    is sensible to just sit blocked waiting on, so it ends the turn now and
    gets woken back up later to actually check, rather than the human
    having to remember to ping it. `cron` is stored as None so
    due_schedules() knows to delete rather than reschedule this entry."""
    if delay_seconds < 1:
        raise ValueError("delay_seconds must be at least 1")
    now = datetime.now()
    next_run = now + timedelta(seconds=delay_seconds)
    entry = {
        "id": uuid.uuid4().hex[:8],
        "chat_id": chat_id,
        "cron": None,
        "message": message,
        "created_at": now.isoformat(),
        "next_run": next_run.isoformat(),
    }
    schedules = _load()
    schedules.append(entry)
    _save(schedules)
    return entry


def list_schedules(chat_id: int) -> list[dict]:
    return [s for s in _load() if s["chat_id"] == chat_id]


def cancel_schedule(chat_id: int, schedule_id: str) -> bool:
    schedules = _load()
    kept = [s for s in schedules if not (s["chat_id"] == chat_id and s["id"] == schedule_id)]
    if len(kept) == len(schedules):
        return False
    _save(kept)
    return True


def due_schedules(now: datetime | None = None) -> list[dict]:
    """Entries whose next_run has passed. Advances next_run (and persists)
    for each returned entry in the same call, so a caller that then crashes
    mid-fire can't get the same entry twice on restart -- next_run is
    already moved forward before the fire is attempted. A one-shot entry
    (cron is None, see add_once) is removed instead of rescheduled -- it
    fires exactly once, same crash-safety property (removed before the
    fire is attempted, not after)."""
    now = now or datetime.now()
    schedules = _load()
    due, kept, changed = [], [], False
    for entry in schedules:
        next_run = datetime.fromisoformat(entry["next_run"])
        if next_run <= now:
            due.append(dict(entry))  # copy -- caller gets the pre-advance next_run too
            changed = True
            if entry["cron"] is not None:
                entry["next_run"] = croniter(entry["cron"], now).get_next(datetime).isoformat()
                kept.append(entry)
            # else: one-shot, drop it -- not re-appended to kept
        else:
            kept.append(entry)
    if changed:
        _save(kept)
    return due
