"""Tests for the update ring preference: corrupt input must degrade,
never throw."""

import json


from update_settings import (
    CHECK_INTERVAL_MS,
    DEFAULT_CHANNEL,
    is_check_due,
    is_newer_version,
    normalize_update_settings,
    read_update_settings,
    write_update_settings,
)


class TestNormalize:
    def test_none_gives_defaults(self):
        s = normalize_update_settings(None)
        assert s["channel"] == DEFAULT_CHANNEL
        assert s["last_checked_at"] is None

    def test_garbage_gives_defaults(self):
        for raw in ("garbage", 42, [], {"channel": 7, "last_checked_at": "no"}):
            s = normalize_update_settings(raw)
            assert s["channel"] == "stable"
            assert s["last_checked_at"] is None

    def test_valid_roundtrip(self):
        s = normalize_update_settings({"channel": "preview", "last_checked_at": 123.5})
        assert s == {"channel": "preview", "last_checked_at": 123.5}

    def test_bad_last_checked_does_not_discard_channel(self):
        s = normalize_update_settings({"channel": "preview", "last_checked_at": "x"})
        assert s["channel"] == "preview"
        assert s["last_checked_at"] is None


class TestFileIO:
    def test_missing_file_gives_defaults(self, tmp_path):
        assert read_update_settings(str(tmp_path / "nope.json"))["channel"] == DEFAULT_CHANNEL

    def test_corrupt_file_gives_defaults(self, tmp_path):
        f = tmp_path / "u.json"
        f.write_text("{not json", encoding="utf-8")
        assert read_update_settings(str(f))["channel"] == DEFAULT_CHANNEL

    def test_write_read_roundtrip(self, tmp_path):
        f = str(tmp_path / "u.json")
        write_update_settings(f, {"channel": "preview", "last_checked_at": 99.0})
        assert read_update_settings(f)["channel"] == "preview"

    def test_write_is_atomic_shape(self, tmp_path):
        f = str(tmp_path / "u.json")
        write_update_settings(f, {"channel": "stable", "last_checked_at": 1.0})
        with open(f, encoding="utf-8") as fh:
            data = json.load(fh)
        assert data["channel"] == "stable"


class TestIsCheckDue:
    def test_never_checked(self):
        assert is_check_due(None, 1000.0) is True

    def test_within_interval(self):
        assert is_check_due(1000.0 - CHECK_INTERVAL_MS / 2, 1000.0) is False

    def test_after_interval(self):
        assert is_check_due(1000.0 - CHECK_INTERVAL_MS - 1, 1000.0) is True

    def test_clock_jumped_backwards(self):
        assert is_check_due(5000.0, 1000.0) is True


class TestSemver:
    def test_newer(self):
        assert is_newer_version("1.1.51", "1.1.50") is True
        assert is_newer_version("1.2.0", "1.1.99") is True
        assert is_newer_version("1.1.50", "1.1.50") is False
        assert is_newer_version("1.1.49", "1.1.50") is False

    def test_non_semver_never(self):
        assert is_newer_version("banana", "1.0.0") is False
        assert is_newer_version("1.0.0", "banana") is False
