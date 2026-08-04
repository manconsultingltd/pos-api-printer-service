"""
Configuration settings for API Printer Service
"""

import json
import os
import sys
from typing import Optional


def _default_log_file() -> str:
    """Return a platform-appropriate default log file path."""
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "C:\\")
        return os.path.join(appdata, "api-printer-service", "api-printer.log")
    elif sys.platform == "darwin":
        return os.path.expanduser("~/Library/Logs/api-printer.log")
    else:  # Linux / other POSIX
        return "/var/log/api-printer.log"


def _default_settings_file() -> str:
    """Platform-appropriate path for the persistent settings JSON."""
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "C:\\")
        return os.path.join(appdata, "api-printer-service", "settings.json")
    elif sys.platform == "darwin":
        return "/Library/Application Support/api-printer-service/settings.json"
    else:
        return "/etc/api-printer-service/settings.json"


class Config:
    """Application configuration"""

    # Platform flags
    IS_WINDOWS: bool = sys.platform == "win32"
    IS_MAC: bool = sys.platform == "darwin"
    IS_LINUX: bool = sys.platform.startswith("linux")

    # Server settings
    HOST: str = "127.0.0.1"  # Localhost only for security
    PORT: int = 5058

    # Default printer settings
    DEFAULT_PRINTER: str = "ThermalPrinter"
    DEFAULT_PAPER_WIDTH: int = 58  # mm (58mm thermal paper)
    DEFAULT_CHARS_PER_LINE: int = 32  # Characters per line for 58mm paper

    # ESC/POS settings
    ENCODING: str = "cp437"  # Standard encoding for ESC/POS printers

    # Cash drawer settings
    DRAWER_PULSE_ON_TIME: int = 50  # milliseconds (25 * 2ms units = 50ms)
    DRAWER_PULSE_OFF_TIME: int = 250  # milliseconds (125 * 2ms units = 250ms)

    # CUPS settings (Linux / macOS)
    LPR_PATH: str = "/usr/bin/lpr"
    LPSTAT_PATH: str = "/usr/bin/lpstat"
    CANCEL_PATH: str = "/usr/bin/cancel"

    # Logging — override via LOG_FILE env var (set by installer / systemd service)
    LOG_FILE: str = os.getenv("LOG_FILE", _default_log_file())
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # Persistent settings (default printer, etc.) — overridable for tests
    SETTINGS_FILE: str = os.getenv("API_PRINTER_SETTINGS_FILE", _default_settings_file())

    # API settings
    CORS_ORIGINS: list = [
        "*",  # Allow all origins to support any ERPNext domain out-of-the-box without browser insecure flags
        "http://localhost",
        "http://localhost:8000",
        "http://localhost:8080",
        "http://127.0.0.1",
        "http://127.0.0.1:8000",
        "http://127.0.0.1:8080",
        "file://",  # Allow local file:// origin
    ]

    @classmethod
    def load_settings(cls) -> dict:
        """Load persisted settings from SETTINGS_FILE (empty dict if missing)."""
        try:
            with open(cls.SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}

    @classmethod
    def save_settings(cls, settings: dict) -> None:
        """Atomically persist settings to SETTINGS_FILE."""
        path = cls.SETTINGS_FILE
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, sort_keys=True)
        os.replace(tmp, path)

    @classmethod
    def get_default_printer(cls) -> str:
        """Resolved default printer.

        Resolution order:
          1. Persisted setting in SETTINGS_FILE (set via the Control Panel).
          2. On Windows: the OS-level default printer reported by the spooler.
             The Windows installer doesn't create a queue named "ThermalPrinter",
             so falling back to the hard-coded name would 404 on every print
             until the user opens the Control Panel — using whichever printer
             Windows itself considers the default lets it just work out of the
             box.
          3. The hard-coded ``DEFAULT_PRINTER`` ("ThermalPrinter") — matches
             the CUPS queue name a Linux USB-printer setup script may create.
        """
        name = cls.load_settings().get("default_printer")
        if name:
            return name
        if cls.IS_WINDOWS:
            try:
                import win32print
                sys_default = win32print.GetDefaultPrinter()
                if sys_default:
                    return sys_default
            except Exception:
                pass
        return cls.DEFAULT_PRINTER

    @classmethod
    def set_default_printer(cls, name: str) -> None:
        settings = cls.load_settings()
        settings["default_printer"] = name
        cls.save_settings(settings)

    @classmethod
    def get_chars_per_line(cls, paper_width: int) -> int:
        """Get characters per line based on paper width"""
        width_map = {
            58: 32,  # 58mm paper
            80: 48,  # 80mm paper
        }
        return width_map.get(paper_width, cls.DEFAULT_CHARS_PER_LINE)

    @classmethod
    def validate_printer_exists(cls, printer_name: str) -> bool:
        """Check if printer exists in CUPS (Linux/macOS) or Windows spooler."""
        if cls.IS_WINDOWS:
            try:
                import win32print
                printers = [p[2] for p in win32print.EnumPrinters(
                    win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
                )]
                return printer_name in printers
            except Exception:
                return False

        import subprocess
        try:
            result = subprocess.run(
                [cls.LPSTAT_PATH, "-p", printer_name],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False
