"""
Update ring (channel) preference for the self-update system.

Design notes:

  - Stored as a small JSON file NEXT TO the persistent settings file (NOT
    inside it — the updater must be readable/writable independently of the
    print settings, and a corrupt preference must never break printing).
  - Everything defensive: a hand-edited, truncated or half-written file
    degrades to the default ring rather than raising. An unreadable
    preference is never a reason to fail to start the print service.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional

UPDATE_CHANNELS = ("stable", "preview")
DEFAULT_CHANNEL = "stable"

# 24 hours between update checks.
CHECK_INTERVAL_MS = 24 * 60 * 60 * 1000


def update_settings_path(settings_file: str) -> str:
    """Path of the update settings file, derived from the main settings file."""
    return os.path.join(
        os.path.dirname(os.path.abspath(settings_file)), "update-settings.json"
    )


def is_update_channel(value: Any) -> bool:
    return isinstance(value, str) and value in UPDATE_CHANNELS


def normalize_update_settings(raw: Any) -> Dict[str, Any]:
    """Coerce anything at all into a valid settings object.

    Unknown fields are dropped, bad fields fall back to their default
    INDEPENDENTLY — a corrupt last_checked_at must not also throw away a
    deliberately chosen channel.
    """
    source = raw if isinstance(raw, dict) else {}
    channel = source.get("channel") if is_update_channel(source.get("channel")) else DEFAULT_CHANNEL
    raw_last = source.get("last_checked_at")
    last_checked_at = (
        raw_last if isinstance(raw_last, (int, float))
        and not isinstance(raw_last, bool)
        and raw_last > 0 else None
    )
    return {"channel": channel, "last_checked_at": last_checked_at}


def read_update_settings(file: str) -> Dict[str, Any]:
    try:
        with open(file, "r", encoding="utf-8") as f:
            return normalize_update_settings(json.load(f))
    except (OSError, ValueError):
        # Missing (first run) or malformed — both mean "use the defaults".
        return normalize_update_settings(None)


def write_update_settings(file: str, settings: Dict[str, Any]) -> None:
    """Write atomically: a crash mid-write must not leave a truncated file
    that silently resets the ring on next boot. Failures are swallowed —
    losing the preference is bad; refusing to run over it is worse."""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(file)), exist_ok=True)
        tmp = f"{file}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(normalize_update_settings(settings), f, indent=2, sort_keys=True)
        os.replace(tmp, file)
    except OSError:
        pass


def is_check_due(
    last_checked_at: Optional[float],
    now: float,
    interval_ms: float = CHECK_INTERVAL_MS,
) -> bool:
    """A machine that is asleep/asuspended at the 24h mark never gets a timer
    tick, so the schedule alone would mean "checks only while awake, forever".
    Persisting last_checked_at and asking this at launch turns that into
    "checks on the first wake after 24h"."""
    if last_checked_at is None:
        return True
    # A clock that jumped backwards (or a future timestamp copied between
    # machines) would otherwise wedge checks off until real time caught up.
    if last_checked_at > now:
        return True
    return now - last_checked_at >= interval_ms


_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def parse_semver(version: str) -> Optional[tuple]:
    if not _VERSION_RE.match(version):
        return None
    return tuple(int(p) for p in version.split("."))


def is_newer_version(candidate: str, current: str) -> bool:
    """Strict three-component compare. Either side unparseable → False:
    never treat a malformed version as an upgrade."""
    c, cur = parse_semver(candidate), parse_semver(current)
    if c is None or cur is None:
        return False
    return c > cur
