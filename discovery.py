"""
Project auto-discovery: point at one root directory of project folders, each
folder that has a project.json manifest becomes an available peer. No
hand-editing a peer list when a new project folder shows up.

Manifest is device-agnostic on purpose -- the project folder itself gets
synced byte-for-byte to every device (e.g. via Syncthing), so a manifest
that's identical on all copies can't sensibly also declare which device owns
it. That lives in devices.json instead (see load_assignments below) --
which device_id maps to which LAN address is never stored anywhere; it's
resolved live via beacon.discover() so a router restart / DHCP change can't
break it (see beacon.py).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

MANIFEST_NAME = "project.json"
REQUIRED_FIELDS = ("key", "name", "description")


@dataclass
class ProjectManifest:
    key: str
    name: str
    description: str
    cwd: str  # absolute path to the project's own folder
    emoji: str = ""
    tools: list[str] | None = None
    prompt: str = ""

    def __post_init__(self) -> None:
        if self.tools is None:
            self.tools = ["Read", "Grep", "Glob", "WebSearch", "WebFetch"]


def discover_projects(root: str) -> dict[str, ProjectManifest]:
    """Walk one level under `root`; read project.json from each subfolder
    that has one. Folders without a manifest are silently skipped -- lets
    you keep work-in-progress project folders that aren't ready yet."""
    found: dict[str, ProjectManifest] = {}
    if not os.path.isdir(root):
        return found

    for entry in sorted(os.listdir(root)):
        project_dir = os.path.join(root, entry)
        manifest_path = os.path.join(project_dir, MANIFEST_NAME)
        if not os.path.isdir(project_dir) or not os.path.isfile(manifest_path):
            continue
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[discovery] skipping {manifest_path}: {exc}")
            continue

        missing = [field for field in REQUIRED_FIELDS if not data.get(field)]
        if missing:
            print(f"[discovery] skipping {manifest_path}: missing {missing}")
            continue

        key = data["key"].strip().lower()
        found[key] = ProjectManifest(
            key=key,
            name=data["name"],
            description=data["description"],
            cwd=project_dir,
            emoji=data.get("emoji", ""),
            tools=data.get("tools"),
            prompt=data.get("prompt", ""),
        )
    return found


def load_assignments(path: str) -> dict[str, str]:
    """Reads devices.json -> {project_key: device_id}. A project with no
    entry (or an entry of "local"/missing) runs in the local process;
    anything else is a device_id resolved live via beacon.discover() --
    no LAN address is ever stored here, so it can't go stale."""
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("assignments", {})
