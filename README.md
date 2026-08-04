# API Printer Service

A small, cross-platform HTTP service that turns ERPNext / POSAwesome receipt
HTML into raw **ESC/POS** thermal-printer commands. It prints receipts in
about 0.5–1 second — 4–10× faster than the browser print dialog — and falls
back to browser printing when it isn't reachable.

The service listens on `127.0.0.1:5058` (localhost only) and runs on Linux,
Windows and macOS.

## How it works

```
POSAwesome (browser)
    │  fetches the print-format HTML from ERPNext
    ▼
PrinterApi.js
    │  POST the raw HTML to /api/print-html
    ▼
API Printer Service (localhost:5058)
    │  BeautifulSoup parses the HTML
    ▼
ESC/POS generator
    │  builds the thermal-printer byte stream
    ▼
CUPS (Linux/macOS) / Windows spooler → thermal printer
```

If the service is down or returns an error, POSAwesome falls back to the
normal browser print dialog.

## Two print modes

The service reads what the print format already renders — there is **no
language detection and no country/marker configuration**. Pick the mode that
fits the ticket:

> **Rule of thumb:** if your receipt fits the standard shape (company, items,
> subtotal, taxes, total, payments), use modeled mode. **If it doesn't — a
> different structure, a fixed fiscal layout, or a fully localized ticket —
> make it a literal format** and let the print format own every line. Don't
> try to bend the modeled path to fit; that's exactly what literal mode is
> for.

### 1. Modeled mode (default)

The service recognizes a standard receipt shape (company, items, subtotal,
taxes, total, payments) and lays it out on the ticket. Values are located
with text anchors that tolerate several languages (e.g. it finds a total row
whether it's labelled `Total:` or `Totaal:`), but the printed **structural
labels are a fixed English set** (`Invoice`, `Cashier`, `TOTAL`, …).

Anything the service doesn't model can ride along in literal
**`data-print`** blocks — mark a `<div data-print="header|fiscal|footer">`
and its content prints as-is, at a fixed position, with the div's own
alignment/emphasis (`center` / `right` / `bold` / `large`). Use this for
extra sections such as fiscal codes, tax IDs, amount-in-words, copies or
legal phrases.

- Example: [`api-printer-service/example_print_format.html`](api-printer-service/example_print_format.html)

### 2. Literal mode

The print format owns the **entire** layout. Put
`data-print-mode="literal"` on the `<div>` that wraps the whole receipt and
the service prints exactly the lines the format renders — no sections,
separators or labels of its own. This is the right choice when the ticket
must be fully localized or must follow a fixed fiscal layout: that logic
stays in the print format, not in the service.

Layout rules:

| Markup | Prints as |
|--------|-----------|
| any leaf element | one line per `<br>` |
| `<table>` row, multiple cells | one line: first cell left, last cell right, middle cells centered |
| `<table>` row, single cell | a plain line |
| `<div class="separator">` | a dashed rule |
| `<div class="spacer">` | a blank line |
| CSS class `center` / `right` | alignment (default left) |
| CSS class `bold` / `large` | emphasized / double size |

Empty elements (e.g. a `<div>` a Jinja `{% if %}` left blank) are dropped,
so guarded fields never leave a blank line.

- Example: [`api-printer-service/print_format_literal_80mm.html`](api-printer-service/print_format_literal_80mm.html)

> **Opt in on a wrapping `<div>`, not `<body>`.** The ERPNext/POSAwesome
> print pipeline re-wraps or strips the format's own `<body>` when
> rendering, which would silently drop the attribute.

## Configuring ERPNext

Create a Print Format (Doc Type: *Sales Invoice*, Custom Format: *Yes*) whose
HTML begins with the API-print marker comment, then paste one of the example
templates:

```html
<!--API_PRINT_ESC_POS_THERMAL_58MM-->
```

Use `80MM` instead of `58MM` for 80mm paper. POSAwesome looks for this exact
comment to decide whether to route the receipt through the service. Then set
this Print Format on the relevant POS Profile.

## How HTML parsing works (modeled mode)

| Data | How it's found |
|------|----------------|
| Company | first element with `class="bold large"` (or `<h1>`) |
| Address | first `class="small"` element (excluding `data-print` blocks) |
| Invoice # | text after `Invoice:` / `Receipt No:` / `Bon:` / `Factura:` … |
| Items | table rows with a `qty x rate` pattern |
| Taxes | rows containing `Tax` / `VAT` / `BTW` with an amount |
| Total | row starting with `Total` / `Totaal` |
| Currency | detected from the `€ $ £ ¥ ₽` symbols (`€` isn't sent to the printer) |

## API

```bash
# Health check
curl http://localhost:5058/health

# List printers
curl http://localhost:5058/api/printers

# Print from receipt HTML
curl -X POST http://localhost:5058/api/print-html \
  -H "Content-Type: application/json" \
  -d '{"printer":"ThermalPrinter","html":"<html>...</html>","paper_width":58}'

# Test print
curl -X POST http://localhost:5058/api/test-print \
  -H "Content-Type: application/json" \
  -d '{"printer":"ThermalPrinter","paper_width":58}'

# Open cash drawer
curl -X POST http://localhost:5058/api/cash-drawer \
  -H "Content-Type: application/json" \
  -d '{"printer":"ThermalPrinter"}'
```

A control panel (set/inspect the default printer, view the log, test print)
is served at `http://localhost:5058/`.

## Install

Prebuilt, self-contained installers are produced by CI for Linux (`.run`) and
Windows (`.exe`) — no Python needed on the target machine. Download them from
the [**Releases**](../../releases) page; each release attaches both installers.
The build is defined in
[`.github/workflows/release.yml`](.github/workflows/release.yml).

The service picks the target printer in this order: the printer saved via the
control panel → the OS default printer (Windows) → the built-in default name
`ThermalPrinter`.

## Run from source

```bash
cd api-printer-service
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python main.py            # serves on http://127.0.0.1:5058
```

`pywin32`, `pystray` and `Pillow` install on Windows only (they're guarded in
`requirements.txt`).

## Tests

```bash
cd api-printer-service
pip install pytest
pytest tests/
```

The suite covers modeled-mode extraction, the `data-print` pass-through
blocks, full literal mode, the ESC/POS byte output, the sync/threadpool
endpoint contract, and Windows printer enumeration.

## Repository layout

| Path | Purpose |
|------|---------|
| `api-printer-service/main.py` | FastAPI app and endpoints |
| `api-printer-service/html_parser.py` | HTML → structured data / literal segments |
| `api-printer-service/escpos_generator.py` | structured data → ESC/POS bytes |
| `api-printer-service/printer_manager.py` | CUPS / Windows spooler abstraction |
| `api-printer-service/config.py` | cross-platform paths, ports, printer resolution |
| `api-printer-service/example_print_format.html` | modeled-mode print format |
| `api-printer-service/print_format_literal_80mm.html` | literal-mode print format |
| `api-printer-service/installers/` | Linux / Windows / macOS installer assets |
| `api-printer-service/web/` | control-panel UI |
| `api-printer-service/tests/` | pytest suite |

## License

Released under the [MIT License](LICENSE) © 2026 MAN Consulting Ltd.
