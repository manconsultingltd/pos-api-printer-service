"""
ESCPOSGenerator: modeled receipts stay stable, data-print blocks land in the
right position with the right styling, and literal mode prints the format's
layout verbatim.
"""

import pytest

from escpos_generator import ESCPOSGenerator
from html_parser import HtmlReceiptParser
from fixtures import DATA_PRINT_HTML, LITERAL_HTML

ALIGN_LEFT = b"\x1b\x61\x00"
ALIGN_CENTER = b"\x1b\x61\x01"
ALIGN_RIGHT = b"\x1b\x61\x02"
BOLD_ON = b"\x1b\x45\x01"
BOLD_OFF = b"\x1b\x45\x00"
CUT_PARTIAL = b"\x1d\x56\x01"


LEGACY_INVOICE = {
    "company": "ACME Store",
    "company_address": "123 Market Street\nSpringfield",
    "currency_symbol": "",
    "invoice_number": "INV-001",
    "date": "15-07-2026 10:00",
    "cashier": "Sam",
    "customer": "Jane",
    "items_label": "ITEMS",
    "items": [{"name": "Widget", "qty": 2, "rate": 2.50, "amount": 5.00}],
    "subtotal_label": "Subtotal",
    "net_total": 4.59,
    "taxes": [{"description": "Tax 9%", "amount": 0.41}],
    "total_label": "TOTAL",
    "total": 5.00,
    "payments": [{"method": "Cash", "amount": 5.00}],
    "change": 0,
    "footer_lines": ["Thank you for your business!"],
}


def _last_cmd_before(raw: bytes, needle: bytes, commands) -> bytes:
    """Return which of `commands` appears last before `needle` in `raw`."""
    pos = raw.index(needle)
    best, best_at = None, -1
    for cmd in commands:
        at = raw.rfind(cmd, 0, pos)
        if at > best_at:
            best, best_at = cmd, at
    return best


def test_legacy_receipt_is_byte_identical_without_print_blocks():
    generator = ESCPOSGenerator(paper_width=58)
    base = generator.generate_receipt(dict(LEGACY_INVOICE))
    with_empty_key = generator.generate_receipt(
        {**LEGACY_INVOICE, "print_blocks": {}}
    )
    with_empty_sections = generator.generate_receipt(
        {**LEGACY_INVOICE, "print_blocks": {"header": [], "fiscal": [], "footer": []}}
    )
    assert base == with_empty_key == with_empty_sections
    assert b"\x1b\x61\x02" not in base  # no right-align leaks into legacy output


def test_test_receipt_still_generates():
    raw = ESCPOSGenerator(paper_width=58).generate_test_receipt()
    assert raw.startswith(b"\x1b@")
    assert raw.endswith(CUT_PARTIAL)


class TestDataPrintReceipt:
    @pytest.fixture(autouse=True)
    def _generate(self):
        invoice = HtmlReceiptParser().parse(DATA_PRINT_HTML)
        self.raw = ESCPOSGenerator(paper_width=80).generate_receipt(invoice)
        self.text = self.raw.decode("cp437")

    def test_blocks_present(self):
        assert "Auth Code: ABC-123-XYZ" in self.text
        assert "Authorized Range: 000-001 - 005-000" in self.text
        assert "Reg ID: 999-8888" in self.text
        assert "Exemption: EX-2026-7" in self.text
        assert "** PAID **" in self.text
        assert "Copy 1: Customer" in self.text

    def test_section_order(self):
        # header block after the address, before the invoice details
        assert (
            self.text.index("123 Market Street")
            < self.text.index("Auth Code:")
            < self.text.index("Invoice: INV-100")
        )
        # fiscal block right after the customer, before the items
        assert (
            self.text.index("Customer: Jane Roe")
            < self.text.index("Reg ID: 999-8888")
            < self.text.index("ITEMS")
        )
        # footer blocks after the thank-you line, copies after the status seal
        assert (
            self.text.index("Thank you for your business!")
            < self.text.index("** PAID **")
            < self.text.index("Copy 1: Customer")
        )

    def test_copies_are_right_aligned(self):
        cmd = _last_cmd_before(
            self.raw, b"Copy 1: Customer", [ALIGN_LEFT, ALIGN_CENTER, ALIGN_RIGHT]
        )
        assert cmd == ALIGN_RIGHT

    def test_status_seal_is_bold(self):
        cmd = _last_cmd_before(self.raw, b"** PAID **", [BOLD_ON, BOLD_OFF])
        assert cmd == BOLD_ON

    def test_styles_reset_after_blocks_and_paper_cut(self):
        # after the right-aligned copies block, alignment is restored
        pos = self.raw.index(b"Copy 1: Customer")
        assert self.raw.find(ALIGN_CENTER, pos) != -1
        assert self.raw.endswith(CUT_PARTIAL)


