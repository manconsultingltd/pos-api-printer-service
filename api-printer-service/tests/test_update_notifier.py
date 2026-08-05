"""Tests for update_notifier — version parsing, daily gate, notify flow."""

import json

import pytest

import update_notifier


@pytest.fixture(autouse=True)
def state_file(tmp_path, monkeypatch):
    path = tmp_path / "update_check.json"
    monkeypatch.setenv("API_PRINTER_UPDATE_STATE_FILE", str(path))
    return path


@pytest.fixture
def notifications(monkeypatch):
    sent = []
    monkeypatch.setattr(
        update_notifier, "notify", lambda title, msg: sent.append((title, msg)) or True
    )
    return sent


# ---- parse_version / is_newer ----------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1.1.23", (1, 1, 23)),
        ("v1.1.23", (1, 1, 23)),
        ("1.0", (1, 0)),
        ("0.0.0-dev", None),
        ("", None),
        (None, None),
        ("garbage", None),
    ],
)
def test_parse_version(text, expected):
    assert update_notifier.parse_version(text) == expected


@pytest.mark.parametrize(
    "latest,current,newer",
    [
        ((1, 1, 24), (1, 1, 23), True),
        ((1, 1, 23), (1, 1, 23), False),
        ((1, 1, 22), (1, 1, 23), False),
        ((1, 2), (1, 1, 99), True),
        ((1, 1), (1, 1, 0), False),  # padding: equal
    ],
)
def test_is_newer(latest, current, newer):
    assert update_notifier.is_newer(latest, current) is newer


# ---- check_once -------------------------------------------------------------


def test_notifies_when_newer(monkeypatch, notifications, state_file):
    monkeypatch.setattr(update_notifier, "fetch_latest_version", lambda: "1.1.24")

    msg = update_notifier.check_once(lambda: "1.1.23", today="2026-08-04")

    assert msg is not None and "1.1.24" in msg
    assert len(notifications) == 1
    state = json.loads(state_file.read_text())
    assert state == {
        "last_check": "2026-08-04",
        "current_version": "1.1.23",
        "latest_version": "1.1.24",
    }


def test_no_notification_when_up_to_date(monkeypatch, notifications, state_file):
    monkeypatch.setattr(update_notifier, "fetch_latest_version", lambda: "1.1.23")

    assert update_notifier.check_once(lambda: "1.1.23", today="2026-08-04") is None
    assert notifications == []
    # Check still consumed today's slot.
    assert json.loads(state_file.read_text())["last_check"] == "2026-08-04"


def test_checks_and_notifies_once_per_day(monkeypatch, notifications):
    calls = []

    def fetch():
        calls.append(1)
        return "1.1.24"

    monkeypatch.setattr(update_notifier, "fetch_latest_version", fetch)

    assert update_notifier.check_once(lambda: "1.1.23", today="2026-08-04")
    assert update_notifier.check_once(lambda: "1.1.23", today="2026-08-04") is None
    assert len(calls) == 1  # second same-day call never hit the API
    assert len(notifications) == 1

    # A new day checks (and notifies) again.
    assert update_notifier.check_once(lambda: "1.1.23", today="2026-08-05")
    assert len(calls) == 2
    assert len(notifications) == 2


@pytest.mark.parametrize("current", [None, "0.0.0-dev"])
def test_skips_without_consuming_slot_when_service_unavailable_or_dev(
    monkeypatch, notifications, state_file, current
):
    monkeypatch.setattr(update_notifier, "fetch_latest_version", lambda: "1.1.24")

    assert update_notifier.check_once(lambda: current, today="2026-08-04") is None
    assert notifications == []
    assert not state_file.exists()  # slot not consumed — next poll retries


def test_fetch_failure_leaves_slot_open(monkeypatch, notifications, state_file):
    monkeypatch.setattr(update_notifier, "fetch_latest_version", lambda: None)

    assert update_notifier.check_once(lambda: "1.1.23", today="2026-08-04") is None
    assert not state_file.exists()

    # API back up on a later poll the same day: check proceeds.
    monkeypatch.setattr(update_notifier, "fetch_latest_version", lambda: "1.1.24")
    assert update_notifier.check_once(lambda: "1.1.23", today="2026-08-04")
    assert len(notifications) == 1


def test_get_current_version_exception_is_swallowed(monkeypatch, notifications):
    monkeypatch.setattr(update_notifier, "fetch_latest_version", lambda: "1.1.24")

    def boom():
        raise ConnectionError("service down")

    assert update_notifier.check_once(boom, today="2026-08-04") is None
    assert notifications == []
