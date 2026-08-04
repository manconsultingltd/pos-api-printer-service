"""
Windows printer enumeration — exercised on any platform by injecting a fake
win32print module, since the real one only imports on Windows.
"""

import sys
import types

from printer_manager import PrinterManager


def _fake_win32print(default_raises: bool) -> types.ModuleType:
    mod = types.ModuleType("win32print")
    mod.PRINTER_ENUM_LOCAL = 2
    mod.PRINTER_ENUM_CONNECTIONS = 4

    def GetDefaultPrinter():
        if default_raises:
            # Mirrors pywin32 when the profile has no default printer,
            # which is the normal state for the SYSTEM account.
            raise RuntimeError("GetDefaultPrinter", "no default printer")
        return "POS-58"

    def EnumPrinters(flags, name, level):
        return [
            {"pPrinterName": "POS-58", "Status": 0},
            {"pPrinterName": "Kitchen", "Status": 0},
        ]

    mod.GetDefaultPrinter = GetDefaultPrinter
    mod.EnumPrinters = EnumPrinters
    return mod


def test_windows_listing_survives_missing_default_printer(monkeypatch):
    monkeypatch.setitem(sys.modules, "win32print", _fake_win32print(True))
    printers = PrinterManager()._list_printers_windows()
    assert [p["name"] for p in printers] == ["POS-58", "Kitchen"]
    assert not any(p["is_default"] for p in printers)


def test_windows_listing_marks_default(monkeypatch):
    monkeypatch.setitem(sys.modules, "win32print", _fake_win32print(False))
    printers = PrinterManager()._list_printers_windows()
    assert [p["is_default"] for p in printers] == [True, False]
