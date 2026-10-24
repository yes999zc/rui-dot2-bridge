"""The plug manifest: what this device offers a socket, as declarative data.

Blind-run v0 of the plug/socket contract (docs/plug-socket-contract.md): rui's
socket spec does not exist yet, so this module reads ``plug.json`` and uses it
as *defaults*. Precedence, lowest to highest:

    plug.json  <  DOT2_* environment variables  <  rui's DeviceConfig

The manifest is data, not code: a deployment can point ``DOT2_PLUG_FILE`` at a
different manifest without touching this package, and fields the socket does not
understand are simply ignored by the consumers here.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_ENTRYPOINT = "rui_dot2_bridge.adapter:build"


def find_manifest() -> Path | None:
    """``DOT2_PLUG_FILE`` first, then the repo root, then the working directory."""
    env = Path(__import__("os").environ.get("DOT2_PLUG_FILE", "")) if __import__("os").environ.get("DOT2_PLUG_FILE") else None
    if env is not None:
        return env if env.is_file() else None
    repo_root = Path(__file__).resolve().parents[2]
    for candidate in (repo_root / "plug.json", Path.cwd() / "plug.json"):
        if candidate.is_file():
            return candidate
    return None


def load(path: Path | None = None) -> dict[str, Any]:
    """Load and sanity-check the manifest; a broken file must not stop the speaker."""
    path = path or find_manifest()
    if path is None:
        return {}
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        log.warning("plug: ignoring unreadable %s (%s)", path, exc)
        return {}
    if not isinstance(data, dict):
        log.warning("plug: %s is not a JSON object", path)
        return {}
    entrypoint = data.get("entrypoint")
    if entrypoint and entrypoint != DEFAULT_ENTRYPOINT:
        log.warning("plug: manifest entrypoint %r != %r", entrypoint, DEFAULT_ENTRYPOINT)
    return data


def get(manifest: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Nested lookup: ``get(m, "audio", "playback", "prebuffer_ms", default=450)``."""
    node: Any = manifest
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node
