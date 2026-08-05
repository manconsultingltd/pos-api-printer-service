"""
ESC/POS Receipt Generator
Generates ESC/POS commands from a parsed invoice dict (as produced by
html_parser from the rendered receipt HTML - there is no JSON payload).
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from config import Config

logger = logging.getLogger(__name__)


class ESCPOSGenerator:
    """Generates ESC/POS commands for thermal receipt printers"""

    # ESC/POS Commands
    ESC = b"\x1b"
    GS = b"\x1d"

    # Initialize printer
    INIT = ESC + b"@"

    # Text alignment
    ALIGN_LEFT = ESC + b"\x61\x00"
    ALIGN_CENTER = ESC + b"\x61\x01"
    ALIGN_RIGHT = ESC + b"\x61\x02"

    # Text emphasis
    BOLD_ON = ESC + b"\x45\x01"
    BOLD_OFF = ESC + b"\x45\x00"
    DOUBLE_HEIGHT_ON = ESC + b"\x21\x10"
    DOUBLE_WIDTH_ON = ESC + b"\x21\x20"
    DOUBLE_SIZE_ON = ESC + b"\x21\x30"
    TEXT_NORMAL = ESC + b"\x21\x00"

    # Line feed
    LF = b"\x0a"

    # Cut paper
    CUT_FULL = GS + b"\x56\x00"
    CUT_PARTIAL = GS + b"\x56\x01"

    # Cash drawer pulse (pin 2)
    # Format: ESC p m t1 t2
    # m = pin number (0 = pin 2, 1 = pin 5)
    # t1 = ON time (units of 2ms)
    # t2 = OFF time (units of 2ms)
    @staticmethod
    def cash_drawer_pulse() -> bytes:
        """Generate cash drawer open command"""
        on_time = Config.DRAWER_PULSE_ON_TIME // 2  # Convert ms to 2ms units
        off_time = Config.DRAWER_PULSE_OFF_TIME // 2  # Convert ms to 2ms units
        return ESCPOSGenerator.ESC + b"\x70\x00" + bytes([on_time, off_time])

    def __init__(self, paper_width: int = 58):
        """
        Initialize generator

        Args:
            paper_width: Paper width in mm (58 or 80)
        """
        self.paper_width = paper_width
        self.chars_per_line = Config.get_chars_per_line(paper_width)
        self.encoding = Config.ENCODING

    def _text(self, text: str) -> bytes:
        """Convert text to bytes with encoding"""
        try:
            return text.encode(self.encoding, errors="replace")
        except Exception as e:
            logger.error(f"Encoding error: {e}")
            return text.encode("ascii", errors="replace")

    def _line(self, text: str = "") -> bytes:
        """Text line with line feed"""
        return self._text(text) + self.LF

    def _separator(self, char: str = "-") -> bytes:
        """Full-width separator line"""
        return self._line(char * self.chars_per_line)

    def _align_columns(self, left: str, right: str, total_width: int = None) -> str:
        """
        Align text in two columns (left and right aligned)

        Args:
            left: Left-aligned text
            right: Right-aligned text
            total_width: Total width in characters (defaults to chars_per_line)

        Returns:
            Formatted string with proper spacing
        """
        if total_width is None:
            total_width = self.chars_per_line

        # Truncate if too long
        max_left = total_width - len(right) - 1
        if len(left) > max_left:
            left = left[:max_left]

        # Calculate spacing
        spaces = total_width - len(left) - len(right)
        return left + (" " * spaces) + right

    def _wrap_text(
        self, text: str, width: int = None, indent: str = ""
    ) -> List[str]:
        """Word-wrap text to the paper width.

        Continuation lines get `indent` prefixed so wrapped content reads as
        one logical line. Words longer than a whole line are hard-split.
        Returns at least one line (possibly empty) so callers can print
        blank lines unchanged.
        """
        if width is None:
            width = self.chars_per_line
        if len(text) <= width:
            return [text]

        lines: List[str] = []
        current = ""
        for word in text.split(" "):
            prefix = indent if lines else ""
            limit = width - len(prefix)
            candidate = f"{current} {word}" if current else word
            if len(candidate) <= limit:
                current = candidate
                continue
            if current:
                lines.append(prefix + current)
                current = ""
            # Hard-split words longer than a whole line
            while len(word) > width - len(indent if lines else ""):
                prefix = indent if lines else ""
                cut = width - len(prefix)
                lines.append(prefix + word[:cut])
                word = word[cut:]
            current = word
        if current:
            lines.append((indent if lines else "") + current)
        return lines or [""]

    def _print_blocks(
        self, blocks: Optional[List[Dict[str, Any]]], restore_align: bytes
    ) -> bytes:
        """
        Render literal data-print pass-through blocks.

        Print formats use them for content the parser doesn't model (e.g.
        fiscal sections some jurisdictions require). Each block prints its
        lines as-is with the alignment/emphasis the HTML classes carried
        (center/right/bold/large). Returns b"" when there are no blocks, so
        receipts without them stay byte-identical.

        Args:
            blocks: List of {"lines": [...], "align": str, "bold": bool,
                "large": bool} dicts from HtmlReceiptParser
            restore_align: Alignment command to re-emit after the blocks
        """
        if not blocks:
            return b""

        align_map = {
            "center": self.ALIGN_CENTER,
            "right": self.ALIGN_RIGHT,
        }

        out = b""
        for block in blocks:
            out += align_map.get(block.get("align"), self.ALIGN_LEFT)
            if block.get("large"):
                out += self.DOUBLE_HEIGHT_ON
            if block.get("bold"):
                out += self.BOLD_ON
            for line in block.get("lines", []):
                for wrapped in self._wrap_text(str(line), indent="  "):
                    out += self._line(wrapped)
            if block.get("bold"):
                out += self.BOLD_OFF
            if block.get("large"):
                out += self.TEXT_NORMAL

        return out + restore_align

    def _format_currency(self, amount: float, symbol: str = "") -> str:
        """
        Format currency amount with optional symbol

        Args:
            amount: The amount to format
            symbol: Currency symbol (e.g., "€", "$"). Empty string for no symbol.

        Returns:
            Formatted string like "€ 10.50" or "10.50"
        """
        formatted = f"{amount:,.2f}"
        if symbol:
            return f"{symbol} {formatted}"
        return formatted

    def generate_receipt(self, invoice_data: Dict[str, Any]) -> bytes:
        """
        Generate complete receipt from invoice data

        Args:
            invoice_data: Dictionary containing invoice information

        Expected invoice dict (produced by html_parser):
        {
            "company": "Company Name",
            "company_address": "Street 123\\nCity, Country",  # Optional
            "currency_symbol": "€",  # Optional, for currency formatting
            "invoice_number": "INV-001",
            "date": "2025-10-23",
            "cashier": "John Doe",
            "customer": "Walk-in Customer",

            "items_label": "ITEMS",  # Optional, defaults to "ITEMS"
            "items": [
                {
                    "name": "Product Name",
                    "qty": 2,
                    "rate": 10.00,
                    "amount": 20.00
                }
            ],

            "subtotal_label": "Subtotal",  # Optional label
            "subtotal": 20.00,  # Legacy field
            "net_total": 20.00,  # Preferred field (excl. tax)

            "taxes": [  # New: array of tax items
                {"description": "VAT 9%", "rate": 9.0, "amount": 1.80},
                {"description": "VAT 21%", "rate": 21.0, "amount": 4.20}
            ],
            "tax": 2.00,  # Legacy single tax field (fallback)

            "discount_label": "Discount",  # Optional label
            "discount": 0.00,

            "total_label": "TOTAL",  # Optional label
            "total": 22.00,

            "payment_label": "Payment",  # Optional label
            "payment_method": "Cash",

            "paid_label": "Paid",  # Optional label
            "amount_paid": 25.00,

            "change_label": "Change",  # Optional label
            "change": 3.00,

            "footer_lines": ["Thank you!", "VAT: 123456"],  # New: array of footer lines
            "footer_text": "Thank you!",  # Legacy single footer (fallback)

            # Optional literal pass-through blocks from data-print sections
            # of the print format. Each entry:
            # {"lines": [...], "align": "left|center|right", "bold": bool, "large": bool}
            "print_blocks": {"header": [], "fiscal": [], "footer": []}
        }

        Returns:
            ESC/POS command bytes
        """
        # Literal print mode: the print format owns the whole layout
        # (parser sets literal_lines only when the HTML body opts in).
        # Presence, not truthiness: a format that opts into literal mode but
        # whose Jinja conditionals all render empty parses to literal_lines=[]
        # and must still take the literal path (blank paper), not fall through
        # to the modeled path where total=None would 500 the print call.
        literal_lines = invoice_data.get("literal_lines")
        if literal_lines is not None:
            return self.generate_literal_receipt(literal_lines)

        receipt = b""

        # Get currency symbol from JSON (default empty for backwards compatibility)
        currency = invoice_data.get("currency_symbol", "")

        # Literal pass-through blocks (data-print="header|fiscal|footer")
        # declared by the print format. Missing/empty -> nothing extra prints.
        print_blocks = invoice_data.get("print_blocks") or {}

        # Initialize printer
        receipt += self.INIT

        # Header - Company name (centered, bold, double size)
        receipt += self.ALIGN_CENTER
        receipt += self.BOLD_ON
        receipt += self.DOUBLE_SIZE_ON
        receipt += self._line(invoice_data.get("company", "POS"))
        receipt += self.TEXT_NORMAL
        receipt += self.BOLD_OFF

        # Company address (if provided) - centered, normal size
        company_address = invoice_data.get("company_address", "")
        if company_address:
            for line in company_address.split("\n"):
                if line.strip():
                    receipt += self._line(line.strip())

        # data-print="header" blocks (e.g. fiscal auth codes / authorized range)
        receipt += self._print_blocks(print_blocks.get("header"), self.ALIGN_CENTER)

        receipt += self.LF

        # Invoice details (centered)
        invoice_label = invoice_data.get("invoice_label", "Invoice")
        receipt += self._line(
            f"{invoice_label}: {invoice_data.get('invoice_number', 'N/A')}"
        )
        receipt += self._line(
            invoice_data.get("date", datetime.now().strftime("%Y-%m-%d %H:%M"))
        )

        if invoice_data.get("cashier"):
            cashier_label = invoice_data.get("cashier_label", "Cashier")
            receipt += self._line(f"{cashier_label}: {invoice_data['cashier']}")

        if invoice_data.get("customer"):
            customer_label = invoice_data.get("customer_label", "Customer")
            receipt += self._line(f"{customer_label}: {invoice_data['customer']}")

        # data-print="fiscal" blocks (e.g. customer tax id / exemption)
        receipt += self._print_blocks(print_blocks.get("fiscal"), self.ALIGN_CENTER)

        receipt += self.LF

        # Items section
        receipt += self.ALIGN_LEFT
        receipt += self._separator("=")
        receipt += self.BOLD_ON
        items_label = invoice_data.get("items_label", "ITEMS")
        receipt += self._line(items_label)
        receipt += self.BOLD_OFF
        receipt += self._separator("-")

        # Item list
        items = invoice_data.get("items", [])
        for item in items:
            item_name = item.get("name", "Unknown")
            qty = item.get("qty", 1)
            rate = item.get("rate", 0.0)
            amount = item.get("amount", 0.0)

            # Item name: word-wrap across as many lines as needed — long
            # names must never be truncated ("Knife: 18.6cm" -> "Knife: 18.")
            for name_line in self._wrap_text(item_name, indent="  "):
                receipt += self._line(name_line)

            # Qty x Rate = Amount
            qty_rate = f"  {qty} x {self._format_currency(rate, currency)}"
            amount_str = self._format_currency(amount, currency)
            receipt += self._line(self._align_columns(qty_rate, amount_str))
            receipt += self.LF

        # Totals section
        receipt += self._separator("=")

        # Subtotal / Net total (use net_total if available, fallback to subtotal)
        subtotal_label = invoice_data.get("subtotal_label", "Subtotal")
        net_total = invoice_data.get("net_total")
        if net_total is not None:
            receipt += self._line(
                self._align_columns(
                    f"{subtotal_label}:", self._format_currency(net_total, currency)
                )
            )
        else:
            subtotal = invoice_data.get("subtotal", 0.0)
            receipt += self._line(
                self._align_columns(
                    f"{subtotal_label}:", self._format_currency(subtotal, currency)
                )
            )

        # Taxes - support both array (new) and single value (legacy)
        taxes = invoice_data.get("taxes", [])
        if taxes:
            # New: multiple tax lines from array
            for tax_item in taxes:
                tax_amount = tax_item.get("amount", 0)
                if tax_amount > 0:
                    tax_desc = tax_item.get("description", "Tax")
                    receipt += self._line(
                        self._align_columns(
                            f"{tax_desc}:", self._format_currency(tax_amount, currency)
                        )
                    )
        else:
            # Legacy: single tax field
            tax = invoice_data.get("tax", 0.0)
            if tax > 0:
                tax_label = invoice_data.get("tax_label", "Tax")
                receipt += self._line(
                    self._align_columns(
                        f"{tax_label}:", self._format_currency(tax, currency)
                    )
                )

        # Discount (if applicable)
        discount = invoice_data.get("discount", 0.0)
        if discount > 0:
            discount_label = invoice_data.get("discount_label", "Discount")
            receipt += self._line(
                self._align_columns(
                    f"{discount_label}:",
                    f"-{self._format_currency(discount, currency)}",
                )
            )

        # Total (bold, larger)
        receipt += self._separator("-")
        receipt += self.BOLD_ON
        receipt += self.DOUBLE_HEIGHT_ON
        total = invoice_data.get("total", 0.0)
        total_label = invoice_data.get("total_label", "TOTAL")
        receipt += self._line(
            self._align_columns(
                f"{total_label}:", self._format_currency(total, currency)
            )
        )
        receipt += self.TEXT_NORMAL
        receipt += self.BOLD_OFF
        receipt += self._separator("-")

        # Payment details - support multiple payments
        payments = invoice_data.get("payments", [])
        if payments:
            # New: multiple payments array
            for payment in payments:
                method = payment.get("method", "")
                amount = payment.get("amount", 0)
                if method and amount > 0:
                    receipt += self._line(
                        self._align_columns(
                            f"{method}:", self._format_currency(amount, currency)
                        )
                    )
        else:
            # Legacy: single payment_method and amount_paid
            payment_label = invoice_data.get("payment_label", "Payment")
            payment_method = invoice_data.get("payment_method", "Cash")
            receipt += self._line(
                self._align_columns(f"{payment_label}:", payment_method)
            )

            amount_paid = invoice_data.get("amount_paid", 0.0)
            if amount_paid > 0:
                paid_label = invoice_data.get("paid_label", "Paid")
                receipt += self._line(
                    self._align_columns(
                        f"{paid_label}:", self._format_currency(amount_paid, currency)
                    )
                )

        change = invoice_data.get("change", 0.0)
        if change > 0:
            change_label = invoice_data.get("change_label", "Change")
            receipt += self._line(
                self._align_columns(
                    f"{change_label}:", self._format_currency(change, currency)
                )
            )

        receipt += self.LF

        # Footer
        receipt += self.ALIGN_CENTER
        receipt += self._separator("=")

        # Support both footer_lines array (new) and footer_text string (legacy)
        footer_lines = invoice_data.get("footer_lines")
        if footer_lines and isinstance(footer_lines, list):
            # New: multiple footer lines from array
            for line in footer_lines:
                if line:
                    # Handle multiline strings within array items
                    for subline in str(line).split("\n"):
                        if subline.strip():
                            receipt += self._line(subline.strip())
        else:
            # Legacy: single footer text (may contain \n)
            footer_text = invoice_data.get("footer_text", "Thank you!")
            for line in footer_text.split("\n"):
                if line.strip():
                    receipt += self._line(line.strip())

        # data-print="footer" blocks (e.g. amount in words, status seal,
        # copies, legal phrases)
        receipt += self._print_blocks(print_blocks.get("footer"), self.ALIGN_CENTER)

        receipt += self.LF
        receipt += self.LF

        # Cut paper
        receipt += self.CUT_PARTIAL

        return receipt

    def generate_literal_receipt(self, segments: List[Dict[str, Any]]) -> bytes:
        """
        Render a receipt whose layout is fully owned by the print format
        (literal print mode, see HtmlReceiptParser._extract_literal_lines).

        No modeled sections, separators or labels are added: each segment
        prints exactly as extracted, then the paper is cut.

        Args:
            segments: List of segment dicts —
                {"type": "text", "lines": [...], "align", "bold", "large"}
                {"type": "columns", "cells": [...], "bold"}
                {"type": "separator"} / {"type": "blank"}

        Returns:
            ESC/POS command bytes
        """
        align_map = {
            "center": self.ALIGN_CENTER,
            "right": self.ALIGN_RIGHT,
        }

        receipt = self.INIT

        for segment in segments:
            kind = segment.get("type")

            if kind == "separator":
                receipt += self.ALIGN_LEFT
                receipt += self._separator("-")
                continue

            if kind == "blank":
                receipt += self.LF
                continue

            if kind == "columns":
                cells = [str(cell) for cell in segment.get("cells", [])]
                receipt += self.ALIGN_LEFT
                # In double-width mode the printer fits half the characters
                # per line, so column math must use the reduced width.
                large = bool(segment.get("large"))
                width = self.chars_per_line // 2 if large else None
                if large:
                    receipt += self.DOUBLE_HEIGHT_ON
                if segment.get("bold"):
                    receipt += self.BOLD_ON
                receipt += self._line(self._layout_columns(cells, width))
                if segment.get("bold"):
                    receipt += self.BOLD_OFF
                if large:
                    receipt += self.TEXT_NORMAL
                continue

            if kind == "text":
                receipt += align_map.get(segment.get("align"), self.ALIGN_LEFT)
                large = bool(segment.get("large"))
                # Double-size text halves the characters per line
                wrap_width = self.chars_per_line // 2 if large else None
                if large:
                    receipt += self.DOUBLE_SIZE_ON
                if segment.get("bold"):
                    receipt += self.BOLD_ON
                for line in segment.get("lines", []):
                    # Word-wrap to the paper width: on narrow paper the
                    # printer would otherwise break mid-word at the edge.
                    for wrapped in self._wrap_text(str(line), wrap_width, indent="  "):
                        receipt += self._line(wrapped)
                if segment.get("bold"):
                    receipt += self.BOLD_OFF
                if large:
                    receipt += self.TEXT_NORMAL

        receipt += self.ALIGN_LEFT
        receipt += self.LF
        receipt += self.LF
        receipt += self.CUT_PARTIAL

        return receipt

    def _layout_columns(self, cells: List[str], width: int = None) -> str:
        """Lay one table row out on a single line: first cell left-aligned,
        last cell right-aligned, middle cells centered in between."""
        if width is None:
            width = self.chars_per_line
        if not cells:
            return ""
        if len(cells) == 1:
            return cells[0]
        if len(cells) == 2:
            return self._align_columns(cells[0], cells[1], width)
        line = [" "] * width

        def put(text: str, start: int) -> None:
            start = max(0, min(start, width - len(text)))
            for offset, char in enumerate(text):
                pos = start + offset
                if not 0 <= pos < width:
                    break
                # Stop at a collision instead of silently overwriting an
                # already-placed cell: on a too-narrow line the row is
                # truncated visibly rather than scrambling both cells.
                if line[pos] != " ":
                    break
                line[pos] = char

        left, right = cells[0], cells[-1]
        middles = cells[1:-1]

        put(left, 0)
        put(right, width - len(right))

        inner_start = len(left) + 1
        inner_end = width - len(right) - 1
        span = max(inner_end - inner_start, 1)
        for index, middle in enumerate(middles):
            center = inner_start + span * (index + 1) // (len(middles) + 1)
            put(middle, center - len(middle) // 2)

        return "".join(line).rstrip()

    def generate_test_receipt(self) -> bytes:
        """
        Generate a test receipt for printer testing

        Returns:
            ESC/POS command bytes
        """
        test_data = {
            "company": "TEST PRINT",
            "company_address": "Test Street 123\n1234 AB Test City",
            "currency_symbol": "",
            "invoice_number": "TEST-001",
            "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "cashier": "System",
            "customer": "Test Customer",
            "items_label": "ITEMS",
            "items": [
                {"name": "Test Item 1", "qty": 1, "rate": 10.00, "amount": 10.00},
                {
                    "name": "Test Item with a Very Long Name That Wraps",
                    "qty": 2,
                    "rate": 5.50,
                    "amount": 11.00,
                },
            ],
            "subtotal_label": "Subtotal",
            "net_total": 19.27,
            "taxes": [
                {"description": "Tax 9%", "rate": 9.0, "amount": 0.83},
                {"description": "Tax 21%", "rate": 21.0, "amount": 2.00},
            ],
            "discount_label": "Discount",
            "discount": 1.00,
            "total_label": "TOTAL",
            "total": 22.10,
            "payment_label": "Payment",
            "payment_method": "Cash",
            "paid_label": "Paid",
            "amount_paid": 25.00,
            "change_label": "Change",
            "change": 2.90,
            "footer_lines": [
                "*** TEST RECEIPT ***",
                "Printer is working correctly!",
            ],
        }

        return self.generate_receipt(test_data)
