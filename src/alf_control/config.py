"""Operator-owned configuration; caller arguments never select a socket or key."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

DEVICE_ID = re.compile(r"^[a-z][a-z0-9-]{1,47}$")

def setting(name: str, legacy: str, default: str = "") -> str:
    return os.environ.get(name, os.environ.get(legacy, default))


def profile() -> str:
    value = setting("ALF_CONTROL_PROFILE", "GUMACU_REMOTE_PROFILE", "local")
    if value not in {"local", "read-only", "controlled", "controlled-write", "operator"}:
        raise ValueError("invalid operator profile")
    return value

@dataclass(frozen=True)
class Device:
    id: str
    socket: str
    os: str = "Linux"
    provider: str = "configured"

def devices() -> list[Device]:
    filename = os.environ.get("ALF_CONTROL_DEVICES_FILE")
    if not filename:
        return [Device("linux-netcup", setting("ALF_CONTROL_SSH_SOCKET", "GUMACU_SSH_SOCKET",
                       "/run/gumacu-ssh-mcp/control.sock"), provider="netcup")]
    path = Path(filename)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError("device configuration must be an absolute regular file")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) != {"devices"} or not isinstance(raw["devices"], list):
        raise ValueError("invalid device configuration")
    if not 1 <= len(raw["devices"]) <= 100:
        raise ValueError("configure between 1 and 100 devices")
    result = []
    for entry in raw["devices"]:
        if not isinstance(entry, dict) or set(entry) - {"id", "socket", "os", "provider"}:
            raise ValueError("invalid device entry")
        device = Device(**entry)
        if not isinstance(device.id, str) or not DEVICE_ID.fullmatch(device.id):
            raise ValueError("invalid device identity")
        if device.id == "windows-main" or device.id in {d.id for d in result}:
            raise ValueError("duplicate or reserved device identity")
        if not isinstance(device.socket, str) or not Path(device.socket).is_absolute():
            raise ValueError("device socket must be absolute")
        if not all(isinstance(x, str) and 0 < len(x) <= 80 for x in (device.os, device.provider)):
            raise ValueError("invalid device metadata")
        result.append(device)
    return result
