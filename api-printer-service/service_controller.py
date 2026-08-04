"""
Platform-aware stop / restart helpers.

Used by main.py's /api/service/* endpoints to tear down the service from
within itself — the service runs as root (Linux systemd, macOS LaunchDaemon)
or SYSTEM (Windows Scheduled Task), so it has the privileges needed to
manage its own unit without elevation.

Stop / restart spawn a **detached** helper process that waits a short
moment and then issues the platform command. This gives the HTTP response
time to drain before the current process is torn down.
"""

from __future__ import annotations

import subprocess
import sys

_IS_WINDOWS = sys.platform == "win32"
_IS_MAC = sys.platform == "darwin"

LINUX_UNIT = "api-printer.service"
MAC_PLIST = "/Library/LaunchDaemons/com.posawesome.api-printer.plist"
MAC_TARGET = "system/com.posawesome.api-printer"
WINDOWS_TASK = "API Printer Service"


def platform_name() -> str:
    if _IS_WINDOWS:
        return "windows"
    if _IS_MAC:
        return "macos"
    return "linux"


def stop_async(delay_s: float = 1.5) -> None:
    """Spawn a detached helper that stops the service after `delay_s` seconds."""
    _spawn_for_verb("stop", delay_s)


def restart_async(delay_s: float = 1.5) -> None:
    """Spawn a detached helper that restarts the service after `delay_s` seconds."""
    _spawn_for_verb("restart", delay_s)


def _spawn_for_verb(verb: str, delay_s: float) -> None:
    if _IS_WINDOWS:
        _spawn_windows(verb, delay_s)
    elif _IS_MAC:
        _spawn_mac(verb, delay_s)
    else:
        _spawn_linux(verb, delay_s)


def _spawn_windows(verb: str, delay_s: float) -> None:
    if verb == "stop":
        inner = f'schtasks /End /TN "{WINDOWS_TASK}"'
    else:
        # /End + pause + /Run. The pause gives schtasks time to flip the
        # task status to Ready before /Run, otherwise /Run refuses with
        # "the task is currently running".
        inner = (
            f'schtasks /End /TN "{WINDOWS_TASK}" >nul 2>&1 & '
            f'timeout /t 2 /nobreak >nul & '
            f'schtasks /Run /TN "{WINDOWS_TASK}"'
        )
    full = f'timeout /t {int(max(1, delay_s))} /nobreak >nul & {inner}'

    # DETACHED_PROCESS keeps the helper alive after our process dies;
    # CREATE_NEW_PROCESS_GROUP prevents Ctrl-C propagation;
    # CREATE_BREAKAWAY_FROM_JOB lets the helper escape Task Scheduler's Job
    # Object — without it, schtasks /End on us would kill the helper too.
    DETACHED = 0x00000008
    NEW_GROUP = 0x00000200
    BREAKAWAY = 0x01000000
    flags = DETACHED | NEW_GROUP | BREAKAWAY
    try:
        subprocess.Popen(
            ["cmd", "/c", full],
            creationflags=flags,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        # Fall back without BREAKAWAY for Job Objects that do not allow it;
        # the helper may then die with us, but stop/restart is best-effort.
        subprocess.Popen(
            ["cmd", "/c", full],
            creationflags=DETACHED | NEW_GROUP,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _spawn_mac(verb: str, delay_s: float) -> None:
    if verb == "stop":
        inner = f'launchctl unload "{MAC_PLIST}"'
    else:
        # kickstart -k: kill-and-restart; works with KeepAlive=true.
        inner = f"launchctl kickstart -k {MAC_TARGET}"
    full = f"sleep {delay_s}; exec {inner}"
    subprocess.Popen(
        ["sh", "-c", full],
        start_new_session=True,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _spawn_linux(verb: str, delay_s: float) -> None:
    inner = (
        f"systemctl stop {LINUX_UNIT}"
        if verb == "stop"
        else f"systemctl restart {LINUX_UNIT}"
    )
    # On systemd, a detached child still lives in our cgroup and gets
    # killed when systemd tears our unit down — before it can fire
    # systemctl. `systemd-run` launches the helper in its own transient
    # unit so it survives the very restart it is about to issue.
    subprocess.Popen(
        [
            "systemd-run", "--quiet",
            "--description", f"API Printer Service {verb} helper",
            "/bin/sh", "-c", f"sleep {delay_s}; exec {inner}",
        ],
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
