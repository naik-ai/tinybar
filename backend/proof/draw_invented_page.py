"""Draw one INVENTED purchase-invoice page (PNG) for the MLX proof.

Copied from Draftbook's scripts/model-proof/draw_invented_page.py (17 Nov 2026). Nothing here comes from a real
document: every name says INVENTED or TEST and every GSTIN is a visibly invented ZZZZZ number. Also writes the
page's exact text, so a reading can be compared with what was drawn.

Usage (from the repository root): uv run --directory backend python proof/draw_invented_page.py OUT_DIR
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

HEADER = [
    "INVENTED SUPPLIER ONE (TEST)",
    "12 Test Road, Invented Nagar, Pune 411001",
    "GSTIN: 27ZZZZZ0002Z1ZZ",
    "",
    "TAX INVOICE",
    "Invoice No: INV-TEST-0042        Date: 14/11/2026",
    "Bill to: INVENTED BUYER (TEST)",
    "Buyer GSTIN: 27ZZZZZ0001Z1ZZ",
]
COLUMNS = ["#", "Description", "HSN", "Qty", "Rate", "Amount"]
ROWS = [
    ["1", "Test widget, steel", "7318", "10", "125.00", "1,250.00"],
    ["2", "Invented bracket", "7326", "4", "310.50", "1,242.00"],
    ["3", "Sample gasket (TEST)", "4016", "25", "18.00", "450.00"],
]
FOOTER = [
    "Taxable value: 2,942.00",
    "CGST 9%: 264.78",
    "SGST 9%: 264.78",
    "Total: 3,471.56",
    "Amount in words: Three thousand four hundred seventy-one rupees and fifty-six paise",
]
COL_X = [60, 110, 520, 640, 730, 860]


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


PAGES = {"invented-invoice": (1100, 1400), "invented-invoice-wide": (1800, 1000)}
TOTAL_AT = (60, 770)  # where "Total: 3,471.56" is drawn on both pages, in pixels (left, top)


def draw(out_dir: Path, name: str = "invented-invoice") -> Path:
    """Same content at the same pixel positions; the wide page tells page fractions from a padded canvas."""
    out_dir.mkdir(parents=True, exist_ok=True)
    page = Image.new("RGB", PAGES[name], "white")
    pen = ImageDraw.Draw(page)
    body, bold = _font(26), _font(34)
    y = 60
    for line in HEADER:
        pen.text((60, y), line, fill="black", font=bold if line in ("TAX INVOICE", HEADER[0]) else body)
        y += 44
    y += 20
    pen.line((50, y - 8, 1050, y - 8), fill="black", width=2)
    for x, title in zip(COL_X, COLUMNS):
        pen.text((x, y), title, fill="black", font=body)
    y += 44
    pen.line((50, y - 8, 1050, y - 8), fill="black", width=1)
    for row in ROWS:
        for x, cell in zip(COL_X, row):
            pen.text((x, y), cell, fill="black", font=body)
        y += 44
    pen.line((50, y - 4, 1050, y - 4), fill="black", width=2)
    y += 30
    for line in FOOTER:
        pen.text((60, y), line, fill="black", font=body)
        y += 44
    path = out_dir / f"{name}.png"
    page.save(path)
    lines = HEADER + ["", " | ".join(COLUMNS)] + [" | ".join(r) for r in ROWS] + [""] + FOOTER
    (out_dir / f"{name}.txt").write_text("\n".join(lines) + "\n")
    return path


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    for page_name in PAGES:
        print(draw(Path(sys.argv[1]), page_name))
