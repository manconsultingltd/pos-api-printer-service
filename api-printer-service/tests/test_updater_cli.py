"""Tests for the standalone updater worker (updater_cli.py) and the shared
install lock. Network and installer launch are faked out."""

import json
import os
import time

import pytest

import updater_cli
from update_settings import read_update_settings, write_update_settings, update_lock

SLUG = "manconsultingltd/pos-api-printer-service"


def make_release(version, *, prerelease=False, asset="api-printer-setup-windows.exe"):
    return {
        "version": version,
        "url": f"https://github.com/{SLUG}/releases/tag/v{version}",
        "draft": False,
        "prerelease": prerelease,
        "assets": [asset],
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    settings = str(tmp_path / "settings.json")
    monkeypatch.setenv("API_PRINTER_SETTINGS_FILE", settings)
    monkeypatch.setattr(updater_cli, "VERSION", "1.1.50")
    return settings


def state(env):
    path = updater_cli._state_file(env)
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def feed(monkeypatch, releases):
    monkeypatch.setattr(updater_cli, "collect_releases", lambda ch: releases)


@pytest.fixture
def windows(monkeypatch):
    """Install paths are Windows-only; force the platform fact."""
    monkeypatch.setattr(updater_cli, "platform_name", lambda: "windows")


class TestScheduledThrottle:
    def test_recent_check_skips_network(self, env, monkeypatch):
        write_update_settings(env, {"channel": "stable", "last_checked_at": time.time() * 1000})

        def boom(channel):
            raise AssertionError("network must not be touched")
        monkeypatch.setattr(updater_cli, "collect_releases", boom)
        assert updater_cli.run(env) == 0

    def test_check_now_bypasses_throttle(self, env, monkeypatch):
        write_update_settings(env, {"channel": "stable", "last_checked_at": time.time() * 1000})
        feed(monkeypatch, [])
        assert updater_cli.run(env, check_now=True) == 0
        assert state(env)["status"] == "up-to-date"


class TestCheck:
    def test_newer_available(self, env, monkeypatch, windows):
        feed(monkeypatch, [make_release("1.1.51")])
        updater_cli.run(env, check_now=True)
        s = state(env)
        assert s["status"] == "available"
        assert s["available_version"] == "1.1.51"
        assert s["capability"] == "auto"

    def test_up_to_date(self, env, monkeypatch):
        feed(monkeypatch, [make_release("1.1.50")])
        updater_cli.run(env, check_now=True)
        assert state(env)["status"] == "up-to-date"

    def test_downgrade_never_offered(self, env, monkeypatch):
        monkeypatch.setattr(updater_cli, "VERSION", "1.1.60")
        feed(monkeypatch, [make_release("1.1.50")])
        updater_cli.run(env, check_now=True)
        assert state(env)["status"] == "up-to-date"

    def test_network_error_state(self, env, monkeypatch):
        def boom(channel):
            raise RuntimeError("GitHub returned 503")
        monkeypatch.setattr(updater_cli, "collect_releases", boom)
        updater_cli.run(env, check_now=True)
        s = state(env)
        assert s["status"] == "error"
        assert "503" in s["error"]

    def test_throttle_persisted_for_both_updaters(self, env, monkeypatch):
        feed(monkeypatch, [])
        updater_cli.run(env, check_now=True)
        assert read_update_settings(env)["last_checked_at"] is not None


class TestInstall:
    def test_windows_installs_silently(self, env, monkeypatch, windows):
        downloaded = []
        launched = []
        monkeypatch.setattr(updater_cli, "_download",
                            lambda url, asset: downloaded.append((url, asset)) or "/tmp/x.exe")
        monkeypatch.setattr(updater_cli, "_launch_installer_windows",
                            lambda p: launched.append(p))
        feed(monkeypatch, [make_release("1.1.51")])
        assert updater_cli.run(env, install_now=True) == 0
        assert downloaded and launched == ["/tmp/x.exe"]
        assert state(env)["status"] == "installing"

    def test_no_asset_no_install(self, env, monkeypatch, windows):
        launched = []
        monkeypatch.setattr(updater_cli, "_launch_installer_windows",
                            lambda p: launched.append(p))
        feed(monkeypatch, [make_release("1.1.51", asset="other.bin")])
        updater_cli.run(env, install_now=True)
        assert not launched
        assert state(env)["status"] == "available"

    def test_download_failure_keeps_available(self, env, monkeypatch, windows):
        def boom(url, asset):
            raise RuntimeError("connection reset")
        monkeypatch.setattr(updater_cli, "_download", boom)
        feed(monkeypatch, [make_release("1.1.51")])
        assert updater_cli.run(env, install_now=True) == 1
        s = state(env)
        assert s["status"] == "available"
        assert "connection reset" in s["error"]


class TestNotifyPlatform:
    def test_linux_never_installs(self, env, monkeypatch):
        launched = []
        monkeypatch.setattr(updater_cli, "_launch_installer_windows",
                            lambda p: launched.append(p))
        feed(monkeypatch, [make_release("1.1.51")])
        assert updater_cli.run(env, install_now=True) == 0
        assert not launched
        assert state(env)["status"] == "available"
        assert state(env)["capability"] == "notify"


class TestLock:
    def test_lock_blocks_second_updater(self, env, monkeypatch):
        feed(monkeypatch, [make_release("1.1.51")])
        with update_lock(env) as acquired:
            assert acquired is True
            assert updater_cli.run(env, install_now=True) == 0
            # No check ran — the network was never touched (state file empty).
            assert state(env) == {}

    def test_stale_lock_is_broken(self, env, monkeypatch):
        lock_path = os.path.join(os.path.dirname(env), "update.lock")
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        with open(lock_path, "w") as f:
            f.write("dead")
        old = time.time() - 7200
        os.utime(lock_path, (old, old))
        feed(monkeypatch, [make_release("1.1.51")])
        updater_cli.run(env, check_now=True)
        assert state(env)["status"] == "available"

    def test_lock_released_after_run(self, env, monkeypatch):
        feed(monkeypatch, [])
        updater_cli.run(env, check_now=True)
        assert not os.path.exists(os.path.join(os.path.dirname(env), "update.lock"))
