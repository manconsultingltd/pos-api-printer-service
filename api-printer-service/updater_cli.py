#!/usr/bin/env python3
"""
Standalone self-update worker — independent of the API service process.

Runs from a Windows Scheduled Task (hourly) or a systemd timer (Linux),
whether or not the service is alive: if the running service crashed or was
replaced by a broken build, this is what installs the next working version.

Design (mirrors the in-service updater's rules, so behaviour is identical
whichever path fires):

  - Ring preference and the 24h throttle live in update-settings.json — the
    SAME file the in-service updater uses, so neither path double-checks.
  - A shared lock file prevents the two updaters from ever downloading or
    installing at the same time.
  - stable = newest non-prerelease; preview = newest of any kind; downgrades
    are never installed.
  - Install is Windows-only (silent NSIS /S). Linux/macOS are notify-only:
    the release page is recorded for humans to open.
  - Every outcome lands in update-state.json (status / available_version /
    error), so any UI can display what happened.

Stdlib only: safe to run under the bundled Windows Python or the system
python3 on Linux.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

from update_feed import (
    REPO_SLUG,
    collect_releases,
    is_release_page_url,
    pick_release_for_channel,
    asset_for_platform,
    is_notifiable_update,
)
from update_settings import (
    is_check_due,
    read_update_settings,
    write_update_settings,
    update_lock,
)

# The version this installation IS. CI stamps service_version.py at build
# time; a dev checkout stays 0.0.0-dev and never self-updates.
from service_version import VERSION

DOWNLOAD_CHUNK = 256 * 1024

# Same watchdog bound as the in-service updater (updater.py): a silent
# installer that hangs must not leave 'installing' in update-state.json
# forever — the GUI reads that state and would show "Installing…" eternally.
INSTALLER_TIMEOUT_S = 15 * 60

# Detached-process flags for the silent installer (Windows).
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_DETACHED = 0x00000008 | 0x00000200 | 0x01000000 if sys.platform == "win32" else 0


def _settings_file() -> str:
    """Same location the service uses for settings.json, so both updaters
    share the ring/throttle state (duplicated from config._default_settings_file —
    config.py pulls in printer-only bits this worker must not need)."""
    env = os.environ.get("API_PRINTER_SETTINGS_FILE")
    if env:
        return env
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", "C:\\")
        return os.path.join(base, "api-printer-service", "settings.json")
    if sys.platform == "darwin":
        return "/Library/Application Support/api-printer-service/settings.json"
    return "/etc/api-printer-service/settings.json"


def _state_file(settings_file: str) -> str:
    return os.path.join(
        os.path.dirname(os.path.abspath(settings_file)), "update-state.json"
    )


def write_state(settings_file: str, patch: dict) -> None:
    """Merge `patch` into update-state.json atomically. Lossy failures are
    swallowed: a read-only state dir must not crash the worker."""
    path = _state_file(settings_file)
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        state = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f) or {}
        except (OSError, ValueError):
            pass
        state.update(patch)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass


def platform_name() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def capability(platform: str) -> tuple[str, str | None]:
    """Same platform facts the in-service updater applies."""
    if platform == "windows":
        return "auto", None
    if platform == "linux":
        return "notify", (
            "The Linux installer is a GUI wizard and must be run "
            "interactively; updates are notified on the release page."
        )
    return "notify", (
        "No installer asset is published for this platform; "
        "updates must be installed manually from the release page."
    )


def _download(release_url: str, asset_name: str) -> str:
    """Stream the installer asset to a temp file. The URL is derived from
    the release page (validated by the feed), never from asset JSON."""
    import re as _re

    m = _re.match(
        r"^https://github\.com/%s/releases/tag/v(.+)$" % _re.escape(REPO_SLUG),
        release_url,
    )
    if not m:
        raise RuntimeError("cannot derive download URL from release page")
    url = (
        f"https://github.com/{REPO_SLUG}/releases/download/"
        f"v{m.group(1)}/{asset_name}"
    )
    req = urllib.request.Request(
        url, headers={"User-Agent": f"api-printer-service/{VERSION}"}
    )
    fd, path = tempfile.mkstemp(prefix="api-printer-update-", suffix=".exe")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, os.fdopen(fd, "wb") as f:
            if resp.status != 200:
                raise RuntimeError(f"download returned {resp.status}")
            total = 0
            while True:
                chunk = resp.read(DOWNLOAD_CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                total += len(chunk)
        return path
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def _launch_installer_windows(path: str) -> "subprocess.Popen":
    """/S = silent NSIS. The installer stops this machine's service, replaces
    the files and re-registers/starts the task itself. The Popen handle is
    returned so the caller can watch the exit code instead of leaving
    'installing' as a terminal state."""
    return subprocess.Popen(
        [path, "/S"],
        creationflags=_DETACHED,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _watch_installer_windows(settings_file: str, proc: "subprocess.Popen") -> None:
    """Publish the installer's outcome to update-state.json.

    The CLI process may be killed by the Task Scheduler's 30-minute limit
    while waiting (harmless: the installer runs detached), so this is
    best-effort by design — it only matters when the installer fails or
    hangs BEFORE stopping the service, in which case somebody has to move
    the state out of 'installing'."""
    try:
        code = proc.wait(timeout=INSTALLER_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        write_state(settings_file, {
            "status": "error",
            "progress_percent": None,
            "error": (
                f"the installer did not finish within "
                f"{INSTALLER_TIMEOUT_S // 60} minutes — it may be hung; "
                "check the install log and try again"
            ),
        })
        return
    if code == 0:
        write_state(settings_file, {
            "status": "ready", "progress_percent": 100, "error": None,
        })
        return
    write_state(settings_file, {
        "status": "available",
        "progress_percent": None,
        "error": (
            f"the installer exited with code {code} and the update was "
            "not applied — the previous version is still running; "
            "retry later"
        ),
    })


def _check(settings_file: str) -> dict:
    """One check. Returns the picked release (or None) and mutates state."""
    settings = read_update_settings(settings_file)
    channel = settings["channel"]
    now_ms = time.time() * 1000

    cap, reason = capability(platform_name())
    try:
        releases = collect_releases(channel)
        picked = pick_release_for_channel(releases, channel)
    except Exception as e:  # noqa: BLE001 — a failed check is not fatal
        write_state(settings_file, {
            "last_checked_at": now_ms,
            "capability": cap,
            "capability_reason": reason,
            "current_version": VERSION,
            "channel": channel,
            "status": "error",
            "error": str(e),
            "available_version": None,
            "release_url": None,
            "asset_name": None,
            "progress_percent": None,
        })
        write_update_settings(
            settings_file, {"channel": channel, "last_checked_at": now_ms}
        )
        return {}

    if picked and is_notifiable_update(VERSION, picked):
        write_state(settings_file, {
            "last_checked_at": now_ms,
            "capability": cap,
            "capability_reason": reason,
            "current_version": VERSION,
            "channel": channel,
            "status": "available",
            "available_version": picked["version"],
            "release_url": picked["url"],
            "asset_name": asset_for_platform(picked, platform_name()),
            "progress_percent": None,
            "error": None,
        })
    else:
        write_state(settings_file, {
            "last_checked_at": now_ms,
            "capability": cap,
            "capability_reason": reason,
            "current_version": VERSION,
            "channel": channel,
            "status": "up-to-date",
            "available_version": None,
            "release_url": None,
            "asset_name": None,
            "progress_percent": None,
            "error": None,
        })
    # Shared throttle: the in-service updater sees this and skips its own
    # tick — one ring decision per interval, whichever path ran first.
    write_update_settings(
        settings_file, {"channel": channel, "last_checked_at": now_ms}
    )
    return picked or {}


def run(settings_file: str, check_now: bool = False, install_now: bool = False) -> int:
    settings = read_update_settings(settings_file)
    if not (check_now or install_now):
        # Scheduled cadence: the task fires hourly, the 24h decision lives
        # in the shared settings file (is_check_due).
        if not is_check_due(settings["last_checked_at"], time.time() * 1000):
            return 0

    with update_lock(settings_file) as acquired:
        if not acquired:
            # The other updater (service or a concurrent task run) is
            # mid-download/install — skipping is the correct outcome.
            return 0
        picked = _check(settings_file)
        if not install_now:
            return 0
        if not picked:
            return 0
        if platform_name() != "windows":
            return 0  # notify-only platforms: nothing to launch
        asset = picked and asset_for_platform(picked, "windows")
        if not asset:
            write_state(settings_file, {
                "status": "available",
                "error": "release has no installer asset for this platform",
            })
            return 0
        release_url = picked.get("url") or ""
        if not is_release_page_url(release_url):
            write_state(settings_file, {"status": "available", "error": "release URL failed validation"})
            return 0
        try:
            write_state(settings_file, {"status": "downloading", "progress_percent": 0})
            path = _download(release_url, asset)
        except Exception as e:  # noqa: BLE001
            write_state(settings_file, {
                "status": "available",
                "progress_percent": None,
                "error": f"download failed: {e}",
            })
            return 1
        write_state(settings_file, {"status": "installing", "progress_percent": 100})
        proc = _launch_installer_windows(path)
        _watch_installer_windows(settings_file, proc)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="API Printer Service updater")
    parser.add_argument("--check-now", action="store_true",
                        help="run a check immediately, ignoring the 24h throttle")
    parser.add_argument("--install-now", action="store_true",
                        help="check, then install if a newer version is available")
    args = parser.parse_args()
    settings_file = _settings_file()
    try:
        return run(settings_file, check_now=args.check_now, install_now=args.install_now)
    except Exception as e:  # noqa: BLE001 — the worker must never crash loudly
        write_state(settings_file, {"status": "error", "error": str(e)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
