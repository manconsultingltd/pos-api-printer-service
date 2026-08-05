"""
HtmlReceiptParser: modeled-mode extraction, literal data-print pass-through
blocks, and full literal print mode.
"""

import pytest

from html_parser import HtmlReceiptParser
from fixtures import (
    DATA_PRINT_HTML,
    ENGLISH_HTML,
    LITERAL_HTML,
)


@pytest.fixture
def parser():
    return HtmlReceiptParser()


# === Modeled mode: structured extraction, labels follow receipt language ===


class TestEnglishReceipt:
    @pytest.fixture(autouse=True)
    def _parse(self, parser):
        self.result = parser.parse(ENGLISH_HTML)

    def test_english_receipt_prints_english_labels(self):
        assert self.result["invoice_label"] == "Invoice"
        assert self.result["total_label"] == "TOTAL"
        assert self.result["change_label"] == "Change"

    def test_fields(self):
        assert self.result["company"] == "ACME Store"
        assert self.result["invoice_number"] == "INV-001"
        assert self.result["date"] == "2026-07-15 10:00:00"
        assert self.result["customer"] == "John Doe"

    def test_items(self):
        assert self.result["items"] == [
            {"name": "Widget", "qty": 2.0, "rate": 10.00, "amount": 20.00}
        ]

    def test_totals_and_taxes(self):
        assert self.result["net_total"] == 20.00
        assert self.result["total"] == 22.00
        assert self.result["taxes"] == [{"description": "Tax", "amount": 2.00}]
        assert self.result["change"] == 3.00

    def test_footer(self):
        assert self.result["footer_lines"][0].startswith("Thank you")

    def test_no_print_blocks(self):
        assert self.result["print_blocks"] == {
            "header": [],
            "fiscal": [],
            "footer": [],
        }

    def test_not_literal(self):
        assert "literal_lines" not in self.result


# === Literal data-print pass-through blocks (modeled invoice + extras) ===


class TestDataPrintBlocks:
    @pytest.fixture(autouse=True)
    def _parse(self, parser):
        self.result = parser.parse(DATA_PRINT_HTML)

    def test_invoice_still_modeled(self):
        assert self.result["company"] == "ACME Store"
        assert self.result["invoice_number"] == "INV-100"
        assert self.result["total"] == 23.00
        assert self.result["taxes"] == [{"description": "Tax 15%", "amount": 3.00}]
        assert self.result["payments"] == [{"method": "Cash", "amount": 25.00}]

    def test_address_is_not_a_print_block(self):
        address = self.result["company_address"]
        assert "123 Market Street" in address
        assert "Auth Code" not in address

    def test_header_block(self):
        header = self.result["print_blocks"]["header"]
        assert len(header) == 1
        assert header[0]["align"] == "center"
        assert header[0]["bold"] is False
        assert header[0]["lines"] == [
            "Auth Code: ABC-123-XYZ",
            "Authorized Range: 000-001 - 005-000",
        ]

    def test_fiscal_block(self):
        fiscal = self.result["print_blocks"]["fiscal"]
        assert len(fiscal) == 1
        assert fiscal[0]["lines"] == ["Reg ID: 999-8888", "Exemption: EX-2026-7"]

    def test_footer_blocks(self):
        footer = self.result["print_blocks"]["footer"]
        assert len(footer) == 2

        paid, copies = footer
        assert paid["lines"] == ["** PAID **"]
        assert paid["bold"] is True
        assert paid["align"] == "center"

        assert copies["align"] == "right"
        assert copies["lines"] == ["Copy 1: Customer", "Copy 2: Store"]

    def test_not_literal(self):
        assert "literal_lines" not in self.result


# === Literal print mode (format owns the whole layout) ===


