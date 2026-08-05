"""
HTML Receipt Parser
Parses HTML receipt content and extracts structured invoice data for ESC/POS generation.

This eliminates the need for parallel JSON templates - just parse the HTML directly.
"""

import logging
import re
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)


class HtmlReceiptParser:
    """
    Parse HTML receipt to extract structured invoice data.

    Two modes:

    * Modeled mode (default) — the parser recognizes a fixed receipt shape
      (company, items, totals, taxes, payments) and the ESC/POS generator
      lays it out on the ticket. Values are located with multilingual text
      anchors (e.g. "Totaal:"/"Total:"), but the printed structural labels
      are the fixed English set (Invoice/Cashier/TOTAL/…). Content the
      parser doesn't model can travel in literal data-print blocks; see
      _extract_print_blocks.

    * Literal mode — a print format opts in with data-print-mode="literal"
      and owns the whole layout; the service prints exactly what the format
      renders (see _extract_literal_lines). This is how fully-localized or
      jurisdiction-specific (fiscal) tickets stay entirely in the print
      format instead of being encoded in the service.
    """

    CURRENCY_SYMBOLS = ["€", "$", "£", "¥", "₽", "EUR"]

    # Currency token inside amounts — symbol characters only. Textual
    # currency names are ambiguous (e.g. "L" is also liters / a size), so
    # they are not auto-detected; a format that needs a textual currency
    # renders it itself and prints via literal mode.
    _CURRENCY_TOKEN = r"[€$£¥₽]"

    # Regex pattern for amounts: € 123.45, €123,45, 123.45, -€ 5.00
    AMOUNT_PATTERN = re.compile(
        rf"{_CURRENCY_TOKEN}?\s*(-?)\s*{_CURRENCY_TOKEN}?\s*([\d.,]+)"
    )

    # Qty x rate on an item row: "2 x 10.00", "3 × € 5,00"
    QTY_RATE_PATTERN = re.compile(
        rf"(\d+(?:[.,]\d+)?)\s*[x×]\s*{_CURRENCY_TOKEN}?\s*([\d.,]+)"
    )

    # A table quantity cell must start with the qty/rate expression. Item
    # descriptions frequently contain dimensions such as "32x48"; searching
    # the whole row makes those descriptions look like an extra line item.
    TABLE_QTY_RATE_PATTERN = re.compile(
        rf"^\s*(\d+(?:[.,]\d+)?)\s*[x×]\s*{_CURRENCY_TOKEN}?\s*([\d.,]+)"
    )

    def _parse_amount_match(self, match) -> float:
        """Parse amount from regex match, including negative sign."""
        if not match:
            return 0.0
        sign = match.group(1)  # "-" or ""
        number = self._parse_number(match.group(2))
        return -number if sign == "-" else number

    # Text anchors used to LOCATE values in the rendered HTML. These stay
    # multilingual so a receipt rendered in any of these languages still
    # parses; they do not decide the printed labels (those are fixed English,
    # see ENGLISH_LABELS).
    LABELS = {
        "invoice": [
            "Bon:",
            "Invoice:",
            "Factuurnummer:",
            "Receipt:",
            "Receipt No:",
            "Bonnummer:",
            "Factura:",
        ],
        "date": ["Datum:", "Date:", "Fecha:"],
        "cashier": ["Kassier:", "Cashier:", "Medewerker:", "Cajero:"],
        "customer": ["Klant:", "Customer:", "Cliënt:", "Cliente:"],
        "items": [
            "ARTIKELEN",
            "ITEMS",
            "Artikelen",
            "Items",
            "Producten",
            "ARTICULOS",
            "Articulos",
        ],
        "subtotal": ["Subtotaal", "Subtotal", "Netto"],
        "tax": ["BTW", "VAT", "Tax", "Belasting"],
        "discount": ["Korting", "Discount", "Afslag", "Descuento"],
        "total": ["Totaal", "Total", "TOTAAL", "TOTAL", "Te betalen"],
        "payment": ["Betaalmethode:", "Payment:", "Betaalwijze:", "Pago:"],
        "paid": ["Betaald:", "Paid:", "Ontvangen:", "Pagado:"],
        "change": ["Wisselgeld:", "Change:", "Terug:", "Cambio:"],
        "footer": ["Bedankt", "Thank you", "Dank u", "Gracias"],
    }

    # Fixed English structural labels the ESC/POS generator prints in modeled
    # mode, regardless of the source receipt's language. A format that needs
    # localized wording uses literal mode.
    ENGLISH_LABELS = {
        "invoice_label": "Invoice",
        "cashier_label": "Cashier",
        "customer_label": "Customer",
        "items_label": "ITEMS",
        "subtotal_label": "Subtotal",
        "tax_label": "Tax",
        "discount_label": "Discount",
        "total_label": "TOTAL",
        "payment_label": "Payment",
        "paid_label": "Paid",
        "change_label": "Change",
    }

    # Non-content tags skipped while walking a literal-mode body
    LITERAL_SKIP_TAGS = {"script", "style", "template", "title", "head"}

    def parse(self, html: str) -> Dict[str, Any]:
        """
        Parse HTML and return invoice dict compatible with ESCPOSGenerator.

        Args:
            html: Raw HTML content of the receipt

        Returns:
            Dictionary with invoice data (modeled mode) plus, when the format
            opts in, a ``literal_lines`` layout for literal mode.
        """
        soup = BeautifulSoup(html, "html.parser")

        # Extract currency symbol (but we won't use € for printing)
        currency_symbol = self._detect_currency(soup)
        # Don't pass € to printer - it can't print it
        printable_currency = "" if currency_symbol == "€" else currency_symbol

        # Extract all components
        result = {
            "company": self._extract_company(soup),
            "company_address": self._extract_company_address(soup),
            "invoice_number": self._extract_labeled_value(soup, self.LABELS["invoice"]),
            "date": self._extract_date(soup),
            "cashier": self._extract_labeled_value(soup, self.LABELS["cashier"]),
            "customer": self._extract_labeled_value(soup, self.LABELS["customer"]),
            "items": self._extract_items(soup),
            "net_total": self._extract_labeled_amount(soup, self.LABELS["subtotal"]),
            "taxes": self._extract_taxes(soup),
            "discount": self._extract_labeled_amount(soup, self.LABELS["discount"])
            or 0,
            "total": self._extract_labeled_amount(soup, self.LABELS["total"]),
            "payments": self._extract_payments(soup),
            "change": self._extract_labeled_amount(soup, self.LABELS["change"]) or 0,
            "footer_lines": self._extract_footer(soup),
            "currency_symbol": printable_currency,  # Empty for € (not printable)
            "print_blocks": self._extract_print_blocks(soup),
        }

        # Literal print mode: the print format takes over the whole layout.
        # Extracted last because it rewrites <br> tags inside the soup.
        literal_lines = self._extract_literal_lines(soup)
        if literal_lines is not None:
            result["literal_lines"] = literal_lines

        # For backwards compatibility, also set payment_method and amount_paid
        if result["payments"]:
            result["payment_method"] = result["payments"][0].get("method", "")
            result["amount_paid"] = sum(p.get("amount", 0) for p in result["payments"])
        else:
            result["payment_method"] = ""
            result["amount_paid"] = 0

        # Add the fixed English structural labels for the ESC/POS generator
        result.update(self.ENGLISH_LABELS)

        logger.info(
            f"Parsed HTML receipt: {result.get('invoice_number')}, "
            f"{len(result.get('items', []))} items, "
            f"total: {result.get('total')}, "
            f"literal: {'literal_lines' in result}"
        )
        logger.debug(f"Full parsed data: {result}")

        return result

    def _detect_currency(self, soup: BeautifulSoup) -> str:
        """Detect currency symbol from the HTML content.

        Only unambiguous symbol characters are recognized. Textual currency
        names are not guessed; a format needing one uses literal mode.
        """
        text = soup.get_text()
        for symbol in self.CURRENCY_SYMBOLS:
            if symbol in text:
                return symbol
        return ""

    def _extract_company(self, soup: BeautifulSoup) -> str:
        """Extract company name - usually first bold/large element."""
        for selector in [
            ".center.bold.large",
            ".bold.large",
            ".company-name",
            "h1",
            "h2",
        ]:
            element = soup.select_one(selector)
            if element:
                return element.get_text(strip=True)

        body = soup.find("body")
        if body:
            bold = body.find(["strong", "b"])
            if bold:
                return bold.get_text(strip=True)

            bold_div = body.find(class_=lambda c: c and "bold" in c)
            if bold_div:
                return bold_div.get_text(strip=True)

        return ""

    def _extract_company_address(self, soup: BeautifulSoup) -> str:
        """Extract company address - element with 'small' class right after company."""
        # Look for element with 'small' class. Literal data-print blocks
        # share these classes and must not be mistaken for the address.
        small = soup.select_one(".center.small:not([data-print])")
        if small:
            text = small.get_text("\n", strip=True)
            return text

        addr = soup.select_one(".address, .company-address")
        if addr:
            return addr.get_text("\n", strip=True)

        return ""

    def _extract_labeled_value(
        self, soup: BeautifulSoup, labels: List[str]
    ) -> Optional[str]:
        """Extract value that follows a label (e.g., 'Kassier: John' -> 'John')."""
        text = soup.get_text()

        for label in labels:
            pattern = re.compile(rf"{re.escape(label)}\s*([^\n<]+)", re.IGNORECASE)
            match = pattern.search(text)
            if match:
                value = match.group(1).strip()
                value = re.sub(r"<[^>]+>", "", value)
                return value

        return None

    def _extract_date(self, soup: BeautifulSoup) -> str:
        """Extract date/time from receipt."""
        text = soup.get_text()

        date_value = self._extract_labeled_value(soup, self.LABELS["date"])
        if date_value:
            return date_value

        date_patterns = [
            r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?",
            r"\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2}",
            r"\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}",
        ]

        for pattern in date_patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(0)

        return ""

    def _extract_items(self, soup: BeautifulSoup) -> List[Dict[str, Any]]:
        """Extract line items from the receipt."""
        items = []
        tables = soup.find_all("table")

        for table in tables:
            rows = table.find_all("tr")

            i = 0
            while i < len(rows):
                row = rows[i]
                cells = row.find_all("td")

                if not cells:
                    i += 1
                    continue

                # Locate the qty/rate expression in an individual cell and
                # require a separate amount cell after it. Searching row_text
                # used to interpret dimensions in a description (for example
                # "Blossom Garland 32x48 ...") as a phantom 32 x 48 item.
                qty_cell_index = None
                qty_rate_match = None
                for cell_index, cell in enumerate(cells[:-1]):
                    match = self.TABLE_QTY_RATE_PATTERN.search(cell.get_text())
                    if match:
                        qty_cell_index = cell_index
                        qty_rate_match = match
                        break

                if qty_rate_match is not None:
                    qty = self._parse_number(qty_rate_match.group(1))
                    rate = self._parse_number(qty_rate_match.group(2))

                    amount_text = cells[-1].get_text()
                    amount_match = self.AMOUNT_PATTERN.search(amount_text)
                    amount = (
                        self._parse_amount_match(amount_match)
                        if amount_match
                        else qty * rate
                    )

                    # In the usual two-cell layout the item name occupies the
                    # preceding row. Wider layouts may put it in cells before
                    # the quantity cell on the same row.
                    if qty_cell_index == 0 and i > 0:
                        prev_row = rows[i - 1]
                        prev_cells = prev_row.find_all("td")
                        if prev_cells:
                            item_name = prev_cells[0].get_text(strip=True)
                        else:
                            item_name = prev_row.get_text(strip=True)
                    else:
                        item_name = " ".join(
                            cell.get_text(" ", strip=True)
                            for cell in cells[:qty_cell_index]
                        ).strip()

                    if item_name and qty > 0:
                        items.append(
                            {
                                "name": item_name,
                                "qty": qty,
                                "rate": rate,
                                "amount": amount,
                            }
                        )

                i += 1

        if not items:
            items = self._extract_items_from_text(soup)

        return items

    def _extract_items_from_text(self, soup: BeautifulSoup) -> List[Dict[str, Any]]:
        """Fallback: extract items from plain text."""
        items = []
        text = soup.get_text()

        items_start = None
        for label in self.LABELS["items"]:
            match = re.search(label, text, re.IGNORECASE)
            if match:
                items_start = match.end()
                break

        if items_start is None:
            return items

        items_end = len(text)
        for label in self.LABELS["subtotal"] + self.LABELS["total"]:
            match = re.search(label, text[items_start:], re.IGNORECASE)
            if match:
                items_end = items_start + match.start()
                break

        items_text = text[items_start:items_end]
        lines = items_text.split("\n")
        current_item_name = None

        for line in lines:
            line = line.strip()
            if not line:
                continue

            qty_match = self.QTY_RATE_PATTERN.search(line)

            if qty_match:
                qty = self._parse_number(qty_match.group(1))
                rate = self._parse_number(qty_match.group(2))

                amount_match = re.search(r"[€$£¥₽]?\s*([\d.,]+)\s*$", line)
                amount = (
                    self._parse_number(amount_match.group(1))
                    if amount_match
                    else qty * rate
                )

                if current_item_name:
                    items.append(
                        {
                            "name": current_item_name,
                            "qty": qty,
                            "rate": rate,
                            "amount": amount,
                        }
                    )
                    current_item_name = None
            else:
                if line and not any(
                    label in line for labels in self.LABELS.values() for label in labels
                ):
                    current_item_name = line

        return items

    def _extract_payments(self, soup: BeautifulSoup) -> List[Dict[str, Any]]:
        """Extract payment methods and amounts from receipt."""
        payments = []

        # Known payment method names (matched as a substring of the row's
        # first cell, case-insensitively).
        payment_methods = [
            "Contant",
            "Cash",
            "Kredietkaart",
            "Credit Card",
            "Card",
            "ING Bank",
            "Bank",
            "overboeking",
            "Voucher",
            "Cheque",
            "Bankcheque",
            "Pin",
            "Debit",
            "Maestro",
            "Visa",
            "Mastercard",
        ]

        # Look in tables for payment rows
        for table in soup.find_all("table"):
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) >= 2:
                    first_cell = cells[0].get_text().strip()
                    # Remove trailing colon
                    first_cell_clean = first_cell.rstrip(":")

                    # Check if this is a payment method
                    for method in payment_methods:
                        if method.lower() in first_cell_clean.lower():
                            amount_text = cells[-1].get_text()
                            amount_match = self.AMOUNT_PATTERN.search(amount_text)
                            if amount_match:
                                amount = self._parse_amount_match(amount_match)
                                if amount != 0:
                                    payments.append(
                                        {"method": first_cell_clean, "amount": amount}
                                    )
                            break

        return payments

    def _extract_taxes(self, soup: BeautifulSoup) -> List[Dict[str, Any]]:
        """Extract tax lines (can be multiple for different VAT rates)."""
        taxes = []
        text = soup.get_text()

        # Pattern for tax lines: BTW 9%, VAT 21%, Tax 15% followed by an
        # amount. Match the description up to the colon, then the amount.
        tax_pattern = re.compile(
            r"((?:BTW|VAT|Tax)[^:]*?):\s*[€$£¥₽]?\s*([\d.,]+)", re.IGNORECASE
        )

        for match in tax_pattern.finditer(text):
            description = match.group(1).strip()
            amount = self._parse_number(match.group(2))

            if amount > 0:
                taxes.append({"description": description, "amount": amount})

        return taxes

    def _extract_labeled_amount(
        self, soup: BeautifulSoup, labels: List[str]
    ) -> Optional[float]:
        """Extract numeric amount after a label using table rows for accuracy."""
        # First try table-based extraction (most reliable)
        for table in soup.find_all("table"):
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if not cells or len(cells) < 2:
                    continue

                first_cell_text = cells[0].get_text().strip().lower()

                for label in labels:
                    # Match label at start of cell (not inside another word)
                    if first_cell_text.startswith(label.lower()):
                        amount_text = cells[-1].get_text()
                        amount_match = self.AMOUNT_PATTERN.search(amount_text)
                        if amount_match:
                            return self._parse_amount_match(amount_match)

        # Fallback: regex with word boundary
        text = soup.get_text()
        for label in labels:
            # Use word boundary to avoid matching "Subtotaal" when looking for "Totaal"
            pattern = re.compile(
                rf"(?<![a-zA-Z]){re.escape(label)}[^€$£¥₽\d]*[€$£¥₽]?\s*(-?[\d.,]+)",
                re.IGNORECASE,
            )
            match = pattern.search(text)
            if match:
                return self._parse_number(match.group(1))

        return None

    def _extract_footer(self, soup: BeautifulSoup) -> List[str]:
        """Extract footer text - only the thank you message, not address."""
        footer_lines = []

        body = soup.find("body")
        if not body:
            return footer_lines

        # Find divs with center class
        center_divs = body.find_all(class_=lambda c: c and "center" in c)

        if center_divs:
            # Look for footer keywords in the last few divs
            for div in reversed(center_divs):
                text = div.get_text(strip=True)

                # Skip empty, header (company name), and address (has newlines or is 'small')
                if not text:
                    continue

                # Literal data-print blocks print on their own; never as footer
                if div.get("data-print"):
                    continue

                # Skip if it's the company name (first center div or has 'large' class)
                if div == center_divs[0]:
                    continue
                if div.get("class") and any("large" in c for c in div.get("class", [])):
                    continue
                if div.get("class") and any("small" in c for c in div.get("class", [])):
                    continue

                # Skip if it contains invoice/cashier/customer info
                skip_patterns = [
                    "Bon:",
                    "Invoice:",
                    "Kassier:",
                    "Cashier:",
                    "Klant:",
                    "Customer:",
                ]
                if any(pat in text for pat in skip_patterns):
                    continue

                # Only include if it looks like a footer message
                footer_keywords = [
                    "bedankt",
                    "thank",
                    "dank",
                    "welkom",
                    "welcome",
                    "gracias",
                ]
                if any(kw in text.lower() for kw in footer_keywords):
                    footer_lines.insert(0, text)
                    break

        # Default footer if none found
        if not footer_lines:
            footer_lines = ["Thank you for your purchase!"]

        return footer_lines

    def _extract_print_blocks(self, soup: BeautifulSoup) -> Dict[str, List[Dict]]:
        """Extract literal data-print pass-through blocks.

        Print formats mark content the parser doesn't model with
        data-print="header|fiscal|footer" (e.g. jurisdiction-specific fiscal
        sections). Each block is printed as-is by the generator, in document
        order, at:
          header -> after the company address
          fiscal -> after the customer line
          footer -> at the very end, after the thank-you footer

        Alignment/emphasis carry over from the div's CSS classes
        (center/right/bold/large); one printed line per <br>-separated line.
        """
        blocks: Dict[str, List[Dict]] = {"header": [], "fiscal": [], "footer": []}

        for div in soup.find_all(attrs={"data-print": True}):
            section = (div.get("data-print") or "").strip().lower()
            if section not in blocks:
                continue

            lines = [
                line.strip()
                for line in div.get_text("\n", strip=True).split("\n")
                if line.strip()
            ]
            if not lines:
                continue

            classes = div.get("class") or []
            if "right" in classes:
                align = "right"
            elif "center" in classes:
                align = "center"
            else:
                align = "left"

            blocks[section].append(
                {
                    "lines": lines,
                    "align": align,
                    "bold": "bold" in classes,
                    "large": "large" in classes,
                }
            )

        return blocks

    def _extract_literal_lines(
        self, soup: BeautifulSoup
    ) -> Optional[List[Dict[str, Any]]]:
        """Extract the full receipt layout in literal print mode.

        A print format opts in by putting data-print-mode="literal" on the
        element that wraps the whole receipt (a <div> — NOT <body>: the
        ERPNext/POSAwesome print pipeline re-wraps or strips the format's
        own <body> when rendering, which silently loses attributes on it).
        The generator then prints exactly these segments — no modeled
        sections, no automatic separators or labels. This keeps
        jurisdiction-specific layouts out of the service code: the format
        owns every line, the service only maps HTML to ESC/POS.

        Segment types:
          {"type": "text", "lines": [...], "align", "bold", "large"}
          {"type": "columns", "cells": [...], "bold"}   one table row
          {"type": "separator"}                         <div class="separator">
          {"type": "blank"}                             <div class="spacer">

        Alignment/emphasis come from the element's own CSS classes
        (center/right/bold/large), like data-print blocks. Divs left empty by
        Jinja conditionals are dropped. Returns None when no element opts
        in, so every existing receipt keeps its exact behavior.
        """
        root = None
        for element in soup.find_all(attrs={"data-print-mode": True}):
            if (element.get("data-print-mode") or "").strip().lower() == "literal":
                root = element
                break
        if root is None:
            return None

        # <br> becomes a real newline so inline markup (<b>, <span>) does
        # not split a line the way get_text("\n") would.
        for br in root.find_all("br"):
            br.replace_with("\n")

        segments: List[Dict[str, Any]] = []
        self._walk_literal(root, segments)
        return segments

    def _walk_literal(self, element: Tag, segments: List[Dict[str, Any]]) -> None:
        """Walk literal-mode content in document order, appending segments."""
        for child in element.children:
            if not isinstance(child, Tag):
                continue
            name = child.name.lower()
            if name in self.LITERAL_SKIP_TAGS:
                continue

            if name == "table":
                self._literal_table(child, segments)
                continue

            classes = child.get("class") or []
            if "separator" in classes:
                segments.append({"type": "separator"})
                continue
            if "spacer" in classes:
                segments.append({"type": "blank"})
                continue

            # Containers recurse; leaves contribute their text lines.
            if child.find(["div", "p", "table"]) is not None:
                self._walk_literal(child, segments)
                continue

            lines = [
                line.strip()
                for line in child.get_text().split("\n")
                if line.strip()
            ]
            if not lines:
                continue

            segments.append(
                {
                    "type": "text",
                    "lines": lines,
                    "align": self._literal_align(classes),
                    "bold": "bold" in classes,
                    "large": "large" in classes,
                }
            )

    def _literal_table(self, table: Tag, segments: List[Dict[str, Any]]) -> None:
        """One segment per table row: single-cell rows print as text,
        multi-cell rows as an aligned column line."""
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if not cells:
                continue

            row_classes = (row.get("class") or []) + (cells[0].get("class") or [])
            texts = [cell.get_text(" ", strip=True) for cell in cells]

            if len(cells) == 1:
                if not texts[0]:
                    continue
                segments.append(
                    {
                        "type": "text",
                        "lines": [texts[0]],
                        "align": self._literal_align(row_classes),
                        "bold": "bold" in row_classes,
                        "large": "large" in row_classes,
                    }
                )
            else:
                if not any(texts):
                    continue
                segments.append(
                    {
                        "type": "columns",
                        "cells": texts,
                        "bold": "bold" in row_classes,
                    }
                )

    @staticmethod
    def _literal_align(classes: List[str]) -> str:
        if "right" in classes:
            return "right"
        if "center" in classes:
            return "center"
        return "left"

    def _parse_number(self, text: str) -> float:
        """Parse a number from text, handling different decimal formats."""
        if not text:
            return 0.0

        text = text.strip()

        for symbol in self.CURRENCY_SYMBOLS:
            text = text.replace(symbol, "")

        text = text.strip()

        if "," in text and "." in text:
            if text.rfind(",") > text.rfind("."):
                text = text.replace(".", "").replace(",", ".")
            else:
                text = text.replace(",", "")
        elif "," in text:
            parts = text.split(",")
            if len(parts) == 2 and len(parts[1]) <= 2:
                text = text.replace(",", ".")
            else:
                text = text.replace(",", "")

        try:
            return float(text)
        except ValueError:
            logger.warning(f"Could not parse number: {text}")
            return 0.0
