"""Copies setting: persisted 1-3, default 1, applied to print jobs."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main
from config import Config


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    return TestClient(main.app)


def test_copies_default_is_one(client):
    assert client.get("/api/settings").json()["copies"] == 1


def test_copies_persisted(client):
    assert client.put("/api/settings", json={"copies": 3}).json()["copies"] == 3
    assert client.get("/api/settings").json()["copies"] == 3


@pytest.mark.parametrize("bad", [0, 4])
def test_copies_out_of_range_rejected(client, bad):
    assert client.put("/api/settings", json={"copies": bad}).status_code == 422


def test_print_html_repeats_receipt(client, monkeypatch):
    sent = {}
    monkeypatch.setattr(main.printer_manager, "test_printer", lambda name: True)
    monkeypatch.setattr(
        main.printer_manager, "print_raw_bytes",
        lambda printer_name, data, job_title: sent.setdefault("data", data) and 1,
    )
    client.put("/api/settings", json={"copies": 2})
    html = (Path(__file__).resolve().parent.parent / "example_print_format.html").read_text()
    client.post("/api/print-html", json={"printer": "P", "html": html})
    single = main.ESCPOSGenerator(paper_width=58).generate_receipt(
        main.HtmlReceiptParser().parse(html)
    )
    assert sent["data"] == main.ESCPOSGenerator.cash_drawer_pulse() + single * 2


def test_each_copy_ends_with_cut(client, monkeypatch):
    sent = {}
    monkeypatch.setattr(main.printer_manager, "test_printer", lambda name: True)
    monkeypatch.setattr(
        main.printer_manager, "print_raw_bytes",
        lambda printer_name, data, job_title: sent.setdefault("data", data) and 1,
    )
    html = (Path(__file__).resolve().parent.parent / "example_print_format.html").read_text()
    client.post("/api/print-html", json={"printer": "P", "html": html, "copies": 3})
    cut = main.ESCPOSGenerator.CUT_PARTIAL
    assert sent["data"].count(cut) == 3
    assert sent["data"].endswith(cut)