class TestLiteralReceipt:
    @pytest.fixture(autouse=True)
    def _generate(self):
        invoice = HtmlReceiptParser().parse(LITERAL_HTML)
        self.raw = ESCPOSGenerator(paper_width=80).generate_receipt(invoice)
        self.text = self.raw.decode("cp437")

    def test_no_modeled_sections(self):
        # Literal mode adds nothing of its own: no separators, no automatic
        # section labels, no default footer.
        assert "=" * 10 not in self.text
        assert "ITEMS" not in self.text
        assert "Thank you for your business!" not in self.text
        assert "TOTAL:" not in self.text  # only the format's own "Total:" row

    def test_format_layout_prints_verbatim(self):
        for line in (
            "ACME STORE",
            "Receipt No: R-100",
            "Issued: 31-07-2026 Time: 11:57:26",
            "Customer: WALK-IN CUSTOMER",
            "7422300500418 Trigger Spray Cleaner 30g",
            "Subtotal: 13.91",
            "Sales Tax 18%: 0.00",  # zero rows DO print here
            "Total: 16.00",
            "Paid with CASH: 16.00",
            "Sixteen dollars exactly.",
            "Authorized Range:",
            "Original: Customer   Copy: Store",
            "Please retain this receipt.",
        ):
            assert line in self.text, line

    def test_column_row_left_and_right_edges(self):
        # 80mm -> 48 chars: first cell at the left edge, last flush right.
        # The decoded line still carries the ESC alignment prefix, cut it off.
        line = next(l for l in self.text.splitlines() if "Qty" in l)
        line = line[line.index("Qty"):]
        assert line.endswith("Amount")
        assert len(line) == ESCPOSGenerator(paper_width=80).chars_per_line

    def test_document_order_is_preserved(self):
        assert (
            self.text.index("ACME STORE")
            < self.text.index("Branch: Downtown")
            < self.text.index("Receipt No:")
            < self.text.index("Qty")
            < self.text.index("Subtotal: 13.91")
            < self.text.index("Paid with CASH")
            < self.text.index("Authorized Range:")
            < self.text.index("THANK YOU FOR SHOPPING WITH US")
            < self.text.index("Please retain this receipt.")
        )

    def test_company_is_bold_and_double_size(self):
        pos = self.raw.index(b"ACME STORE")
        assert self.raw.rfind(BOLD_ON, 0, pos) != -1
        assert self.raw.rfind(b"\x1b\x21\x30", 0, pos) != -1  # double size
        cmd = _last_cmd_before(self.raw, b"ACME STORE",
                               [ALIGN_LEFT, ALIGN_CENTER, ALIGN_RIGHT])
        assert cmd == ALIGN_CENTER

    def test_tax_breakdown_is_right_aligned(self):
        cmd = _last_cmd_before(self.raw, b"Subtotal: 13.91",
                               [ALIGN_LEFT, ALIGN_CENTER, ALIGN_RIGHT])
        assert cmd == ALIGN_RIGHT

    def test_no_returns_line_is_bold(self):
        cmd = _last_cmd_before(self.raw, b"NO RETURNS", [BOLD_ON, BOLD_OFF])
        assert cmd == BOLD_ON

    def test_init_and_cut(self):
        assert self.raw.startswith(b"\x1b@")
        assert self.raw.endswith(CUT_PARTIAL)


def test_literal_lines_key_absent_keeps_legacy_path():
    """No opt-in (key absent) -> modeled path, byte-identical."""
    generator = ESCPOSGenerator(paper_width=58)
    base = generator.generate_receipt(dict(LEGACY_INVOICE))
    with_absent = generator.generate_receipt(dict(LEGACY_INVOICE))
    assert base == with_absent


