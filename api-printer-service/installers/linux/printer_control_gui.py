#!/usr/bin/env python3
"""
API Printer Service — Control Panel (GTK3 + AppIndicator).

Works on XFCE4 (native) and on GNOME 40/50 with the "AppIndicator and
KStatusNotifierItem Support" extension (preinstalled on Ubuntu; on
Debian install `gnome-shell-extension-appindicator`).

All HTTP/logic is in PrinterServiceClient so the same module can later
be reused on Windows/macOS by swapping only the Gtk UI layer.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from typing import Any, Callable
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

# AppIndicator: prefer the maintained Ayatana fork, fall back to the
# older libappindicator3.  On distros with neither the window still
# works — only the tray icon is missing.
_INDICATOR_LIB: str | None = None
AppIndicator: Any = None
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402
    _INDICATOR_LIB = "AyatanaAppIndicator3"
except (ValueError, ImportError):
    try:
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3 as AppIndicator  # noqa: E402
        _INDICATOR_LIB = "AppIndicator3"
    except (ValueError, ImportError):
        AppIndicator = None

API_URL = os.environ.get("API_PRINTER_URL", "http://127.0.0.1:5058")
# Release pages are the only URL this GUI may hand to the OS browser.
RELEASE_URL_PREFIX = (
    "https://github.com/manconsultingltd/pos-api-printer-service/releases/"
)
APP_ID = "api-printer-service"
APP_NAME = "API Printer Service"
SERVICE_UNIT = "api-printer.service"
DEFAULT_PAPER_WIDTH = 58
REFRESH_INTERVAL_SECONDS = 10

# Map systemd ActiveState values to (icon colour, human-readable label).
SERVICE_STATE_LABELS: dict[str, tuple[str, str]] = {
    "active":       ("#27ae60", "Running"),
    "reloading":    ("#c9a227", "Reloading…"),
    "activating":   ("#c9a227", "Starting…"),
    "deactivating": ("#c9a227", "Stopping…"),
    "inactive":     ("#666666", "Stopped"),
    "failed":       ("#c0392b", "Failed"),
    "unknown":      ("#c0392b", "Unknown"),
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  %(message)s",
)
log = logging.getLogger("api-printer-gui")


# =============================================================================
# Service client — pure HTTP, no UI dependencies.  Reusable on Win/Mac.
# =============================================================================


class PrinterServiceClient:
    """Thin REST client for the local API Printer Service."""

    def __init__(self, base_url: str = API_URL, timeout: float = 3.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, payload: dict | None = None) -> Any:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        req = urlrequest.Request(url, data=data, method=method, headers=headers)
        with urlrequest.urlopen(req, timeout=self.timeout) as resp:
            body = resp.read()
            return json.loads(body) if body else None

    def health(self) -> dict | None:
        try:
            return self._request("GET", "/health")
        except (HTTPError, URLError, TimeoutError, ConnectionError, OSError):
            return None

    def list_printers(self) -> list[dict]:
        try:
            data = self._request("GET", "/api/printers") or {}
            return list(data.get("printers", []))
        except (HTTPError, URLError, TimeoutError, ConnectionError, OSError) as e:
            log.warning("list_printers failed: %s", e)
            return []

    def get_settings(self) -> dict:
        try:
            data = self._request("GET", "/api/settings") or {}
            return dict(data)
        except (HTTPError, URLError, TimeoutError, ConnectionError, OSError) as e:
            log.warning("get_settings failed: %s", e)
            return {}

    def set_default_printer(self, name: str) -> bool:
        try:
            self._request("PUT", "/api/settings", {"default_printer": name})
            return True
        except (HTTPError, URLError, TimeoutError, ConnectionError, OSError) as e:
            log.error("set_default_printer failed: %s", e)
            return False

    def test_print(self, printer: str, paper_width: int = DEFAULT_PAPER_WIDTH) -> dict:
        try:
            return self._request(
                "POST", "/api/test-print",
                {"printer": printer, "paper_width": paper_width},
            ) or {}
        except HTTPError as e:
            try:
                detail = json.loads(e.read()).get("detail", str(e))
            except Exception:
                detail = str(e)
            return {"success": False, "error": detail}
        except (URLError, TimeoutError, ConnectionError, OSError) as e:
            return {"success": False, "error": str(e)}

    # ---- Self-update (the service runs the scheduler; the GUI only reads
    # state and presses the same buttons the web panel does) ---------------

    def update_status(self) -> dict | None:
        try:
            data = self._request("GET", "/api/update/status") or {}
            return dict(data)
        except (HTTPError, URLError, TimeoutError, ConnectionError, OSError):
            return None

    def update_check(self) -> dict | None:
        try:
            data = self._request("POST", "/api/update/check", {}) or {}
            return dict(data)
        except (HTTPError, URLError, TimeoutError, ConnectionError, OSError):
            return None

    def update_set_channel(self, channel: str) -> dict | None:
        try:
            data = self._request(
                "POST", "/api/update/set-channel", {"channel": channel},
            ) or {}
            return dict(data)
        except (HTTPError, URLError, TimeoutError, ConnectionError, OSError):
            return None


# =============================================================================
# systemd service controller — wraps systemctl.  Needs a polkit rule
# (installed by installer_gui.py) so start/stop/restart run without a
# password prompt.  Falls back to "unknown" state when systemctl is absent
# so the GUI still works in non-systemd environments (e.g. dev shells).
# =============================================================================


class ServiceController:
    UNIT = SERVICE_UNIT

    @classmethod
    def _systemctl(cls, verb: str, timeout: float = 15.0) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["systemctl", verb, cls.UNIT],
            capture_output=True, text=True, timeout=timeout,
        )

    @classmethod
    def available(cls) -> bool:
        return shutil.which("systemctl") is not None

    @classmethod
    def state(cls) -> str:
        """Return systemd's ActiveState for the unit — or 'unknown' on error."""
        if not cls.available():
            return "unknown"
        try:
            r = cls._systemctl("is-active", timeout=5.0)
        except (subprocess.TimeoutExpired, OSError):
            return "unknown"
        # is-active prints "active" / "inactive" / "failed" etc. and uses
        # the return code only to indicate active-vs-not — the text is
        # always there regardless of exit status.
        state = (r.stdout or "").strip()
        return state or "unknown"

    @classmethod
    def _action(cls, verb: str) -> tuple[bool, str]:
        if not cls.available():
            return False, "systemctl no está disponible en este sistema."
        try:
            r = cls._systemctl(verb)
        except (subprocess.TimeoutExpired, OSError) as e:
            return False, str(e)
        if r.returncode == 0:
            return True, ""
        # systemctl prints authorization failures to stderr via
        # "Interactive authentication required." — surface the message
        # verbatim so the user knows to re-run the installer.
        detail = (r.stderr or r.stdout or "").strip() or f"exit {r.returncode}"
        return False, detail

    @classmethod
    def start(cls) -> tuple[bool, str]:
        return cls._action("start")

    @classmethod
    def stop(cls) -> tuple[bool, str]:
        return cls._action("stop")

    @classmethod
    def restart(cls) -> tuple[bool, str]:
        return cls._action("restart")


