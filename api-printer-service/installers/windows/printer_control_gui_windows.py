"""
API Printer Service — Control Panel (Tkinter, Windows).

Port of installers/linux/printer_control_gui.py. PrinterServiceClient is
HTTP-only and portable across platforms; only the UI toolkit layer
(Tk / Gtk) and ServiceController (schtasks / systemctl) differ.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import tkinter as tk
from functools import partial
from tkinter import ttk
from typing import Any, Callable
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

# pystray + Pillow provide the Windows system-tray icon. Both are bundled
# offline by the installer (see requirements.txt), but we still guard the
# import so the GUI can still open its window if they are somehow missing
# — only the tray icon is lost in that case.
try:
    from PIL import Image, ImageDraw
    from pystray import Icon as TrayIcon, Menu as TrayMenu, MenuItem as TrayMenuItem
    _TRAY_AVAILABLE = True
except ImportError:
    Image = ImageDraw = None  # type: ignore[assignment]
    TrayIcon = TrayMenu = TrayMenuItem = None  # type: ignore[assignment]
    _TRAY_AVAILABLE = False

# Daily update check + desktop notification (plyer). The installer copies
# update_notifier.py into the same directory as this script; guarded so a
# dev checkout or an old install without the module still runs the GUI.
try:
    import update_notifier
except ImportError:
    update_notifier = None

API_URL = os.environ.get("API_PRINTER_URL", "http://127.0.0.1:5058")
APP_NAME = "API Printer Service"
TASK_NAME = "API Printer Service"
DEFAULT_PAPER_WIDTH = 58
REFRESH_INTERVAL_MS = 10_000

# Map the (state, http_up) tuple to (colour, label).
SERVICE_STATE_LABELS: dict[str, tuple[str, str]] = {
    "running":  ("#27ae60", "Running"),
    "stopped":  ("#666666", "Stopped"),
    "failed":   ("#c0392b", "Failed"),
    "missing":  ("#c0392b", "Not registered"),
    "unknown":  ("#c0392b", "Unknown"),
}

# Hide the black console window that schtasks/PowerShell would otherwise
# flash on every call. 0x08000000 = CREATE_NO_WINDOW.
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  %(message)s",
)
log = logging.getLogger("api-printer-gui")


# =============================================================================
# Service client — identical to the Linux version.
# =============================================================================


class PrinterServiceClient:
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
            return dict(self._request("GET", "/api/settings") or {})
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


# =============================================================================
# Scheduled-task controller — Windows counterpart of systemctl.
# =============================================================================


class ServiceController:
    """Start / stop / restart via schtasks. Elevation via PowerShell Start-Process -Verb RunAs."""

    TASK = TASK_NAME

    @classmethod
    def state(cls) -> str:
        # Use Get-ScheduledTask instead of `schtasks /Query` — schtasks prints
        # the Status column in the system UI language (e.g. "En ejecución" on
        # es-ES Windows), which would make English-only string matching report
        # Unknown on any localized install. The .State enum is culture-invariant.
        ps = (
            f"try {{ (Get-ScheduledTask -TaskName '{cls.TASK}' "
            f"-ErrorAction Stop).State }} catch {{ 'NotFound' }}"
        )
        try:
            # 15s timeout — at first-login the PowerShell host + ScheduledTasks
            # module can take several seconds to load while the rest of the
            # session is still settling. 5s was tight enough to return Unknown
            # on Win11 from time to time.
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True, timeout=15.0,
                creationflags=_CREATE_NO_WINDOW,
            )
        except (subprocess.TimeoutExpired, OSError):
            return "unknown"
        if r.returncode != 0:
            return "unknown"
        status = (r.stdout or "").strip().lower()
        if status == "running":
            return "running"
        if status == "queued":
            # Transient state between an AtStartup trigger firing and the
            # process actually launching — Windows reports the task as
            # Queued for a short window. Mapping it to "stopped" would
            # disable Stop and confuse the UI; "running" is closer to the
            # truth, and _apply_state's HTTP overlay will still add
            # "(HTTP not responding yet)" until the service is reachable.
            return "running"
        if status in ("ready", "disabled"):
            return "stopped"
        if status == "notfound":
            # Distinct from "unknown" — the task is provably not registered.
            # Callers can use this to give a clearer error than "exit 1".
            return "missing"
        return "unknown"

    # The elevated command's output is captured via a file in ProgramData,
    # not %TEMP%: the elevated process may run as a DIFFERENT user than the
    # GUI (standard POS operator + an admin's credentials in the UAC
    # prompt), so the two do not necessarily share a TEMP directory.
    # ProgramData is machine-wide: admins can write it, everyone can read.
    _PROGRAMDATA_DIR = os.path.join(
        os.environ.get("ProgramData", r"C:\ProgramData"), "api-printer-service",
    )
    _ACTION_LOG = os.path.join(_PROGRAMDATA_DIR, "last-task-action.log")

    @classmethod
    def _elevate_cmd(cls, command: str) -> tuple[bool, str]:
        """Run a cmd.exe one-liner elevated; return (ok, human-readable detail).

        Start-Process -Verb RunAs cannot capture the elevated child's
        output streams — that is why failures used to surface as a bare
        "exit 1". Instead, the elevated cmd itself redirects the command's
        stdout+stderr into _ACTION_LOG, which this non-elevated process
        reads back on failure, so the user sees schtasks' real error text
        in the OS display language.

        The whole argument string is passed as ONE pre-quoted -ArgumentList
        element: Windows PowerShell 5.1 joins multi-element lists with
        spaces WITHOUT quoting, so a task name with spaces would reach
        schtasks split into several arguments ("Invalid argument/option").
        """
        inner = (
            f'(if not exist "{cls._PROGRAMDATA_DIR}" mkdir "{cls._PROGRAMDATA_DIR}") & '
            f'({command}) > "{cls._ACTION_LOG}" 2>&1'
        )
        # Exit 1223 = ERROR_CANCELLED: the user clicked "No" on the UAC
        # prompt (Start-Process throws a Win32Exception in that case).
        ps = (
            f"try {{ $p = Start-Process cmd -ArgumentList '/c {inner}' "
            f"-Verb RunAs -Wait -PassThru -WindowStyle Hidden; "
            f"exit $p.ExitCode }} catch {{ "
            f"$w = $_.Exception; "
            f"if ($w.InnerException) {{ $w = $w.InnerException }}; "
            f"if ($w -is [System.ComponentModel.Win32Exception] -and "
            f"$w.NativeErrorCode -eq 1223) {{ exit 1223 }}; "
            f"Write-Error $_.Exception.Message; exit 1 }}"
        )
        try:
            # 180s: the UAC prompt itself can sit on screen for ~2 minutes
            # before Windows auto-cancels it; a shorter timeout would give
            # up while the prompt is still waiting for the user.
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive",
                 "-WindowStyle", "Hidden", "-Command", ps],
                capture_output=True, text=True, timeout=180.0,
                creationflags=_CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            return False, ("timed out waiting for the elevated command — "
                           "was the UAC prompt left unanswered?")
        except OSError as e:
            return False, f"could not launch PowerShell: {e}"
        if r.returncode == 0:
            return True, ""
        if r.returncode == 1223:
            return False, ("the UAC elevation prompt was declined — "
                           "click Yes there to allow this action")
        detail = cls._read_action_log()
        if not detail:
            detail = (r.stderr or r.stdout or "").strip()
        if detail:
            return False, f"{detail} [exit {r.returncode}]"
        return False, f"exit {r.returncode}"

    @classmethod
    def _read_action_log(cls) -> str:
        # schtasks prints in the console (OEM) codepage — cp850 on es-ES,
        # cp437 on en-US. The "oem" codec resolves the right one at runtime.
        try:
            with open(cls._ACTION_LOG, "r", encoding="oem",
                      errors="replace") as f:
                return " ".join(f.read().split())
        except OSError:
            return ""

    @classmethod
    def start(cls) -> tuple[bool, str]:
        return cls._elevate_cmd(f'schtasks /Run /TN "{cls.TASK}"')

    @classmethod
    def stop(cls) -> tuple[bool, str]:
        return cls._elevate_cmd(f'schtasks /End /TN "{cls.TASK}"')

    @classmethod
    def restart(cls) -> tuple[bool, str]:
        # Single elevated run does both stop+start, so the user only sees
        # one UAC prompt. /End may fail when the task is already stopped —
        # the chain swallows it (&) and the final /Run decides success.
        return cls._elevate_cmd(
            f'schtasks /End /TN "{cls.TASK}" & '
            f'timeout /t 2 /nobreak >nul & '
            f'schtasks /Run /TN "{cls.TASK}"'
        )


# =============================================================================
# Tk application
# =============================================================================


class PrinterControlApp:

    def __init__(
        self,
        root: tk.Tk,
        client: PrinterServiceClient | None = None,
        *,
        start_hidden: bool = False,
    ):
        self.root = root
        self.client = client or PrinterServiceClient()
        self.controller = ServiceController
        self.printers: list[dict] = []
        self.default_printer: str | None = None
        self.service_up: bool = False
        self.service_state: str = "unknown"
        self._service_action_in_flight = False
        self._tray_icon: Any = None

        self.root.title(APP_NAME)
        self.root.geometry("680x520")
        self.root.minsize(560, 420)

        # Clicking the window's X button hides to tray instead of quitting —
        # matches the Linux/GTK "Hide to tray" behaviour. Actual quit goes
        # through the tray menu's "Quit" item.
        self.root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)

        # Use ttk's "clam" theme — closer to modern Windows and the Gtk look
        # than the 1990s-era default.
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")

        self._build_ui()
        self._setup_tray()

        # When auto-starting at login the GUI stays out of the way — the
        # user only sees the tray icon until they click it. The installer
        # passes --start-hidden in the startup shortcut.
        if start_hidden:
            self.root.withdraw()

        self._refresh_async()
        self.root.after(REFRESH_INTERVAL_MS, self._refresh_tick)

        # Once-a-day update check; the notification itself is OS-level
        # (plyer balloon tip), only the log line needs the Tk thread.
        if update_notifier is not None:
            update_notifier.start_daily_check(
                lambda: (self.client.health() or {}).get("version"),
                on_update=lambda msg: self.root.after(
                    0, self._log, "↑ " + msg.replace("\n", " "), "info"
                ),
            )

    # ---- UI -----------------------------------------------------------------

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=(16, 14))
        outer.pack(fill="both", expand=True)

        # Header
        header = ttk.Frame(outer)
        header.pack(fill="x")

        title = ttk.Label(header, text=APP_NAME,
                          font=("Segoe UI", 14, "bold"))
        title.pack(side="left", anchor="w")

        self.refresh_btn = ttk.Button(
            header, text="Refresh", command=self._refresh_async, width=10,
        )
        self.refresh_btn.pack(side="right")

        self.status_label = ttk.Label(
            outer, text="Checking service…", font=("Segoe UI", 9),
        )
        self.status_label.pack(anchor="w", pady=(2, 10))

        ttk.Separator(outer, orient="horizontal").pack(fill="x", pady=(0, 10))

        # Service control row
        service_frame = ttk.LabelFrame(outer, text="Service", padding=(10, 6))
        service_frame.pack(fill="x", pady=(0, 10))

        self.service_state_label = ttk.Label(
            service_frame, text="Querying status…",
            font=("Segoe UI", 10, "bold"),
        )
        self.service_state_label.pack(side="left", anchor="w")

        self.restart_btn = ttk.Button(
            service_frame, text="Restart",
            command=lambda: self._service_action_async("restart"),
        )
        self.restart_btn.pack(side="right", padx=4)

        self.stop_btn = ttk.Button(
            service_frame, text="Stop",
            command=lambda: self._service_action_async("stop"),
        )
        self.stop_btn.pack(side="right", padx=4)

        self.start_btn = ttk.Button(
            service_frame, text="Start",
            command=lambda: self._service_action_async("start"),
        )
        self.start_btn.pack(side="right", padx=4)

        # Printers table
        printers_frame = ttk.LabelFrame(
            outer, text="Available printers", padding=(10, 6),
        )
        printers_frame.pack(fill="both", expand=True, pady=(0, 10))

        tree_box = ttk.Frame(printers_frame)
        tree_box.pack(fill="both", expand=True)

        self.printer_tree = ttk.Treeview(
            tree_box, columns=("default", "name", "status"),
            show="headings", selectmode="browse", height=6,
        )
        self.printer_tree.heading("default", text="Default")
        self.printer_tree.heading("name", text="Name")
        self.printer_tree.heading("status", text="Status")
        self.printer_tree.column("default", width=70, anchor="center", stretch=False)
        self.printer_tree.column("name", anchor="w")
        self.printer_tree.column("status", width=120, anchor="w", stretch=False)

        scroll = ttk.Scrollbar(
            tree_box, orient="vertical", command=self.printer_tree.yview,
        )
        self.printer_tree.configure(yscrollcommand=scroll.set)
        self.printer_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.printer_tree.bind("<Double-1>", self._on_printer_double_click)

        # Action row
        action_frame = ttk.Frame(outer)
        action_frame.pack(fill="x", pady=(0, 10))

        ttk.Label(action_frame, text="Paper width (mm):").pack(side="left")
        self.paper_combo = ttk.Combobox(
            action_frame, values=("58", "80"), state="readonly", width=6,
        )
        self.paper_combo.set("58")
        self.paper_combo.pack(side="left", padx=(6, 0))

        self.set_default_btn = ttk.Button(
            action_frame, text="Set as default",
            command=self._on_set_default_clicked,
        )
        self.set_default_btn.pack(side="right", padx=4)

        self.test_btn = ttk.Button(
            action_frame, text="Print test page",
            command=self._on_test_clicked,
        )
        self.test_btn.pack(side="right", padx=4)

        # Log
        log_frame = ttk.LabelFrame(outer, text="Log", padding=(10, 6))
        log_frame.pack(fill="both", expand=False)

        log_box = ttk.Frame(log_frame)
        log_box.pack(fill="both", expand=True)

        self.log_view = tk.Text(
            log_box, height=6, wrap="word", state="disabled",
            font=("Consolas", 9), bg="#1e1e1e", fg="#e0e0e0",
            insertbackground="#e0e0e0",
        )
        log_scroll = ttk.Scrollbar(
            log_box, orient="vertical", command=self.log_view.yview,
        )
        self.log_view.configure(yscrollcommand=log_scroll.set)
        self.log_view.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")
        self.log_view.tag_configure("ok", foreground="#27ae60")
        self.log_view.tag_configure("err", foreground="#e74c3c")
        self.log_view.tag_configure("info", foreground="#9ecbff")

    def _log(self, msg: str, tag: str = "info") -> None:
        self.log_view.configure(state="normal")
        self.log_view.insert("end", msg + "\n", tag)
        self.log_view.see("end")
        self.log_view.configure(state="disabled")

    # ---- Event handlers -----------------------------------------------------

    def _selected_printer(self) -> str | None:
        sel = self.printer_tree.selection()
        if not sel:
            return None
        values = self.printer_tree.item(sel[0], "values")
        return values[1] if len(values) >= 2 else None

    def _on_printer_double_click(self, _event) -> None:
        name = self._selected_printer()
        if name and name != self.default_printer:
            self._set_default_printer(name)

    def _on_set_default_clicked(self) -> None:
        name = self._selected_printer()
        if not name:
            self._log("Select a printer first.", "err")
            return
        if name == self.default_printer:
            return
        self._set_default_printer(name)

    def _on_test_clicked(self) -> None:
        printer = self._selected_printer() or self.default_printer
        if not printer:
            self._log("Select a printer before sending a test page.", "err")
            return
        self._test_print_async(printer)

    # ---- Tray --------------------------------------------------------------

    def _setup_tray(self) -> None:
        """Create the system-tray icon + menu in a background pystray thread.

        Callbacks from the tray run on that thread and must marshal any Tk
        work back to the main thread via ``root.after(0, ...)``.
        """
        if not _TRAY_AVAILABLE:
            log.warning(
                "pystray/Pillow not available — tray icon disabled. "
                "The installer bundles them; this should only happen in "
                "dev environments."
            )
            return
        self._tray_icon = TrayIcon(
            "api-printer-service",
            self._make_tray_image(),
            APP_NAME,
            menu=TrayMenu(self._tray_menu_items),
        )
        # run_detached() runs the Windows message loop on a dedicated
        # thread, so Tk stays on the main thread as required.
        self._tray_icon.run_detached()

    @staticmethod
    def _make_tray_image(colour: str = "#27ae60"):
        """Printer icon for the tray.

        Prefers the multi-resolution ``printer.ico`` shipped by the installer
        (same file used by the desktop / start-menu / startup shortcuts, so
        every place the user sees the app shows the same glyph). Falls back
        to a PIL-drawn coloured dot when the .ico is missing — useful in dev
        environments where the installer hasn't been run.
        """
        here = os.path.dirname(os.path.abspath(__file__))
        ico_path = os.path.join(here, "printer.ico")
        if os.path.isfile(ico_path):
            try:
                img = Image.open(ico_path)
                img.load()
                return img
            except Exception:
                log.exception("Failed to load %s — falling back to drawn icon",
                              ico_path)

        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle((4, 4, 60, 60), radius=14, fill="#2b2b2b")
        draw.ellipse((20, 20, 44, 44), fill=colour)
        return img

    def _tray_menu_items(self):
        """Called by pystray every time the menu is opened.

        Returning a generator of MenuItems means the state-dependent
        enablement / radio checks are always computed from fresh values,
        no explicit ``update_menu()`` needed for those.
        """
        yield TrayMenuItem("Open window", self._tray_show, default=True)

        yield TrayMenuItem(
            "Select printer",
            TrayMenu(lambda: list(self._tray_printer_items())),
            enabled=bool(self.printers),
        )

        yield TrayMenuItem(
            "Print test page",
            self._tray_test_print,
            enabled=bool(self.default_printer),
        )

        yield TrayMenu.SEPARATOR

        # Mirror the in-window button enablement rules so tray clicks
        # can't race schtasks (same logic as _update_buttons).
        transient = self.service_state in (
            "activating", "deactivating", "reloading",
        )
        running = self.service_state == "running"
        stopped = self.service_state in ("stopped", "failed")
        busy = self._service_action_in_flight

        yield TrayMenuItem(
            "Start service",
            self._tray_action("start"),
            enabled=(not busy and not transient and not running),
        )
        yield TrayMenuItem(
            "Stop service",
            self._tray_action("stop"),
            enabled=(not busy and not transient and running),
        )
        yield TrayMenuItem(
            "Restart service",
            self._tray_action("restart"),
            enabled=(not busy and not transient and (running or stopped)),
        )

        yield TrayMenu.SEPARATOR

        yield TrayMenuItem("Quit", self._tray_quit)

    def _tray_printer_items(self):
        for p in self.printers:
            name = p.get("name", "?")
            yield TrayMenuItem(
                name,
                partial(self._tray_select_printer, name),
                checked=(lambda _item, n=name: n == self.default_printer),
                radio=True,
            )

    def _tray_action(self, verb: str) -> Callable:
        def _cb(_icon, _item):
            self.root.after(0, lambda: self._service_action_async(verb))
        return _cb

    def _tray_show(self, _icon=None, _item=None) -> None:
        self.root.after(0, self._show_window)

    def _tray_test_print(self, _icon, _item) -> None:
        if self.default_printer:
            self.root.after(
                0, lambda: self._test_print_async(self.default_printer),
            )

    def _tray_select_printer(self, name: str, _icon, _item) -> None:
        self.root.after(0, lambda: self._set_default_printer(name))

    def _tray_quit(self, icon, _item) -> None:
        # Callback runs on pystray thread. Stop the tray, then schedule
        # the Tk destroy on the main thread.
        icon.stop()
        self.root.after(0, self._quit)

    def _show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _hide_to_tray(self) -> None:
        if self._tray_icon is not None:
            self.root.withdraw()
        else:
            # No tray available — X on the window quits the app, since
            # otherwise the process would have no way out.
            self._quit()

    def _quit(self) -> None:
        if self._tray_icon is not None:
            try:
                self._tray_icon.stop()
            except Exception:
                log.exception("Failed to stop tray icon")
        self.root.destroy()

    def _update_tray(self) -> None:
        if self._tray_icon is None:
            return
        _, label = SERVICE_STATE_LABELS.get(
            self.service_state, SERVICE_STATE_LABELS["unknown"],
        )
        try:
            # Tooltip reflects current service state; icon image stays
            # static (printer glyph) to match the Linux GTK indicator.
            self._tray_icon.title = f"{APP_NAME} — {label}"
            self._tray_icon.update_menu()
        except Exception:
            log.exception("Tray update failed")

    # ---- Background work ---------------------------------------------------

    def _run_bg(self, fn: Callable[[], Any],
                on_done: Callable[[Any], None]) -> None:
        def target():
            try:
                result = fn()
            except Exception as e:
                log.exception("Background task failed")
                result = e
            self.root.after(0, on_done, result)
        threading.Thread(target=target, daemon=True).start()

    def _refresh_tick(self) -> None:
        self._refresh_async()
        self.root.after(REFRESH_INTERVAL_MS, self._refresh_tick)

    def _refresh_async(self) -> None:
        def work():
            return (
                self.controller.state(),
                self.client.health(),
                self.client.list_printers(),
                self.client.get_settings(),
            )

        def done(result):
            if isinstance(result, Exception):
                self._apply_state("unknown", False, [], {})
                return
            state, health, printers, settings = result
            self._apply_state(state, health is not None, printers, settings)

        self._run_bg(work, done)

    def _apply_state(self, state: str, up: bool, printers: list[dict],
                     settings: dict) -> None:
        # HTTP response is authoritative — if the API answers, the service
        # is up, even if the schtasks query timed out, was localized oddly,
        # or returned an unmapped TaskState like "Queued". Otherwise a
        # still-running service can flash as "Unknown" and leave the Start
        # button incorrectly enabled (triggering an "already running" error
        # from schtasks /Run).
        if up and state != "running":
            state = "running"

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
        suffix = ""
        if state == "running" and not up:
            suffix = "  (HTTP not responding yet)"
        self.status_label.configure(
            text=f"● Service: {label}   ({API_URL}){suffix}",
            foreground=colour,
        )
        self.service_state_label.configure(
            text=f"● {label}", foreground=colour,
        )

        self._update_buttons()
        self._update_tray()

        # Rebuild table; try to preserve selection by printer name.
        prev_sel = self._selected_printer()
        for item in self.printer_tree.get_children():
            self.printer_tree.delete(item)
        for p in printers:
            name = p.get("name", "?")
            pstate = p.get("status") or "-"
            mark = "★" if name == self.default_printer else ""
            iid = self.printer_tree.insert(
                "", "end", values=(mark, name, pstate),
            )
            if name == prev_sel:
                self.printer_tree.selection_set(iid)

    def _update_buttons(self) -> None:
        if self._service_action_in_flight:
            for b in (self.start_btn, self.stop_btn, self.restart_btn):
                b.state(["disabled"])
            return
        running = self.service_state == "running"
        stopped = self.service_state in ("stopped", "failed")
        # When the scheduled task is missing, every schtasks /Run will
        # return exit 1 — disable the verbs that depend on it so the user
        # isn't repeatedly stuck on "Start failed: exit 1" with no hint
        # about what to fix. The error log already surfaces the message.
        missing = self.service_state == "missing"
        self.start_btn.state(
            ["!disabled"] if (not running and not missing) else ["disabled"]
        )
        self.stop_btn.state(["!disabled"] if running else ["disabled"])
        self.restart_btn.state(
            ["!disabled"] if (running or stopped) else ["disabled"],
        )

    def _service_action_async(self, verb: str) -> None:
        if self._service_action_in_flight:
            return
        self._service_action_in_flight = True
        self._update_buttons()

        human = {"start": "Starting", "stop": "Stopping",
                 "restart": "Restarting"}[verb]
        self._log(f"→ {human} service (UAC prompt may appear)…", "info")

        def work():
            return getattr(self.controller, verb)()

        def done(result):
            self._service_action_in_flight = False
            if isinstance(result, Exception):
                self._log(f"✗ Error: {result}", "err")
            else:
                ok, detail = result
                if ok:
                    self._log(f"✓ {verb.capitalize()} succeeded.", "ok")
                else:
                    # The bare "exit 1" we were showing is what schtasks
                    # /Run returns both when the user cancels UAC and when
                    # the task isn't registered, which left users guessing.
                    # If we already know the task is missing, say so
                    # explicitly with the fix the user needs to apply.
                    if (
                        verb in ("start", "restart")
                        and self.service_state == "missing"
                    ):
                        self._log(
                            "✗ Scheduled task 'API Printer Service' is not "
                            "registered. Re-run the installer as "
                            "Administrator to register it.",
                            "err",
                        )
                    else:
                        self._log(
                            f"✗ {verb.capitalize()} failed: {detail}",
                            "err",
                        )
            self._refresh_async()

        self._run_bg(work, done)

    def _set_default_printer(self, name: str) -> None:
        if name == self.default_printer:
            return

        def work():
            return self.client.set_default_printer(name)

        def done(result):
            if isinstance(result, Exception) or not result:
                self._log(f"✗ Could not set printer \"{name}\".", "err")
                return
            self.default_printer = name
            self._log(f"✓ Default printer: \"{name}\".", "ok")
            # Update the star markers in the tree without a full refresh.
            for iid in self.printer_tree.get_children():
                vals = list(self.printer_tree.item(iid, "values"))
                vals[0] = "★" if vals[1] == name else ""
                self.printer_tree.item(iid, values=vals)

        self._run_bg(work, done)

    def _test_print_async(self, printer: str) -> None:
        try:
            paper = int(self.paper_combo.get() or DEFAULT_PAPER_WIDTH)
        except (TypeError, ValueError):
            paper = DEFAULT_PAPER_WIDTH

        self._log(f"→ Sending test page to \"{printer}\" ({paper}mm)…", "info")
        self.test_btn.state(["disabled"])

        def work():
            return self.client.test_print(printer, paper)

        def done(result):
            self.test_btn.state(["!disabled"])
            if isinstance(result, Exception):
                self._log(f"✗ Error: {result}", "err")
                return
            if result.get("success"):
                self._log(
                    f"✓ Test page sent (job ID: {result.get('job_id')}).", "ok",
                )
            else:
                err = (result.get("error") or result.get("message")
                       or "unknown error")
                self._log(f"✗ Test failed: {err}", "err")

        self._run_bg(work, done)


# =============================================================================
# Entry point
# =============================================================================


def main() -> int:
    start_hidden = "--start-hidden" in sys.argv[1:]
    root = tk.Tk()
    PrinterControlApp(root, start_hidden=start_hidden)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
