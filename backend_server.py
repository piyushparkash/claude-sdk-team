"""
Execution backend: runs on a remote device (laptop2, a phone via Termux),
hosts one or more AgentPeers assigned to this device (per devices.json),
exposes them over HTTP/SSE for the orchestrator to call.

Run: DEVICE_ID=laptop2 PROJECTS_DIR=... python backend_server.py
"""

from __future__ import annotations

import asyncio
import json
import os

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from discovery import discover_projects, load_devices
from peers import AgentPeer

PROJECTS_DIR = os.environ.get("PROJECTS_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects"))
DEVICES_PATH = os.environ.get("DEVICES_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "devices.json"))
DEVICE_ID = os.environ.get("DEVICE_ID")
SHARED_SECRET = os.environ.get("TEAM_SHARED_SECRET")
HOST = os.environ.get("BACKEND_HOST", "0.0.0.0")
PORT = int(os.environ.get("BACKEND_PORT", "8800"))

if not DEVICE_ID:
    raise SystemExit("Set DEVICE_ID (must match a key in devices.json's 'devices' map).")

app = FastAPI()
peers: dict[str, AgentPeer] = {}


async def _rebuild_peers() -> None:
    """Discover projects assigned to THIS device, create AgentPeers for any
    new ones, drop peers whose project/assignment disappeared. Mirrors the
    orchestrator's own per-message rescan (see ChatSession.handle_human_message)."""
    manifests = discover_projects(PROJECTS_DIR)
    _devices, assignments = load_devices(DEVICES_PATH)
    mine = {key for key, device in assignments.items() if device == DEVICE_ID}

    for key in list(peers):
        if key not in mine or key not in manifests:
            print(f"[backend:{DEVICE_ID}] dropping peer '{key}' (no longer assigned/present)")
            await peers.pop(key).disconnect()

    for key in mine:
        manifest = manifests.get(key)
        if manifest is None or key in peers:
            continue
        print(f"[backend:{DEVICE_ID}] adding peer '{key}' ({manifest.name})")
        peers[key] = AgentPeer(
            role_key=key,
            system_prompt=manifest.prompt,
            tools=manifest.tools,
            cwd=manifest.cwd,
        )


def _check_secret(request: Request) -> None:
    if SHARED_SECRET and request.headers.get("X-Team-Secret") != SHARED_SECRET:
        raise HTTPException(status_code=401, detail="bad or missing X-Team-Secret")


@app.get("/health")
async def health(request: Request) -> dict:
    _check_secret(request)
    await _rebuild_peers()
    return {"status": "ok", "device": DEVICE_ID, "peers": list(peers.keys())}


@app.post("/peer/{role_key}/message")
async def message(role_key: str, request: Request) -> StreamingResponse:
    _check_secret(request)
    await _rebuild_peers()
    peer = peers.get(role_key)
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


def main() -> None:
    asyncio.run(_rebuild_peers())
    print(f"[backend:{DEVICE_ID}] peers: {list(peers.keys())}, serving on {HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
