"""
API Printer Service — self-update orchestration.

Design notes:

  'auto'   — download the platform installer asset from the GitHub release
             and run it silently. Windows only: the NSIS installer supports
             `/S` and its install action stops and re-registers the service
             task itself, so no extra orchestration is needed here.
  'notify' — every other platform: the control panel shows the new version
             and links to the release page. Linux ships a GUI installer that
             needs a display and user interaction; macOS has no published
             asset at all.

The check never blocks printing: it runs in a daemon thread, and any failure
downgrades to an 'error' state — the service keeps printing on the version
it already has.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import threading
import time
import urllib.request
from typing import Any, Dict, Optional

from update_feed import (
    RELEASES_API,
    RELEASES_PER_PAGE,
    REPO_SLUG,
    asset_for_platform,
    is_notifiable_update,
    is_release_page_url,
    pick_release_for_channel,
)
from update_settings import (
    is_check_due,
    is_update_channel,
    read_update_settings,
    write_update_settings,
    update_lock,
)

logger = logging.getLogger(__name__)

# Delay before the first check so it never competes with service startup.
FIRST_CHECK_DELAY_S = 60
# Timer tick (the actual cadence is is_check_due over persisted state).
TIMER_TICK_S = 60 * 60

DOWNLOAD_CHUNK = 256 * 1024

# How long the launched NSIS installer may run before we give up watching
# it. A silent install extracts a ~180 MB bundle; minutes are normal — a
# quarter of an hour is not. When the cap fires the state leaves the
# terminal 'installing' status, so the UI can never be stuck on
# "Installing…" forever (the #1 reported update failure mode).
INSTALLER_TIMEOUT_S = 15 * 60


class UpdateError(Exception):
    pass


def resolve_capability(platform: str, current_version: str) -> Dict[str, Any]:
    """Which path this install can take. Mirrors updateCapability.ts: chosen
    by platform fact, not preference."""
    if platform == "windows":
        return {"capability": "auto", "reason": None}
    if platform == "linux":
        return {
            "capability": "notify",
            "reason": "The Linux installer is a GUI wizard and must be run "
                      "interactively; the update is downloaded to the release page instead.",
        }
    return {
        "capability": "notify",
        "reason": "No installer asset is published for this platform; "
                  "updates must be installed manually from the release page.",
    }


class Updater:
    """State machine + scheduler. One instance per process."""

    def __init__(self, current_version: str, settings_file: str, platform: str):
        self.current_version = current_version
        self.settings_file = settings_file
        self.platform = platform
        self._cap = resolve_capability(platform, current_version)
        self._timer: Optional[Any] = None
        self._mutex = threading.Lock()
        self._check_in_flight = False
        self._state: Dict[str, Any] = {
            "channel": "stable",
            "capability": self._cap["capability"],
            "capability_reason": self._cap["reason"],
            "current_version": current_version,
            "last_checked_at": None,
            "status": "idle",
            "available_version": None,
            "release_url": None,
            "asset_name": None,
            "progress_percent": None,
            "error": None,
        }

    # ── state plumbing ────────────────────────────────────────────────────

    def state(self) -> Dict[str, Any]:
        with self._mutex:
            return dict(self._state)

    def _publish(self, patch: Dict[str, Any]) -> None:
        with self._mutex:
            self._state.update(patch)

    # ── check ─────────────────────────────────────────────────────────────

    def check(self) -> Dict[str, Any]:
        """Run one check. Concurrent checks are dropped (the launch check
        racing a user's 'Check now' would interleave state transitions)."""
        if self._check_in_flight:
            return self.state()
        self._check_in_flight = True
        try:
            self._publish({"status": "checking", "error": None})
            self._run_check()
            return self.state()
        finally:
            self._check_in_flight = False

    def _run_check(self) -> None:
        try:
            settings = read_update_settings(self.settings_file)
            channel = settings["channel"]
            releases = self._fetch_releases(channel)
            picked = pick_release_for_channel(
                releases, channel
            )
            now = time.time() * 1000
            write_update_settings(
                self.settings_file, {"channel": channel, "last_checked_at": now}
            )
            patch: Dict[str, Any] = {"channel": channel, "last_checked_at": now}
            if picked and is_notifiable_update(self.current_version, picked):
                patch.update(
                    status="available",
                    available_version=picked["version"],
                    release_url=picked["url"],
                    asset_name=asset_for_platform(picked, self.platform),
                    progress_percent=None,
                    error=None,
                )
            else:
                patch.update(
                    status="up-to-date",
                    available_version=None,
                    release_url=None,
                    asset_name=None,
                    progress_percent=None,
                    error=None,
                )
            self._publish(patch)

        except Exception as e:  # noqa: BLE001 — a failed check is not fatal
            logger.warning("Update check failed: %s", e)
            self._publish(
                {"status": "error", "progress_percent": None, "error": str(e)}
            )

    def _fetch_releases(self, channel: str) -> list:
        import json as _json

        releases: list = []
        for page in range(1, 4):
            req = urllib.request.Request(
                f"{RELEASES_API}?per_page={RELEASES_PER_PAGE}&page={page}",
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": f"api-printer-service/{self.current_version}",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status != 200:
                    raise UpdateError(f"GitHub returned {resp.status}")
                body = _json.load(resp)
            if not isinstance(body, list):
                raise UpdateError("unexpected releases payload")
            for raw in body:
                from update_feed import parse_release

                parsed = parse_release(raw)
                if parsed is None or parsed["draft"]:
                    continue
                if channel == "stable" and parsed["prerelease"]:
                    continue
                releases.append(parsed)
            if len(body) < RELEASES_PER_PAGE:
                break
        return releases

    # ── install (capability 'auto' / Windows only) ────────────────────────

    def install(self) -> Dict[str, Any]:
        """Download the installer and run it silently (Windows).

        The NSIS installer re-registers the scheduled task: it stops this
        service, replaces the files and starts the new version. From this
        process's point of view that is simply death mid-request — the HTTP
        response is sent first, and the caller must treat a successful
        'installing' state as 'goodbye'.
        """
        # The out-of-service updater (updater_cli.py scheduled task) shares
        # this lock: two installers racing the same machine is the one
        # failure mode that must be impossible.
        with update_lock(self.settings_file) as acquired:
            if not acquired:
                raise UpdateError("another updater is already working")
            return self._install_locked()

    def _install_locked(self) -> Dict[str, Any]:
        st = self.state()
        if self._cap["capability"] != "auto":
            raise UpdateError("auto-install is not available on this platform")
        if st["status"] not in ("available",):
            raise UpdateError(f"nothing to install (status: {st['status']})")
        if not st["asset_name"]:
            raise UpdateError("release has no installer asset for this platform")
        if not st["release_url"] or not is_release_page_url(st["release_url"]):
            raise UpdateError("release URL failed validation")

        self._publish({"status": "downloading", "progress_percent": 0, "error": None})
        logger.info(
            "Downloading update %s (%s) from %s",
            st["available_version"], st["asset_name"], st["release_url"],
        )
        try:
            path = self._download_asset(st)
        except Exception as e:  # noqa: BLE001
            logger.warning("Update download failed: %s", e)
            self._publish(
                {"status": "available", "progress_percent": None, "error": f"download failed: {e}"}
            )
            raise UpdateError(f"download failed: {e}") from e

        self._publish({"status": "installing", "progress_percent": 100})
        logger.info("Launching silent installer %s — the service will be stopped by it", path)
        try:
            proc = self._launch_installer(path)
        except OSError as e:
            # A downloaded exe Windows refuses to execute (corrupt download,
            # AV quarantine, wrong arch) used to escape as an unhandled
            # OSError — the endpoint answered a bare text/plain 500 that the
            # GUI rendered as the useless 'HTTP Error 500'. Any launch
            # failure must be an UpdateError so the caller gets a real
            # message and can retry.
            logger.warning("Could not launch installer %s: %s", path, e)
            self._publish({
                "status": "available",
                "progress_percent": None,
                "error": f"could not launch the installer: {e}",
            })
            raise UpdateError(
                f"could not launch the downloaded installer: {e} "
                "(antivirus may have quarantined it) — try again"
            ) from e
        # The installer normally ends this process (it stops the service
        # first), and the watchdog thread dies with it — that is fine. The
        # watchdog only matters when the installer FAILS or HANGS BEFORE
        # stopping the service: then someone must move the state out of
        # 'installing', or the UI is stuck on it forever.
        threading.Thread(
            target=self._watch_installer, args=(proc,), daemon=True
        ).start()
        return self.state()

    def _download_asset(self, st: Dict[str, Any]) -> str:
        """Stream the installer asset to a temp file. The URL is derived from
        the release page URL we validated, NOT from asset JSON: the API's
        browser_download_url is untrusted and could point anywhere."""
        import re as _re

        m = _re.match(
            r"^https://github\.com/%s/releases/tag/v(.+)$" % _re.escape(REPO_SLUG),
            st["release_url"] or "",
        )
        if not m:
            raise UpdateError("cannot derive download URL from release page")
        version = m.group(1)
        url = (
            f"https://github.com/{REPO_SLUG}/releases/download/"
            f"v{version}/{st['asset_name']}"
        )
        req = urllib.request.Request(
            url,
            headers={"User-Agent": f"api-printer-service/{self.current_version}"},
        )
        fd, path = tempfile.mkstemp(prefix="api-printer-update-", suffix=".exe")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp, os.fdopen(fd, "wb") as f:
                if resp.status != 200:
                    raise UpdateError(f"download returned {resp.status}")
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

    def _launch_installer(self, path: str) -> "subprocess.Popen":
        import sys

        # /S = silent NSIS install. DETACHED + breakaway so the installer
        # outlives this process, which it is about to kill and replace.
        flags = 0
        if sys.platform == "win32":
            flags = 0x00000008 | 0x00000200 | 0x01000000  # DETACHED|NEW_GROUP|BREAKAWAY
        return subprocess.Popen(
            [path, "/S"],
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )

    def _watch_installer(self, proc: "subprocess.Popen") -> None:
        """Wait for the silent installer and publish the outcome.

        'installing' must never be a terminal state: a failed installer
        (file locked, admin policy, NSIS error) previously left the state
        there indefinitely with the old version still running and no error
        anywhere. Now: failure → back to 'available' with the exit code so
        the user can retry; hang → 'error' after the timeout; success →
        'ready' (usually moot, since a successful installer has already
        stopped this service by then)."""
        try:
            code = proc.wait(timeout=INSTALLER_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            logger.warning(
                "Update installer still running after %s min — publishing "
                "timeout error", INSTALLER_TIMEOUT_S // 60,
            )
            self._publish({
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
            logger.info("Update installer finished (exit 0)")
            self._publish({
                "status": "ready",
                "progress_percent": 100,
                "error": None,
            })
            return
        logger.warning("Update installer exited with code %s", code)
        self._publish({
            "status": "available",
            "progress_percent": None,
            "error": (
                f"the installer exited with code {code} and the update was "
                "not applied — the previous version is still running; "
                "press Install update to retry"
            ),
        })

    # ── channel ───────────────────────────────────────────────────────────

    def set_channel(self, channel: Any) -> Dict[str, Any]:
        """Renderer/HTTP input is validated here, not trusted."""
        if not is_update_channel(channel):
            raise ValueError("invalid channel")
        settings = read_update_settings(self.settings_file)
        if channel == settings["channel"]:
            return self.state()
        # Switching ring is an explicit act — check immediately rather than
        # leaving the user on the other ring for up to a day.
        write_update_settings(
            self.settings_file, {"channel": channel, "last_checked_at": None}
        )
        self._publish(
            {
                "channel": channel,
                "last_checked_at": None,
                "status": "idle",
                "available_version": None,
                "release_url": None,
                "asset_name": None,
                "progress_percent": None,
                "error": None,
            }
        )
        return self.check()

    # ── scheduler ─────────────────────────────────────────────────────────

    def start(self, first_delay_s: float = FIRST_CHECK_DELAY_S) -> None:
        t = threading.Timer(
            first_delay_s, self._scheduled_check
        )
        t.daemon = True
        t.start()
        self._timer = t

    def _scheduled_check(self) -> None:
        try:
            settings = read_update_settings(self.settings_file)
            if is_check_due(settings["last_checked_at"], time.time() * 1000):
                self.check()
        except Exception:  # noqa: BLE001 — never take the service down
            logger.exception("scheduled update check failed")
        # Re-arm; the 24h decision lives in is_check_due so sleep-wake still
        # checks on the first wake after the interval has passed.
        import threading

        self._timer = threading.Timer(TIMER_TICK_S, self._scheduled_check)
        self._timer.daemon = True
        self._timer.start()

    def stop(self) -> None:
        if self._timer:
            self._timer.cancel()


_singleton: Optional[Updater] = None


def init_updater(current_version: str, settings_file: str) -> Updater:
    """Create and start the singleton updater. Failing to set updates up must
    not abort boot — the caller is expected to catch exceptions."""
    global _singleton
    from service_controller import platform_name

    _singleton = Updater(current_version, settings_file, platform_name())
    _singleton.start()
    return _singleton


def get_updater() -> Optional[Updater]:
    return _singleton
