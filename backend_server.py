"""
Execution backend: runs on a remote device (laptop2, a phone via Termux),
lazily hosts an AgentPeer for whichever project the orchestrator actually
asks for, exposed over HTTP/SSE.

Placement (which project runs on which device) is decided by the
orchestrator, not here -- since every device gets an identical copy of
`projects/` via Syncthing, any device can serve any project it's asked
about. That's what makes placement possible to automate at all: the
orchestrator round-robins across a declared roster of device_ids, live-
checking via the discovery beacon which one actually answers right now,
and just calls that device -- no per-device "am I assigned this?" bookkeeping
needed here.

Run: DEVICE_ID=laptop2 PROJECTS_DIR=... python backend_server.py
"""

from __future__ import annotations

import asyncio
import json
import os

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

import beacon
from discovery import discover_projects
from peers import AgentPeer
from prompts import make_peer_prompt

PROJECTS_DIR = os.environ.get("PROJECTS_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects"))
DEVICE_ID = os.environ.get("DEVICE_ID")
SHARED_SECRET = os.environ.get("TEAM_SHARED_SECRET")
HOST = os.environ.get("BACKEND_HOST", "0.0.0.0")
PORT = int(os.environ.get("BACKEND_PORT", "8800"))

if not DEVICE_ID:
    raise SystemExit("Set DEVICE_ID (an id used in devices.json's 'roster' list).")

app = FastAPI()
peers: dict[str, AgentPeer] = {}


async def _ensure_peer(role_key: str) -> AgentPeer | None:
    """Lazily instantiate a peer for `role_key` the first time it's asked
    for. Returns None if no such project exists locally (its files haven't
    synced here yet, or the key is just wrong) -- the caller treats that as
    a 404, same as an offline device from the orchestrator's point of view."""
    if role_key in peers:
        return peers[role_key]
    manifest = discover_projects(PROJECTS_DIR).get(role_key)
    if manifest is None:
        return None
    print(f"[backend:{DEVICE_ID}] instantiating peer '{role_key}' ({manifest.name})")
    peers[role_key] = AgentPeer(
        role_key=role_key,
        system_prompt=make_peer_prompt(role_key, manifest.description, manifest.prompt),
        tools=manifest.tools,
        cwd=manifest.cwd,
    )
    return peers[role_key]


def _check_secret(request: Request) -> None:
    if SHARED_SECRET and request.headers.get("X-Team-Secret") != SHARED_SECRET:
        raise HTTPException(status_code=401, detail="bad or missing X-Team-Secret")


@app.get("/health")
async def health(request: Request) -> dict:
    _check_secret(request)
    available = list(discover_projects(PROJECTS_DIR).keys())
    return {"status": "ok", "device": DEVICE_ID, "active_peers": list(peers.keys()), "available_projects": available}


@app.post("/peer/{role_key}/message")
async def message(role_key: str, request: Request) -> StreamingResponse:
    _check_secret(request)
    peer = await _ensure_peer(role_key)
    if peer is None:
        raise HTTPException(status_code=404, detail=f"no peer '{role_key}' on this device")
    body = await request.json()
    incoming = body["text"]

    async def event_stream():
        # A generator can't yield from inside peer.say()'s own on_note
        # callback, so notes/reply are relayed through a queue instead.
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def run() -> None:
            reply = await peer.say(incoming, on_note=lambda t: queue.put(json.dumps({"type": "note", "text": t})))
            await queue.put(json.dumps({"type": "reply", "text": reply}))
            await queue.put(None)  # sentinel

        task = asyncio.create_task(run())
        while True:
            item = await queue.get()
            if item is None:
                break
            yield f"data: {item}\n\n"
        await task

    return StreamingResponse(event_stream(), media_type="text/event-stream")


async def _run() -> None:
    available = list(discover_projects(PROJECTS_DIR).keys())
    print(f"[backend:{DEVICE_ID}] projects available to serve: {available}, "
          f"serving on {HOST}:{PORT}, beacon on UDP {beacon.DISCOVERY_PORT}")
    config = uvicorn.Config(app, host=HOST, port=PORT, log_level="info")
    server = uvicorn.Server(config)
    # The beacon is what lets the orchestrator find this device's current IP
    # without a stored address anywhere (see beacon.py) -- it has to be
    # alive for as long as the HTTP server is, so both run as one asyncio
    # task set instead of the beacon being an afterthought.
    await asyncio.gather(
        server.serve(),
        beacon.serve(DEVICE_ID, PORT),
    )


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
