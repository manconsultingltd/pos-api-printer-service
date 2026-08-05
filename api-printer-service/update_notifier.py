"""
Daily update check + desktop notification for the API Printer Service.

Runs inside the user-session control-panel GUIs (Linux GTK / Windows Tk),
NOT inside the service process: the service runs as root (systemd) or as
SYSTEM in session 0 (Windows Scheduled Task), where a desktop notification
can never reach the logged-in user's desktop.

The installed version is read from the running service's /health endpoint
(the service binary is the artifact CI stamps with the release version;
GUI source files keep the 0.0.0-dev placeholder). The latest published
version comes from the GitHub Releases API.

Check + notification happen at most once per calendar day. The gate is a
per-user state file recording the date of the last successful check, so
restarting the GUI does not re-check or re-notify the same day.

Notification transport is plyer (works on Windows via pywin32 and on Linux
via notify-send/dbus). The Linux GUI runs on the system python3 where pip
packages may be absent, so a direct notify-send fallback covers that case.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from typing import Callable, Optional
from urllib import request as urlrequest

log = logging.getLogger("api-printer-update")

GITHUB_REPO = "manconsultingltd/pos-api-printer-service"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"
APP_NAME = "API Printer Service"

# Retry cadence for the polling loop. The once-per-day guarantee lives in
# the state file (last successful check date), not in this interval — the
# loop just wakes up to see whether a new day has started or whether an
# earlier attempt failed (service down, no network) and should be retried.
POLL_INTERVAL_SECONDS = 60 * 60
STARTUP_DELAY_SECONDS = 20
HTTP_TIMEOUT = 10.0


def _state_file() -> str:
    """Per-user path of the JSON state file (overridable for tests)."""
    override = os.environ.get("API_PRINTER_UPDATE_STATE_FILE")
    if override:
        return override
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "api-printer-service", "update_check.json")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "api-printer-service", "update_check.json")


def _load_state() -> dict:
    try:
        with open(_state_file(), "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    path = _state_file()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def parse_version(text: Optional[str]) -> Optional[tuple]:
    """
    "1.1.23" / "v1.1.23" -> (1, 1, 23). None for anything non-numeric —
    including the 0.0.0-dev placeholder, so dev builds never notify.
    """
    if not text:
        return None
    text = text.strip().lstrip("vV")
    try:
        return tuple(int(p) for p in text.split("."))
    except ValueError:
        return None


def is_newer(latest: tuple, current: tuple) -> bool:
    """Numeric compare with length padding: (1, 2) vs (1, 2, 0) is equal."""
    width = max(len(latest), len(current))
    pad = lambda v: v + (0,) * (width - len(v))
    return pad(latest) > pad(current)


def fetch_latest_version() -> Optional[str]:
    """Tag of the latest GitHub release without the leading 'v', or None."""
    req = urlrequest.Request(
        LATEST_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "api-printer-service-update-check",
        },
    )
    try:
        with urlrequest.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.load(resp)
    except Exception as e:
        log.warning("Update check: GitHub API unreachable: %s", e)
        return None
    tag = (data.get("tag_name") or "").strip()
    return tag.lstrip("vV") or None


def notify(title: str, message: str) -> bool:
    """Desktop notification via plyer, notify-send fallback on Linux."""
    try:
        from plyer import notification

        notification.notify(
            title=title, message=message, app_name=APP_NAME, timeout=15
        )
        return True
    except Exception as e:
        # ImportError (plyer not installed — the Linux GUI runs on the
        # system python3) or NotImplementedError (no backend available).
        log.info("plyer notification unavailable: %s", e)
    if sys.platform.startswith("linux"):
        try:
            subprocess.run(
                ["notify-send", "--app-name", APP_NAME, "--icon", "printer",
                 title, message],
                capture_output=True, timeout=10, check=True,
            )
            return True
        except Exception as e:
            log.warning("notify-send fallback failed: %s", e)
    return False


def check_once(
    get_current_version: Callable[[], Optional[str]],
    today: Optional[str] = None,
) -> Optional[str]:
    """
    Run one gated check. Returns the human-readable update message when a
    newer release was found (already notified), else None.

    The check only consumes today's slot after BOTH sides resolved: an
    unreachable service or a failed GitHub call leaves the state untouched
    so the next poll retries.
    """
    today = today or time.strftime("%Y-%m-%d")
    if _load_state().get("last_check") == today:
        return None

    try:
        current = get_current_version()
    except Exception as e:
        log.debug("Update check: current version unavailable: %s", e)
        return None
    current_t = parse_version(current)
    if current_t is None:
        # Service down, pre-versioning /health, or a 0.0.0-dev build.
        log.debug("Update check skipped (current version: %r)", current)
        return None

    latest = fetch_latest_version()
    latest_t = parse_version(latest)
    if latest_t is None:
        return None

    _save_state({
        "last_check": today,
        "current_version": current,
        "latest_version": latest,
    })

    if not is_newer(latest_t, current_t):
        log.info("Update check: %s is up to date (latest: %s)", current, latest)
        return None

    message = (
        f"Version {latest} is available (installed: {current}).\n"
        f"Download: {RELEASES_PAGE}"
    )
    notify(f"{APP_NAME} — Update available", message)
    log.info("Update available: %s -> %s", current, latest)
    return message


def start_daily_check(
    get_current_version: Callable[[], Optional[str]],
    on_update: Optional[Callable[[str], None]] = None,
) -> threading.Thread:
    """
    Start the daemon polling thread. `get_current_version` is called from
    that thread (the GUIs pass a lambda over their HTTP client). `on_update`
    also runs on the thread — GUI callers must marshal to their main loop.
    """

    def _loop() -> None:
        time.sleep(STARTUP_DELAY_SECONDS)
        while True:
            try:
                message = check_once(get_current_version)
                if message and on_update is not None:
                    try:
                        on_update(message)
                    except Exception:
                        log.exception("on_update callback failed")
            except Exception:
                log.exception("Update check iteration failed")
            time.sleep(POLL_INTERVAL_SECONDS)

    thread = threading.Thread(target=_loop, name="update-check", daemon=True)
    thread.start()
    return thread