def test_empty_literal_lines_prints_blank_not_modeled():
    """A format that opts into literal mode but renders every segment empty
    parses to literal_lines=[]. That must take the literal path (blank paper),
    NOT fall through to the modeled path (which would 500 on total=None)."""
    generator = ESCPOSGenerator(paper_width=58)
    modeled = generator.generate_receipt(dict(LEGACY_INVOICE))
    literal_empty = generator.generate_receipt(
        {**LEGACY_INVOICE, "literal_lines": []}
    )
    # Not the modeled receipt...
    assert literal_empty != modeled
    # ...and identical to rendering zero segments directly: a bare cut.
    assert literal_empty == generator.generate_literal_receipt([])
    assert b"ACME Store" not in literal_empty
    assert literal_empty.startswith(b"\x1b@")
    assert literal_empty.endswith(CUT_PARTIAL)


def test_empty_literal_wrapper_end_to_end_does_not_500():
    """End-to-end guard: <div data-print-mode="literal"></div> with no
    content must parse+generate without raising (the /print 500 regression)."""
    parser = HtmlReceiptParser()
    generator = ESCPOSGenerator(paper_width=58)
    parsed = parser.parse(
        '<div data-print-mode="literal"></div>'
    )
    assert parsed.get("literal_lines") == []
    raw = generator.generate_receipt(parsed)
    assert raw.startswith(b"\x1b@")
    assert raw.endswith(CUT_PARTIAL)


def test_modeled_long_item_name_wraps_fully_without_truncation():
    """A long item name must word-wrap across as many lines as needed —
    'Knife: 18.6cm' used to come out as 'Knife: 18.' on 58mm paper."""
    generator = ESCPOSGenerator(paper_width=58)
    name = "3 Piece Cutlery Set Fork: 16.3 cm Spoon: 16.3cm Knife: 18.6cm"
    invoice = {
        **LEGACY_INVOICE,
        "items": [{"name": name, "qty": 1, "rate": 21.95, "amount": 21.95}],
    }
    text = generator.generate_receipt(invoice).decode("cp437")
    printed = " ".join(text.split())
    assert "Knife: 18.6cm" in printed
    # No printed line exceeds the paper width
    for line in text.splitlines():
        clean = line.lstrip("\x1b\x1d\x00\x01\x10\x20\x30!@Ea")
        assert len(clean) <= generator.chars_per_line, repr(line)


def test_literal_text_wraps_to_paper_width():
    """Literal mode must word-wrap at the paper width instead of letting the
    printer break mid-word at the edge of narrow paper."""
    generator = ESCPOSGenerator(paper_width=58)  # 32 chars
    raw = generator.generate_literal_receipt([
        {"type": "text",
         "lines": ["3 Piece Cutlery Set Fork: 16.3 cm Spoon: 16.3cm Knife: 18.6cm"],
         "align": "left", "bold": False, "large": False},
    ])
    text = raw.decode("cp437")
    assert "Knife: 18.6cm" in " ".join(text.split())
    body_lines = [l for l in text.splitlines() if "16.3" in l or "18.6" in l]
    assert len(body_lines) >= 2  # actually wrapped
    for line in body_lines:
        assert len(line.replace("\x1b\x61\x00", "")) <= generator.chars_per_line


def test_literal_large_column_row_prints_double_size_at_half_width():
    generator = ESCPOSGenerator(paper_width=58)  # 32 chars -> 16 when large
    raw = generator.generate_literal_receipt([
        {"type": "columns", "cells": ["Totaal:", "34.90"],
         "bold": True, "large": True},
    ])
    assert b"\x1b\x21\x10" in raw  # double height on
    assert b"\x1b\x21\x00" in raw  # reset after
    text = raw.decode("cp437")
    line = next(l for l in text.splitlines() if "Totaal" in l)
    line = line[line.index("Totaal"):]
    assert line == "Totaal:    34.90"
    assert len(line) == generator.chars_per_line // 2


def test_columns_collision_truncates_without_scrambling():
    """On a too-narrow line, colliding cells truncate visibly instead of
    overwriting each other's characters."""
    generator = ESCPOSGenerator(paper_width=58)  # 32 chars/line
    # Three cells whose combined width overflows the line; the middle cell
    # collides with the left cell's region.
    row = generator._layout_columns(
        ["LEFTLEFTLEFTLEFT", "MIDDLEMIDDLE", "RIGHTRIGHTRIGHT"]
    )
    # The left cell survives intact and nothing overruns the line width.
    assert "LEFTLEFTLEFTLEFT" in row
    assert len(row) <= generator.chars_per_line
