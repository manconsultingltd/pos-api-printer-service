"""Generate the printer.ico used by the Windows installer and GUI.

Draws a simple printer silhouette at 256x256 and saves a multi-resolution
.ico file with sizes 16/24/32/48/64/128/256 — enough for every context
Windows may render it in (tray, shortcuts, taskbar, Alt+Tab, etc).

Developer tool: run by hand when the icon design changes and commit the
resulting printer.ico (next to this script). CI copies the committed file
into the bundle — it can't regenerate here because the posawesome-builder
image blocks `pip install`, so Pillow isn't available during the build.

    python3 -m pip install Pillow   # one-time setup
    python3 make_printer_icon.py printer.ico
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw


BODY_DARK = "#37474f"
BODY_LIGHT = "#455a64"
PAPER = "#ffffff"
PAPER_OUTLINE = "#90a4ae"
PAPER_LINES = "#cfd8dc"
LED = "#66bb6a"
POWER = "#263238"


def render(size: int = 256) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Coordinates scale from a 256-canvas base — PIL downscales later.
    # Top paper slot.
    d.rounded_rectangle((56, 36, 200, 68), radius=4, fill=BODY_DARK)
    # Main body.
    d.rounded_rectangle((40, 72, 216, 176), radius=10, fill=BODY_LIGHT)
    # Output paper.
    d.rectangle((56, 180, 200, 216), fill=PAPER,
                outline=PAPER_OUTLINE, width=2)
    for y in (190, 198, 206):
        d.line((66, y, 190, y), fill=PAPER_LINES, width=2)
    # Status LED (green).
    d.ellipse((180, 108, 196, 124), fill=LED)
    # Power button.
    d.ellipse((54, 108, 70, 124), fill=POWER)
    return img


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    dest = Path(argv[1])
    dest.parent.mkdir(parents=True, exist_ok=True)
    img = render(256)
    img.save(
        dest, format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
               (64, 64), (128, 128), (256, 256)],
    )
    print(f"Wrote {dest} ({dest.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
