"""Read authoritative, user-visible conversation metadata from Antigravity.

Antigravity CLI maintains a small JSON cache keyed by conversation ID.  This
module intentionally reads only the summary title; it never opens conversation
databases or returns previews/prompts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_METADATA_PATH = Path("~/.gemini/antigravity-cli/cache/conversation_metadata.json")


def metadata_path() -> Path:
    override = os.environ.get("AGY_MCP_ANTIGRAVITY_METADATA")
    return Path(override).expanduser() if override else DEFAULT_METADATA_PATH.expanduser()


def conversation_title(session_id: str | None, path: Path | None = None) -> str | None:
    """Return Antigravity's own title for *session_id*, if currently available."""

    if not session_id:
        return None
    target = path or metadata_path()
    try:
        payload: Any = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    conversations = payload.get("conversations") if isinstance(payload, dict) else None
    entry = conversations.get(session_id) if isinstance(conversations, dict) else None
    summary = entry.get("summary") if isinstance(entry, dict) else None
    title = summary.get("Title") if isinstance(summary, dict) else None
    if not isinstance(title, str):
        return None
    clean = " ".join(title.split()).strip()
    return clean[:200] or None


__all__ = ["conversation_title", "metadata_path"]
