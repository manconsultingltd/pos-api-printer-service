"""State-machine tests for updater.Updater — network is faked out."""

import pytest

import updater as updater_mod
from updater import Updater, resolve_capability

SLUG = "manconsultingltd/pos-api-printer-service"


def make_updater(platform="windows", current="1.1.50", settings_file="/tmp/x.json"):
    u = Updater(current, settings_file, platform)
    u._mutex = __import__("threading").Lock()
    return u


def fake_releases(u, versions):
    def _fetch(channel):
        return [
            {"version": v, "url": f"https://github.com/{SLUG}/releases/tag/v{v}",
             "draft": False, "prerelease": False,
             "assets": ["api-printer-setup-windows.exe"]}
            for v in versions
        ]
    u._fetch_releases = _fetch


class TestCapability:
    def test_windows_auto(self):
        assert resolve_capability("windows", "1.1.5")["capability"] == "auto"

    def test_linux_notify_with_reason(self):
        cap = resolve_capability("linux", "1.1.5")
        assert cap["capability"] == "notify"
        assert cap["reason"]

    def test_mac_notify(self):
        assert resolve_capability("darwin", "1.1.5")["capability"] == "notify"


class TestCheck:
    def test_newer_available(self):
        u = make_updater()
        fake_releases(u, ["1.1.50", "1.1.51"])
        u._run_check()
        s = u.state()
        assert s["status"] == "available"
        assert s["available_version"] == "1.1.51"
        assert s["asset_name"] == "api-printer-setup-windows.exe"

    def test_up_to_date(self):
        u = make_updater(current="1.1.51")
        fake_releases(u, ["1.1.50"])
        u._run_check()
        assert u.state()["status"] == "up-to-date"

    def test_downgrade_never_offered(self):
        u = make_updater(current="1.1.60")
        fake_releases(u, ["1.1.50"])
        u._run_check()
        assert u.state()["status"] == "up-to-date"

    def test_network_error_is_error_state(self):
        u = make_updater()

        def boom(channel):
            raise RuntimeError("GitHub returned 503")
        u._fetch_releases = boom
        u._run_check()
        s = u.state()
        assert s["status"] == "error"
        assert "503" in s["error"]

    def test_check_marks_last_checked(self):
        u = make_updater()
        fake_releases(u, [])
        u.check()
        assert u.state()["last_checked_at"] is not None
        assert u.state()["status"] == "up-to-date"


class TestInstallGuards:
    def test_notify_platform_rejected(self):
        u = make_updater(platform="linux")
        with pytest.raises(updater_mod.UpdateError):
            u.install()

    def test_nothing_to_install(self):
        u = make_updater()
        u._publish({"status": "up-to-date"})
        with pytest.raises(updater_mod.UpdateError):
            u.install()
