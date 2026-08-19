"""
LAN discovery beacon: lets the orchestrator find a backend device's current
IP without any static address stored anywhere. A router restart / DHCP
lease change would otherwise silently break a hardcoded devices.json IP --
this replaces that with "ask the LAN, whoever answers for this device_id
tells you where it is right now."

Protocol: tiny UDP request/reply on DISCOVERY_PORT, broadcast-based.
  orchestrator -> broadcast {"type": "discover", "device_id": "phone1"}
  matching backend -> unicast reply {"type": "here", "device_id": "phone1", "port": 8800}
No third-party dependency, no persistent daemon beyond the one already
listening for the backend's own HTTP server.
"""

from __future__ import annotations

import asyncio
import json
import socket

DISCOVERY_PORT = 8801
BROADCAST_ADDR = "255.255.255.255"


def _local_subnet_broadcast() -> str | None:
    """Best-effort subnet-directed broadcast (e.g. 192.168.1.255) for this
    host's primary LAN interface. The global 255.255.255.255 broadcast
    gets silently dropped on some router/OS combos (confirmed live: reached
    a phone fine via direct unicast, never arrived via 255.255.255.255) --
    a directed broadcast to the actual /24 is handled far more reliably.
    Assumes a /24 subnet, the overwhelmingly common case for a home LAN;
    worst case this is just an extra harmless packet alongside the global
    broadcast this is sent together with."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # UDP connect() does no I/O, just picks a route
        local_ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    octets = local_ip.split(".")
    if len(octets) != 4:
        return None
    return ".".join(octets[:3] + ["255"])


class _BeaconServerProtocol(asyncio.DatagramProtocol):
    def __init__(self, device_id: str, service_port: int):
        self.device_id = device_id
        self.service_port = service_port
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            msg = json.loads(data.decode())
        except (ValueError, UnicodeDecodeError):
            return
        if msg.get("type") == "discover" and msg.get("device_id") == self.device_id:
            reply = json.dumps(
                {"type": "here", "device_id": self.device_id, "port": self.service_port}
            ).encode()
            self.transport.sendto(reply, addr)


async def serve(device_id: str, service_port: int, host: str = "0.0.0.0") -> None:
    """Runs forever: answer LAN discovery queries for `device_id` with our
    own current address. Meant to run as a background asyncio task
    alongside the backend's own HTTP server."""
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _BeaconServerProtocol(device_id, service_port),
        local_addr=(host, DISCOVERY_PORT),
        allow_broadcast=True,
    )
    try:
        await asyncio.Event().wait()  # run until cancelled
    finally:
        transport.close()


class _ClientProtocol(asyncio.DatagramProtocol):
    def __init__(self, device_id: str, found: asyncio.Future[str]):
        self.device_id = device_id
        self.found = found

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            msg = json.loads(data.decode())
        except (ValueError, UnicodeDecodeError):
            return
        if msg.get("type") == "here" and msg.get("device_id") == self.device_id and not self.found.done():
            self.found.set_result(f"http://{addr[0]}:{msg['port']}")


async def discover(device_id: str, timeout: float = 2.0) -> str | None:
    """Broadcasts a discovery query on the LAN, returns the first
    `http://ip:port` reply matching `device_id`, or None if nobody
    answers within `timeout` seconds (device offline/unreachable)."""
    loop = asyncio.get_running_loop()
    found: asyncio.Future[str] = loop.create_future()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _ClientProtocol(device_id, found),
        local_addr=("0.0.0.0", 0),
        allow_broadcast=True,
    )
    try:
        query = json.dumps({"type": "discover", "device_id": device_id}).encode()
        transport.sendto(query, (BROADCAST_ADDR, DISCOVERY_PORT))
        subnet_bcast = _local_subnet_broadcast()
        if subnet_bcast:
            transport.sendto(query, (subnet_bcast, DISCOVERY_PORT))
        return await asyncio.wait_for(found, timeout=timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        transport.close()