class TestLiteralMode:
    @pytest.fixture(autouse=True)
    def _parse(self, parser):
        self.result = parser.parse(LITERAL_HTML)
        self.segments = self.result["literal_lines"]

    def test_opt_in_produces_segments(self):
        assert self.segments, "literal body must produce segments"

    def test_invoice_number_still_extracted_for_job_title(self):
        assert self.result["invoice_number"] == "R-100"

    def test_receipts_without_opt_in_have_no_literal_lines(self, parser):
        for html in (ENGLISH_HTML, DATA_PRINT_HTML):
            assert "literal_lines" not in parser.parse(html)

    def test_company_segment_keeps_classes(self):
        first = self.segments[0]
        assert first == {
            "type": "text",
            "lines": ["ACME STORE"],
            "align": "center",
            "bold": True,
            "large": True,
        }

    def test_br_splits_lines_inside_one_div(self):
        address = self.segments[2]
        assert address["lines"][0] == "123 Market Street"
        assert address["lines"][-1] == "Reg ID: 12-3456789"

    def test_style_block_is_not_content(self):
        texts = [
            line
            for seg in self.segments
            for line in seg.get("lines", [])
        ]
        assert not any("font-family" in t for t in texts)

    def test_table_rows_become_column_segments(self):
        columns = [s for s in self.segments if s["type"] == "columns"]
        assert {"type": "columns", "cells": ["Qty", "Price", "Amount"],
                "bold": False, "large": False} in columns
        assert {"type": "columns", "cells": ["1", "13.91", "13.91"],
                "bold": False, "large": False} in columns

    def test_single_cell_row_is_a_text_line(self):
        item_lines = [
            s for s in self.segments
            if s["type"] == "text"
            and s["lines"] == ["7422300500418 Trigger Spray Cleaner 30g"]
        ]
        assert item_lines and item_lines[0]["bold"] is True

    def test_zero_amount_lines_survive(self):
        # The whole point of literal mode: a fiscal layout shows 0.00 rows.
        texts = [line for s in self.segments for line in s.get("lines", [])]
        assert "Sales Tax 18%: 0.00" in texts
        assert "Discount: 0.00" in texts

    def test_tax_breakdown_is_right_aligned(self):
        subtotal = next(
            s for s in self.segments
            if s.get("lines") == ["Subtotal: 13.91"]
        )
        assert subtotal["align"] == "right"

    def test_spacer_and_document_order(self):
        kinds = [s["type"] for s in self.segments]
        assert "blank" in kinds
        texts = [line for s in self.segments for line in s.get("lines", [])]
        assert texts.index("Receipt No: R-100") < texts.index(
            "Paid with CASH: 16.00"
        ) < texts.index("Please retain this receipt.")

    def test_internal_spacing_is_preserved(self):
        texts = [line for s in self.segments for line in s.get("lines", [])]
        assert "Original: Customer   Copy: Store" in texts


# Regression: the opt-in attribute must not depend on the format's own <body>
# surviving — the ERPNext/POSAwesome print pipeline re-wraps or strips it.


def test_labels_follow_the_receipt_language(parser):
    """Modeled mode must print the receipt's own labels (e.g. Dutch), not a
    fixed English set. English stays the fallback for absent sections."""
    html = """<body>
    <div class="center bold large">Hartley's</div>
    <div class="center">Bon: ACC-1<br>2026-08-05 23:23:36</div>
    <div class="bold">ARTIKELEN</div>
    <table>
      <tr><td colspan="2">Widget</td></tr>
      <tr><td>&nbsp;1.0 x 4.45</td><td>4.45</td></tr>
    </table>
    <table>
      <tr><td>Subtotaal:</td><td>4.11</td></tr>
      <tr><td>BTW 9% over 4.11:</td><td>0.37</td></tr>
      <tr><td>Totaal:</td><td>4.45</td></tr>
    </table>
    <div class="center">Bedankt voor uw aankoop!</div>
    </body>"""

    result = parser.parse(html)
    assert result["invoice_label"] == "Bon"
    assert result["items_label"] == "ARTIKELEN"
    assert result["subtotal_label"] == "Subtotaal"
    assert result["total_label"] == "Totaal"
    # Sections absent from the receipt keep the English fallback
    assert result["cashier_label"] == "Cashier"
    assert result["discount_label"] == "Discount"


def test_totaal_label_not_shadowed_by_subtotaal(parser):
    html = """<body><div class="bold large">Shop</div>
    <table><tr><td>Subtotaal:</td><td>4.11</td></tr>
    <tr><td>Totaal:</td><td>4.45</td></tr></table></body>"""
    result = parser.parse(html)
    assert result["subtotal_label"] == "Subtotaal"
    assert result["total_label"] == "Totaal"


def test_literal_large_column_row_keeps_large_flag(parser):
    html = ('<div data-print-mode="literal"><table>'
            '<tr class="bold large"><td>Totaal:</td><td>34.90</td></tr>'
            "</table></div>")
    assert parser.parse(html)["literal_lines"] == [
        {"type": "columns", "cells": ["Totaal:", "34.90"],
         "bold": True, "large": True}
    ]


