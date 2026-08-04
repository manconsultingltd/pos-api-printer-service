"""
Rendered receipt HTML fixtures (post-Jinja), as POSAwesome sends them to
/api/print-html.

ENGLISH_HTML pins down the modeled-mode behavior. DATA_PRINT_HTML exercises
the literal data-print pass-through blocks. LITERAL_HTML exercises full
literal print mode, where the format owns the whole layout.
"""

# Modeled mode: the parser recognizes the receipt shape and the generator
# lays it out. Structural labels print in fixed English.
ENGLISH_HTML = """<!doctype html>
<html>
<head>
  <meta charset="UTF-8">
  <title>Receipt - INV-001</title>
</head>
<body>
<div class="center bold large">ACME Store</div>
<div class="center">
  Invoice: INV-001<br>
  2026-07-15 10:00:00<br>
  Cashier: admin<br>
  Customer: John Doe
</div>
<div class="bold">ITEMS</div>
<table>
  <tr><td colspan="2">Widget</td></tr>
  <tr>
    <td>&nbsp;2 x 10.00</td>
    <td class="text-right">20.00</td>
  </tr>
</table>
<table>
  <tr>
    <td>Subtotal:</td>
    <td class="text-right">20.00</td>
  </tr>
  <tr>
    <td>Tax:</td>
    <td class="text-right">2.00</td>
  </tr>
  <tr>
    <td>TOTAL:</td>
    <td class="text-right">22.00</td>
  </tr>
</table>
<table>
  <tr>
    <td>Payment:</td>
    <td class="text-right">Cash</td>
  </tr>
  <tr>
    <td>Paid:</td>
    <td class="text-right">25.00</td>
  </tr>
  <tr>
    <td>Change:</td>
    <td class="text-right">3.00</td>
  </tr>
</table>
<div class="center">Thank you for your business!<br>Please come again.</div>
</body>
</html>
"""

# Modeled mode with literal data-print pass-through blocks. The parser models
# the invoice; anything it doesn't model rides along in
# data-print="header|fiscal|footer" blocks that print as-is (with the
# block's own alignment/emphasis) at fixed positions. Mirrors a fiscal
# invoice's extra sections without hard-coding any jurisdiction in the
# service. Includes the whitespace Jinja leaves behind.
DATA_PRINT_HTML = """<!--API_PRINT_ESC_POS_THERMAL_80MM-->
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title>Receipt - INV-100</title>
</head>
<body>

<div class="center bold large">ACME Store</div>

<div class="center small">
  123 Market Street<br>
  Springfield
</div>

<div class="center small" data-print="header">
  Auth Code: ABC-123-XYZ<br>

    Authorized Range: 000-001 - 005-000

</div>

<div class="separator"></div>

<div class="center">
  Invoice: INV-100<br>
  2026-07-15 14:00:00<br>
  Cashier: Sam<br>
  Customer: Jane Roe
</div>

<div class="center small" data-print="fiscal">
  Reg ID: 999-8888<br>
  Exemption: EX-2026-7
</div>

<div class="separator"></div>

<div class="bold">ITEMS</div>
<table>
  <tr><td colspan="2">Widget</td></tr>
  <tr>
    <td>&nbsp;2 x 10.00</td>
    <td class="text-right">20.00</td>
  </tr>
</table>

<div class="separator"></div>

<table>
  <tr>
    <td>Subtotal:</td>
    <td class="text-right">20.00</td>
  </tr>
  <tr>
    <td>Tax 15%:</td>
    <td class="text-right">3.00</td>
  </tr>
  <tr class="total-row">
    <td>TOTAL:</td>
    <td class="text-right">23.00</td>
  </tr>
</table>

<div class="separator"></div>

<table>
  <tr>
    <td>Cash:</td>
    <td class="text-right">25.00</td>
  </tr>
  <tr>
    <td>Change:</td>
    <td class="text-right">2.00</td>
  </tr>
</table>

<div class="separator"></div>

<div class="center">Thank you for your business!</div>

<div class="center bold" data-print="footer">
  ** PAID **
</div>

<div class="right small" data-print="footer">
  Copy 1: Customer<br>
  Copy 2: Store
</div>

</body>
</html>
"""

# Literal print mode: a wrapper <div data-print-mode="literal"> hands the
# whole layout to the print format (a div, not <body>: the ERPNext print
# pipeline re-wraps or strips the format's body). The service prints exactly
# these lines, in order, with each element's own alignment/emphasis and no
# sections of its own — including the zero-amount rows a fiscal layout shows.
# Includes the leftover Jinja whitespace and empty divs conditionals leave.
LITERAL_HTML = """<!--API_PRINT_ESC_POS_THERMAL_80MM-->
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title>Receipt - R-100</title>
  <style>
    body { font-family: 'Courier New', monospace; }
    .center { text-align: center; }
  </style>
</head>
<body>
<div data-print-mode="literal">

<div class="center bold large">ACME STORE</div>
<div class="center bold">ACME RETAIL GROUP LLC</div>
<div class="center">
  123 Market Street<br>
  Suite 4, Springfield<br>
  Email: hello@acme.example<br>
  Tel: 555-0100<br>
  Reg ID: 12-3456789
</div>

  <div class="center bold">Branch: Downtown</div>
  <div class="center">10 Center Ave, Springfield</div>

<div class="bold">Receipt No: R-100</div>
<div>Issued: 31-07-2026 Time: 11:57:26</div>
<div class="bold">Customer: WALK-IN CUSTOMER</div>
<div>Reg ID / ID: </div>

<table>
  <tr><td>Qty</td><td>Price</td><td>Amount</td></tr>
  <tr class="bold"><td colspan="3">7422300500418 Trigger Spray Cleaner 30g</td></tr>
  <tr><td>1</td><td>13.91</td><td>13.91</td></tr>
</table>

<div class="spacer"></div>
<div>1.0 Items</div>
<div class="spacer"></div>

    <div class="right">Subtotal: 13.91</div>
    <div class="right">Discount: 0.00</div>
    <div class="right">Exempt: 0.00</div>
    <div class="right">Taxable 15%: 13.91</div>
    <div class="right">Sales Tax 15%: 2.09</div>
    <div class="right">Sales Tax 18%: 0.00</div>
    <div class="right">Total: 16.00</div>
<div class="spacer"></div>

<div>Paid with CASH: 16.00</div>
<div>Amount in words:</div>
<div>Sixteen dollars exactly.</div>
<div>Change: 0.00</div>
<div class="spacer"></div>

<div>Registration No: </div>
<div>Purchase Order No: </div>
<div class="spacer"></div>

<div>Authorized Range:</div>
<div>000-001 to 005-000</div>
<div>Valid Through: 19-06-2027</div>
<div class="spacer"></div>

<div>Original: Customer   Copy: Store</div>
<div class="spacer"></div>

<div class="center">THANK YOU FOR SHOPPING WITH US</div>
<div class="center">Served by: Alex Doe</div>
<div class="center">KEEP YOUR RECEIPT</div>
<div class="center bold">NO RETURNS OR EXCHANGES!</div>
<div class="center bold">Please retain this receipt.</div>

</div>
</body>
</html>
"""
