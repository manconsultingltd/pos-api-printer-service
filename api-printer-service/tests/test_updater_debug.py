"""Verbose-download (debug) toggle for the updater."""
import os
import tempfile

import pytest

import updater
from update_settings import normalize_update_settings, read_update_settings
from updater import Updater


def test_normalize_debug_defaults_off():
    assert normalize_update_settings(None)["debug"] is False
    assert normalize_update_settings({"debug": True})["debug"] is True
    assert normalize_update_settings({"debug": "yes"})["debug"] is True  # truthy
    assert normalize_update_settings({"debug": 0})["debug"] is False


def test_state_includes_debug_flag(tmp_path):
    f = str(tmp_path / "settings.json")
    u = Updater("1.1.0", f, "windows")
    assert u.state()["debug"] is False
    u.set_debug(True)
    assert u.state()["debug"] is True
    u.set_debug(False)
    assert u.state()["debug"] is False


def test_set_debug_persists_across_instances(tmp_path):
    f = str(tmp_path / "settings.json")
    Updater("1.1.0", f, "windows").set_debug(True)
    # A NEW updater (e.g. after service restart) must see the persisted flag.
    assert read_update_settings(f)["debug"] is True
    assert Updater("1.1.0", f, "windows").debug_enabled() is True


def test_debug_log_capped_and_logged_on_failure(monkeypatch, tmp_path):
    f = str(tmp_path / "settings.json")
    u = Updater("1.1.0", f, "windows")
    u.set_debug(True)

    class Fail:
        def __enter__(self):
            raise OSError("network down")

        def __exit__(self, *a):
            return False

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Fail())
    # _download_asset re-raises the raw error; _install_locked wraps it
    # into an UpdateError('download failed: ...') for the endpoint.
    with pytest.raises(OSError):
        u._download_asset({
            "available_version": "1.1.28",
            "release_url": "https://github.com/manconsultingltd/"
                           "pos-api-printer-service/releases/tag/v1.1.28",
            "asset_name": "api-printer-setup-windows.exe",
        })
    assert any("GET https://github.com" in x for x in u.state()["debug_log"])
    assert any("download FAILED" in x for x in u.state()["debug_log"])


def test_download_publishes_url_and_bytes_when_debug(monkeypatch, tmp_path):
    f = str(tmp_path / "settings.json")
    u = Updater("1.1.0", f, "windows")
    u.set_debug(True)

    class Resp:
        status = 200

        def __init__(self):
            self.headers = {"Content-Length": "8"}
            self._data = [b"12345678", b""]
            self._i = 0

        def geturl(self):
            return "https://objects.githubusercontent.com/signed-thing"

        def read(self, n=-1):
            out = self._data[self._i]
            self._i = min(self._i + 1, len(self._data) - 1)
            return out

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Resp())
    path = u._download_asset({
        "available_version": "1.1.28",
        "release_url": "https://github.com/manconsultingltd/"
                       "pos-api-printer-service/releases/tag/v1.1.28",
        "asset_name": "api-printer-setup-windows.exe",
    })
    st = u.state()
    assert st["download_url"].startswith("https://github.com/manconsultingltd/")
    assert st["download_bytes"] == 8
    assert any("final URL: https://objects.githubusercontent.com" in x
               for x in st["debug_log"])
    assert any("Content-Length: 8" in x for x in st["debug_log"])
    os.unlink(path)
