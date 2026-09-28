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

import contextlib
import json
import os
import re
import time
from typing import Any, Dict, Iterator, Optional

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


# --- shared install lock -----------------------------------------------------
# Two updaters exist by design: the in-service one (drive by /api/update/*)
# and the OUT-OF-SERVICE scheduled one (updater_cli.py, works while the
# service is dead). Both must never download/install concurrently — the lock
# file lives next to the settings file and is taken for the whole
# download+launch span. O_EXCL makes creation atomic; a lock older than an
# hour is stale (crashed holder) and may be broken.

LOCK_MAX_AGE_S = 60 * 60


@contextlib.contextmanager
def update_lock(settings_file: str) -> Iterator[bool]:
    """Yield True when the lock was acquired, False when someone else is
    mid-update (the caller should skip quietly)."""
    lock_path = os.path.join(
        os.path.dirname(os.path.abspath(settings_file)), "update.lock"
    )
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    try:
        if os.path.exists(lock_path) and \
                time.time() - os.path.getmtime(lock_path) > LOCK_MAX_AGE_S:
            os.unlink(lock_path)  # stale — crashed holder
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        yield False
        return
    except OSError:
        # Can't create the lock dir/file: better to let the caller proceed
        # unlocked than to make updates impossible.
        yield True
        return
    try:
        os.write(fd, str(os.getpid()).encode())
        yield True
    finally:
        try:
            os.close(fd)
            os.unlink(lock_path)
        except OSError:
            pass