# =============================================================================
# Tray application (Gtk layer)
# =============================================================================


class PrinterTrayApp:
    """GTK3 control panel + system tray for the API Printer Service."""

    def __init__(self, client: PrinterServiceClient | None = None):
        self.client = client or PrinterServiceClient()
        self.controller = ServiceController
        self.printers: list[dict] = []
        self.default_printer: str | None = None
        self.service_up: bool = False
        self.service_state: str = "unknown"
        self._updating_tray = False
        self._service_action_in_flight = False
        self.update_state: dict | None = None
        self._notified_update_version: str | None = None

        self.window = Gtk.Window(title=APP_NAME)
        self.window.set_default_size(620, 560)
        self.window.set_icon_name("printer")
        self.window.connect("delete-event", self._on_delete_event)
        self._build_window()

        self.indicator = None
        self.tray_menu: Gtk.Menu | None = None
        if AppIndicator is not None:
            self.indicator = AppIndicator.Indicator.new(
                APP_ID, "printer",
                AppIndicator.IndicatorCategory.APPLICATION_STATUS,
            )
            self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
            self.indicator.set_title(APP_NAME)
            self._rebuild_tray_menu()
        else:
            log.warning(
                "No AppIndicator library available — tray icon disabled. "
                "Install gir1.2-ayatanaappindicator3-0.1 "
                "(or gir1.2-appindicator3-0.1)."
            )

        self._refresh_async()
        GLib.timeout_add_seconds(REFRESH_INTERVAL_SECONDS, self._refresh_async_tick)

    # ---- Window -------------------------------------------------------------

    def _build_window(self) -> None:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.window.add(outer)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header.set_margin_top(16)
        header.set_margin_bottom(8)
        header.set_margin_start(16)
        header.set_margin_end(16)

        icon = Gtk.Image.new_from_icon_name("printer", Gtk.IconSize.DIALOG)
        header.pack_start(icon, False, False, 0)

        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label()
        title.set_markup(f"<b><span size='x-large'>{APP_NAME}</span></b>")
        title.set_xalign(0)
        title_box.pack_start(title, False, False, 0)

        self.status_label = Gtk.Label()
        self.status_label.set_xalign(0)
        self.status_label.set_markup("<i>Checking service…</i>")
        title_box.pack_start(self.status_label, False, False, 0)

        header.pack_start(title_box, True, True, 0)

        self.refresh_button = Gtk.Button.new_from_icon_name(
            "view-refresh", Gtk.IconSize.BUTTON
        )
        self.refresh_button.set_tooltip_text("Refresh")
        self.refresh_button.connect("clicked", lambda _b: self._refresh_async())
        header.pack_start(self.refresh_button, False, False, 0)

        outer.pack_start(header, False, False, 0)
        outer.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
                         False, False, 0)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        body.set_margin_top(12)
        body.set_margin_bottom(12)
        body.set_margin_start(16)
        body.set_margin_end(16)
        outer.pack_start(body, True, True, 0)

        service_label = Gtk.Label()
        service_label.set_markup("<b>Service</b>")
        service_label.set_xalign(0)
        body.pack_start(service_label, False, False, 0)

        service_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self.service_state_label = Gtk.Label()
        self.service_state_label.set_xalign(0)
        self.service_state_label.set_markup("<i>Querying status…</i>")
        service_row.pack_start(self.service_state_label, True, True, 0)

        self.start_button = Gtk.Button(label="Start")
        self.start_button.connect(
            "clicked", lambda _b: self._service_action_async("start")
        )
        service_row.pack_start(self.start_button, False, False, 0)

        self.stop_button = Gtk.Button(label="Stop")
        self.stop_button.connect(
            "clicked", lambda _b: self._service_action_async("stop")
        )
        service_row.pack_start(self.stop_button, False, False, 0)

        self.restart_button = Gtk.Button(label="Restart")
        self.restart_button.connect(
            "clicked", lambda _b: self._service_action_async("restart")
        )
        service_row.pack_start(self.restart_button, False, False, 0)

        body.pack_start(service_row, False, False, 0)
        body.pack_start(
            Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
            False, False, 0,
        )

        printers_label = Gtk.Label()
        printers_label.set_markup("<b>Available printers</b>")
        printers_label.set_xalign(0)
        body.pack_start(printers_label, False, False, 0)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(180)

        # Columns: default (bool), name (str), status (str)
        self.printer_store = Gtk.ListStore(bool, str, str)
        self.printer_view = Gtk.TreeView(model=self.printer_store)
        self.printer_view.set_headers_visible(True)

        renderer_toggle = Gtk.CellRendererToggle()
        renderer_toggle.set_radio(True)
        renderer_toggle.connect("toggled", self._on_printer_row_toggled)
        col_default = Gtk.TreeViewColumn("Default", renderer_toggle, active=0)
        self.printer_view.append_column(col_default)

        col_name = Gtk.TreeViewColumn("Name", Gtk.CellRendererText(), text=1)
        col_name.set_expand(True)
        self.printer_view.append_column(col_name)

        col_status = Gtk.TreeViewColumn("Status", Gtk.CellRendererText(), text=2)
        self.printer_view.append_column(col_status)

        scroller.add(self.printer_view)
        body.pack_start(scroller, True, True, 0)

        action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        action_row.pack_start(Gtk.Label(label="Paper width (mm):"), False, False, 0)

        self.paper_combo = Gtk.ComboBoxText()
        for w in ("58", "80"):
            self.paper_combo.append_text(w)
        self.paper_combo.set_active(0)
        action_row.pack_start(self.paper_combo, False, False, 0)

        self.test_button = Gtk.Button(label="Print test page")
        self.test_button.connect("clicked", self._on_test_print_clicked)
        action_row.pack_end(self.test_button, False, False, 0)

        body.pack_start(action_row, False, False, 0)

        # ---- Updates -------------------------------------------------------
        # The service's updater does the checking on its own schedule; this
        # section mirrors its state and offers the same actions as the web
        # panel. Linux is notify-only: the release page opens in a browser.
        updates_label = Gtk.Label()
        updates_label.set_markup("<b>Updates</b>")
        updates_label.set_xalign(0)
        body.pack_start(updates_label, False, False, 0)

        updates_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.update_version_label = Gtk.Label(label="v—")
        updates_row.pack_start(self.update_version_label, False, False, 0)

        self.update_status_label = Gtk.Label(label="—")
        updates_row.pack_start(self.update_status_label, False, False, 0)

        self.release_btn = Gtk.Button(label="Open release page")
        self.release_btn.connect("clicked", self._on_open_release_page)
        self.release_btn.set_sensitive(False)
        updates_row.pack_end(self.release_btn, False, False, 0)

        self.check_updates_button = Gtk.Button(label="Check now")
        self.check_updates_button.connect("clicked", self._on_check_updates)
        updates_row.pack_end(self.check_updates_button, False, False, 0)

        self.channel_combo = Gtk.ComboBoxText()
        for c in ("stable", "preview"):
            self.channel_combo.append_text(c)
        self.channel_combo.set_active(0)
        self.channel_combo.connect("changed", self._on_channel_changed)
        updates_row.pack_end(self.channel_combo, False, False, 0)

        ring_label = Gtk.Label(label="Ring:")
        updates_row.pack_end(ring_label, False, False, 0)

        self.update_note_label = Gtk.Label(label="")
        self.update_note_label.set_xalign(0)
        self.update_note_label.set_line_wrap(True)
        self.update_note_label.set_opacity(0.75)
        body.pack_start(self.update_note_label, False, False, 0)

        updates_row.set_margin_top(2)
        body.pack_start(updates_row, False, False, 0)

        log_label = Gtk.Label()
        log_label.set_markup("<b>Log</b>")
        log_label.set_xalign(0)
        body.pack_start(log_label, False, False, 0)

        log_scroller = Gtk.ScrolledWindow()
        log_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        log_scroller.set_min_content_height(110)
        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_monospace(True)
        self.log_view.set_cursor_visible(False)
        self.log_buffer = self.log_view.get_buffer()
        log_scroller.add(self.log_view)
        body.pack_start(log_scroller, False, True, 0)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_margin_top(4)
        footer.set_margin_bottom(10)
        footer.set_margin_start(16)
        footer.set_margin_end(16)

        hide_btn = Gtk.Button(label="Hide to tray")
        hide_btn.connect("clicked", lambda _b: self.window.hide())
        footer.pack_end(hide_btn, False, False, 0)

        quit_btn = Gtk.Button(label="Quit")
        quit_btn.connect("clicked", lambda _b: self.quit())
        footer.pack_end(quit_btn, False, False, 0)

        outer.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
                         False, False, 0)
        outer.pack_start(footer, False, False, 0)

    def _log(self, msg: str) -> None:
        end_iter = self.log_buffer.get_end_iter()
        self.log_buffer.insert(end_iter, msg + "\n")
        mark = self.log_buffer.get_insert()
        self.log_view.scroll_to_mark(mark, 0.0, False, 0.0, 0.0)

    # ---- Tray ---------------------------------------------------------------

    def _rebuild_tray_menu(self) -> None:
        if self.indicator is None:
            return
        self._updating_tray = True
        try:
            menu = Gtk.Menu()

            item_open = Gtk.MenuItem(label="Open window")
            item_open.connect("activate", self._on_show_window)
            menu.append(item_open)

            item_printers = Gtk.MenuItem(label="Select printer")
            submenu = Gtk.Menu()
            group: Gtk.RadioMenuItem | None = None
            if not self.printers:
                empty = Gtk.MenuItem(label="(no printers)")
                empty.set_sensitive(False)
                submenu.append(empty)
            else:
                for p in self.printers:
                    name = p.get("name", "?")
                    rb = Gtk.RadioMenuItem.new_with_label_from_widget(group, name)
                    if group is None:
                        group = rb
                    if name == self.default_printer:
                        rb.set_active(True)
                    rb.connect("toggled", self._on_tray_printer_toggled, name)
                    submenu.append(rb)
            item_printers.set_submenu(submenu)
            menu.append(item_printers)

            item_test = Gtk.MenuItem(label="Print test page")
            item_test.connect("activate", self._on_tray_test_print)
            item_test.set_sensitive(bool(self.default_printer))
            menu.append(item_test)

            # Updates — visible from the tray even while the window is
            # hidden, which is where a merge-cut release is most likely to
            # be noticed first.
            item_updates = Gtk.MenuItem(label="Check for updates")
            item_updates.connect("activate", self._on_check_updates)
            menu.append(item_updates)
            if (
                self.update_state
                and self.update_state.get("status") == "available"
                and self.update_state.get("available_version")
            ):
                item_avail = Gtk.MenuItem(
                    label=f"Update available — v{self.update_state['available_version']}"
                )
                item_avail.connect("activate", self._on_open_release_page)
                menu.append(item_avail)

            menu.append(Gtk.SeparatorMenuItem())

            # Service control — match the state-aware enablement used by
            # the in-window buttons so tray clicks can't race systemd.
            transient = self.service_state in (
                "activating", "deactivating", "reloading",
            )
            running = self.service_state == "active"

            item_start = Gtk.MenuItem(label="Start service")
            item_start.connect(
                "activate", lambda _i: self._service_action_async("start")
            )
            item_start.set_sensitive(
                not self._service_action_in_flight and not transient and not running
            )
            menu.append(item_start)

            item_stop = Gtk.MenuItem(label="Stop service")
            item_stop.connect(
                "activate", lambda _i: self._service_action_async("stop")
            )
            item_stop.set_sensitive(
                not self._service_action_in_flight and not transient and running
            )
            menu.append(item_stop)

            item_restart = Gtk.MenuItem(label="Restart service")
            item_restart.connect(
                "activate", lambda _i: self._service_action_async("restart")
            )
            item_restart.set_sensitive(
                not self._service_action_in_flight and not transient
            )
            menu.append(item_restart)

            menu.append(Gtk.SeparatorMenuItem())

            item_quit = Gtk.MenuItem(label="Quit")
            item_quit.connect("activate", lambda _i: self.quit())
            menu.append(item_quit)

            menu.show_all()
            self.indicator.set_menu(menu)
            self.tray_menu = menu
        finally:
            self._updating_tray = False

    # ---- Event handlers -----------------------------------------------------

    def _on_delete_event(self, _widget, _event):
        self.window.hide()
        return True

    def _on_show_window(self, _item=None):
        self.window.show_all()
        self.window.present()

    def _on_printer_row_toggled(self, _renderer, path):
        it = self.printer_store.get_iter(path)
        name = self.printer_store.get_value(it, 1)
        self._set_default_printer(name)

    def _on_tray_printer_toggled(self, item: Gtk.RadioMenuItem, name: str):
        if self._updating_tray or not item.get_active():
            return
        self._set_default_printer(name)

    def _on_tray_test_print(self, _item):
        if self.default_printer:
            self._test_print_async(self.default_printer)

    # ---- Updates handlers --------------------------------------------------

    UPDATE_STATUS_MARKUP = {
        "idle":        ("", "Idle"),
        "checking":    ("#c9a227", "Checking…"),
        "up-to-date":  ("#27ae60", "Up to date"),
        "available":   ("#c9a227", "Update available"),
        "downloading": ("#c9a227", "Downloading…"),
        "installing":  ("#27ae60", "Installing…"),
        "ready":       ("#27ae60", "Ready"),
        "error":       ("#c0392b", "Error"),
    }

    def _apply_update(self, s: dict | None) -> None:
        if not s:
            self.update_state = None
            self.update_status_label.set_text("—")
            self.update_version_label.set_text("v—")
            self.release_btn.set_sensitive(False)
            return
        self.update_state = s
        colour, label = self.UPDATE_STATUS_MARKUP.get(
            s.get("status", "idle"), ("", "—"),
        )
        version = s.get("available_version")
        status_text = label + (f" — v{version}" if version else "")
        if colour:
            self.update_status_label.set_markup(
                f'<span foreground="{colour}" weight="bold">{status_text}</span>'
            )
        else:
            self.update_status_label.set_text(status_text)
        self.update_version_label.set_text(f"v{s.get('current_version', '?')}")
        self.release_btn.set_sensitive(
            s.get("status") == "available" and bool(s.get("release_url"))
        )
        self._sync_channel_combo(s)
        note = (s.get("capability_reason")
                if s.get("capability") == "notify" else "")
        if s.get("status") == "error" and s.get("error"):
            note = s["error"]
        self.update_note_label.set_text(note or "")
        self._rebuild_tray_menu()
        self._maybe_notify_update(s)

    def _sync_channel_combo(self, s: dict) -> None:
        # Setting the combo inside the refresh loop must not re-trigger the
        # changed handler; the guard flag handles that.
        channel = s.get("channel") or "stable"
        if self.channel_combo.get_active_text() != channel:
            self._applying_channel = True
            self.channel_combo.set_active(0 if channel == "stable" else 1)
            self._applying_channel = False

    def _maybe_notify_update(self, s: dict) -> None:
        """Proactive notice — exactly once per version, never on every
        refresh tick. Re-arms only when a DIFFERENT version appears."""
        version = s.get("available_version")
        if (
            s.get("status") != "available"
            or not version
            or version == self._notified_update_version
        ):
            return
        self._notified_update_version = version
        self._log(f"✓ A new version (v{version}) is available.")

        dialog = Gtk.MessageDialog(
            transient_for=self.window,
            flags=Gtk.DialogFlags.MODAL,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text=f"A new version (v{version}) is available",
        )
        dialog.format_secondary_text(
            "Open the release page to download and install it. "
            "The link is also available in the Updates section."
        )
        dialog.run()
        dialog.destroy()

    def _on_check_updates(self, _btn=None) -> None:
        self.check_updates_button.set_sensitive(False)
        self._log("Checking for updates…")

        def work():
            return self.client.update_check()

        def done(result):
            self.check_updates_button.set_sensitive(True)
            if not result:
                self._log("✗ Update check failed (service unreachable).")
                return
            status = result.get("status")
            if status == "available":
                self._log(f"✓ Update available: v{result.get('available_version')}.")
            elif status == "up-to-date":
                self._log(f"✓ Up to date (v{result.get('current_version')}).")
            elif status == "error":
                self._log(f"✗ Update check failed: {result.get('error')}")
            self._apply_update(result)

        self._run_bg(work, done)

    def _on_open_release_page(self, _btn=None) -> None:
        """Open the release page in the browser (notify path).

        The URL comes from the service, which already pins it to this
        repo's release pages; the same prefix is re-checked here because
        this is the one place a URL leaves the app for the OS browser."""
        url = (self.update_state or {}).get("release_url") or ""
        if not url.startswith(RELEASE_URL_PREFIX):
            self._log("✗ No valid release page to open.")
            return
        import webbrowser
        webbrowser.open(url)
        self._log(f"Opened release page for v{self.update_state.get('available_version')}.")

    def _on_channel_changed(self, _combo=None) -> None:
        if getattr(self, "_applying_channel", False):
            return
        channel = self.channel_combo.get_active_text()
        if not channel:
            return
        self._log(f"Switching update ring to '{channel}' and checking now…")

        def work():
            return self.client.update_set_channel(channel)

        def done(result):
            if not result:
                self._log("✗ Ring change failed (service unreachable).")
                return
            self._apply_update(result)

        self._run_bg(work, done)

    def _on_test_print_clicked(self, _btn):
        model, tree_iter = self.printer_view.get_selection().get_selected()
        printer = model.get_value(tree_iter, 1) if tree_iter is not None else None
        printer = printer or self.default_printer
        if not printer:
            self._log("✗ Select a printer before sending a test page.")
            return
        self._test_print_async(printer)

    # ---- Async operations ---------------------------------------------------

    def _run_bg(self, fn: Callable[[], Any], on_done: Callable[[Any], None]) -> None:
        def target():
            try:
                result = fn()
            except Exception as e:
                log.exception("Background task failed")
                result = e
            GLib.idle_add(on_done, result)
        threading.Thread(target=target, daemon=True).start()

    def _refresh_async_tick(self) -> bool:
        self._refresh_async()
        return True

    def _refresh_async(self) -> None:
        def work():
            return (
                self.controller.state(),
                self.client.health(),
                self.client.list_printers(),
                self.client.get_settings(),
                self.client.update_status(),
            )

        def done(result):
            if isinstance(result, Exception):
                self._apply_state("unknown", False, [], {})
                return
            state, health, printers, settings, update = result
            self._apply_state(state, health is not None, printers, settings)
            self._apply_update(update)

        self._run_bg(work, done)

    def _apply_state(self, state: str, up: bool, printers: list[dict],
                     settings: dict) -> None:
        self.service_state = state
        self.service_up = up
        self.printers = printers
        self.default_printer = (
            settings.get("default_printer")
            if settings and settings.get("default_printer")
            else next((p["name"] for p in printers if p.get("is_default")), None)
        )

        colour, label = SERVICE_STATE_LABELS.get(
            state, SERVICE_STATE_LABELS["unknown"]
        )
        # If systemd says "active" but the HTTP port hasn't come up yet,
        # surface that distinction so the user knows a restart is still
        # settling instead of thinking the UI is frozen.
        suffix = ""
        if state == "active" and not up:
            suffix = "  <span foreground='#c9a227'>(not responding yet)</span>"
        self.status_label.set_markup(
            f"<span foreground='{colour}'>● Service: {label}</span>  "
            f"<span foreground='#666'>({API_URL})</span>{suffix}"
        )
        self.service_state_label.set_markup(
            f"<span foreground='{colour}'>● {label}</span>"
        )

        self._update_service_buttons()

        self.printer_store.clear()
        for p in printers:
            name = p.get("name", "?")
            pstate = p.get("status") or "-"
            self.printer_store.append([name == self.default_printer, name, pstate])

        self._rebuild_tray_menu()

    def _update_service_buttons(self) -> None:
        """Enable/disable the start/stop/restart buttons based on state."""
        if self._service_action_in_flight:
            self.start_button.set_sensitive(False)
            self.stop_button.set_sensitive(False)
            self.restart_button.set_sensitive(False)
            return
        state = self.service_state
        # Transient states (activating/deactivating/reloading): block all
        # actions to avoid racing systemd; the next refresh will settle.
        transient = state in ("activating", "deactivating", "reloading")
        running = state == "active"
        stopped = state in ("inactive", "failed")

        self.start_button.set_sensitive(not transient and not running)
        self.stop_button.set_sensitive(not transient and running)
        self.restart_button.set_sensitive(not transient and (running or stopped))

    def _service_action_async(self, verb: str) -> None:
        if self._service_action_in_flight:
            return
        self._service_action_in_flight = True
        self._update_service_buttons()

        human = {"start": "Starting", "stop": "Stopping",
                 "restart": "Restarting"}[verb]
        self._log(f"→ {human} service…")

        def work():
            return getattr(self.controller, verb)()

        def done(result):
            self._service_action_in_flight = False
            if isinstance(result, Exception):
                self._log(f"✗ Error: {result}")
            else:
                ok, detail = result
                if ok:
                    self._log(f"✓ {verb.capitalize()} succeeded.")
                else:
                    self._log(f"✗ {verb.capitalize()} failed: {detail}")
            # Immediately re-read state; the periodic tick would eventually
            # catch up, but a snappy UI is better feedback after a click.
            self._refresh_async()

        self._run_bg(work, done)

    def _set_default_printer(self, name: str) -> None:
        if name == self.default_printer:
            return

        def work():
            return self.client.set_default_printer(name)

        def done(result):
            if isinstance(result, Exception) or not result:
                self._log(f"✗ Could not set printer \"{name}\".")
                return
            self.default_printer = name
            self._log(f"✓ Default printer: \"{name}\".")
            for row in self.printer_store:
                row[0] = row[1] == name
            self._rebuild_tray_menu()

        self._run_bg(work, done)

    def _test_print_async(self, printer: str) -> None:
        try:
            paper = int(self.paper_combo.get_active_text() or DEFAULT_PAPER_WIDTH)
        except (TypeError, ValueError):
            paper = DEFAULT_PAPER_WIDTH

        self._log(f"→ Sending test page to \"{printer}\" ({paper}mm)…")
        self.test_button.set_sensitive(False)

        def work():
            return self.client.test_print(printer, paper)

        def done(result):
            self.test_button.set_sensitive(True)
            if isinstance(result, Exception):
                self._log(f"✗ Error: {result}")
                return
            if result.get("success"):
                self._log(f"✓ Test page sent (job ID: {result.get('job_id')}).")
            else:
                err = result.get("error") or result.get("message") or "unknown error"
                self._log(f"✗ Test failed: {err}")

        self._run_bg(work, done)

    # ---- Lifecycle ----------------------------------------------------------

    def run(self) -> None:
        self.window.show_all()
        Gtk.main()

    def quit(self) -> None:
        Gtk.main_quit()


# =============================================================================
# Entry point
# =============================================================================


def main() -> int:
    log.info("Starting %s (indicator=%s)", APP_NAME, _INDICATOR_LIB or "none")
    app = PrinterTrayApp()
    try:
        app.run()
    except KeyboardInterrupt:
        app.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