@pytest.mark.parametrize(
    "body_line, expected_date",
    [
        # ISO with time (and microseconds trimmed)
        ("Bon: X<br>2026-08-05 23:23:36.482170", "2026-08-05 23:23:36"),
        # Local formats ERPNext get_formatted produces
        ("Bon: X<br>05-08-2026 23:23:36", "05-08-2026 23:23:36"),
        ("Bon: X<br>5-8-2026 23:23", "5-8-2026 23:23"),
        ("Bon: X<br>05.08.2026 23:23", "05.08.2026 23:23"),
        # Date only
        ("Bon: X<br>05-08-2026", "05-08-2026"),
        # Date and time on the same line but separated by other words
        ("Bon: X<br>Issued: 05-08-2026 Time: 23:23:36", "05-08-2026 23:23:36"),
    ],
)
def test_date_and_time_survive_all_common_formats(parser, body_line, expected_date):
    """The date/time line must never get lost on the modeled ticket."""
    html = (
        '<body><div class="bold large">Shop</div>'
        f"<div>{body_line}</div>"
        '<table><tr><td>Totaal:</td><td>1.00</td></tr></table></body>'
    )
    result = parser.parse(html)
    assert result["date"] == expected_date
    assert result["invoice_number"] == "X"


def test_minified_html_does_not_bleed_into_labeled_values(parser):
    """Minified HTML (no whitespace between tags) used to glue the whole
    document into the invoice number."""
    html = (
        '<body><div class="bold large">S</div>'
        '<div>Bon: ACC-1<br>2026-08-05 23:23:36</div>'
        '<table><tr><td>Totaal:</td><td>1</td></tr></table></body>'
    )
    result = parser.parse(html)
    assert result["invoice_number"] == "ACC-1"
    assert result["date"] == "2026-08-05 23:23:36"


def test_item_dimensions_do_not_create_phantom_items(parser):
    """A description containing 32x48 must not be parsed as qty x rate."""
    html = """<body>
    <div class="bold">ITEMS</div>
    <table>
      <tr><td colspan="2">Blossom Garland 32x48 eckig - 40 original - Tischset</td></tr>
      <tr><td>&nbsp;1.0 x 7.95</td><td>7.95</td></tr>
    </table>
    <table>
      <tr><td>Subtotal:</td><td>6.57</td></tr>
      <tr><td>TOTAL:</td><td>7.95</td></tr>
    </table>
    </body>"""

    assert parser.parse(html)["items"] == [
        {
            "name": "Blossom Garland 32x48 eckig - 40 original - Tischset",
            "qty": 1.0,
            "rate": 7.95,
            "amount": 7.95,
        }
    ]


def test_description_only_row_with_dimensions_is_not_an_item(parser):
    html = """<body>
    <div class="bold">ITEMS</div>
    <table>
      <tr><td>Blossom Garland 32x48 eckig</td><td>40 original - Tischset</td></tr>
    </table>
    <table><tr><td>TOTAL:</td><td>7.95</td></tr></table>
    </body>"""

    assert parser.parse(html)["items"] == []


def test_same_row_item_layout_still_parses(parser):
    html = """<body><div class="bold">ITEMS</div><table>
      <tr><td>Widget</td><td>2 x 10.00</td><td>20.00</td></tr>
    </table><table><tr><td>TOTAL:</td><td>20.00</td></tr></table></body>"""

    assert parser.parse(html)["items"] == [
        {"name": "Widget", "qty": 2.0, "rate": 10.0, "amount": 20.0}
    ]


def test_literal_detected_when_pipeline_wraps_the_document(parser):
    wrapped = (
        '<html><head><title>Print</title></head><body>'
        '<div class="print-format">' + LITERAL_HTML + "</div>"
        "</body></html>"
    )
    assert (
        parser.parse(wrapped)["literal_lines"]
        == parser.parse(LITERAL_HTML)["literal_lines"]
    )


def test_literal_detected_when_body_tags_are_stripped(parser):
    inner = LITERAL_HTML.split("<body>", 1)[1].rsplit("</body>", 1)[0]
    assert (
        parser.parse(inner)["literal_lines"]
        == parser.parse(LITERAL_HTML)["literal_lines"]
    )


def test_literal_attribute_on_body_still_works(parser):
    html = '<body data-print-mode="literal"><div class="center">Hola</div></body>'
    assert parser.parse(html)["literal_lines"] == [
        {
            "type": "text",
            "lines": ["Hola"],
            "align": "center",
            "bold": False,
            "large": False,
        }
    ]
