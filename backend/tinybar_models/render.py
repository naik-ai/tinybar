"""The render image Jev compares a crop with: a block's text drawn as it would print.

Markup is drawn, not printed: **bold** and \\textbf{…}/\\thead{…} in bold weight, Markdown '#' headings in
bold, LaTeX tabular and Markdown pipe tables as a ruled grid, and LaTeX escapes (\\#, \\%, \\&, \\$, \\_)
as their characters. The text given to Jev is never changed; only this image is. There is no branch on block
class or document type: the same markup always draws the same way. Inline maths ($…$) is drawn as written.

Fonts are fixed files (Arial Unicode for regular, Arial Bold for bold, Helvetica for what neither has, such
as the rupee sign ₹ and U+202F); a missing font is an error, never Pillow's default font. Each character is
drawn in the first font of its weight's list whose cmap maps it; a bold character no bold font has is drawn
in the regular weight. Whitespace draws as a space and invisible format characters (Cf) are not drawn.
unshapeable() names, and draw() refuses with UnsupportedScript instead of a wrong image:
- a character no font maps (it would print as an empty box);
- without raqm, a script that needs shaping (Devanagari, Gujarati and the other Indic scripts, Arabic,
  Thai): Pillow here has no raqm;
- text over MAX_CHARS characters, or a render estimated over MAX_PIXELS.

layout() and unshapeable() are standard library only (tested without Pillow; without the font files,
unshapeable() skips the glyph check, as the Renderer cannot start there); draw() needs Pillow.
"""

import functools
import hashlib
import re
import struct
import unicodedata
from pathlib import Path

VERSION = "draftbook-render/2"  # /2: per-character font fallback (₹), missing-glyph and size refusals; scores do not carry over
FONT_REGULAR = Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf")
FONT_BOLD = Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf")
FONT_FALLBACK = Path("/System/Library/Fonts/Helvetica.ttc")  # face 0 Regular, face 1 Bold: both have ₹
SIZE, PAD, LINE, CELL_PAD = 26, 20, 40, 12
MAX_CHARS = 10_000  # layout() on 10,000 characters of unterminated tabulars takes about 0.02 s
MAX_PIXELS = 25_000_000  # 75 MB of RGB; a full-page table block is under 10 million
COMPLEX_SCRIPTS = [(0x0590, 0x08FF, "Hebrew/Arabic"), (0x0900, 0x0DFF, "Indic (Devanagari, Gujarati, …)"),
                   (0x0E00, 0x0EFF, "Thai/Lao"), (0x1000, 0x109F, "Myanmar"), (0x1780, 0x17FF, "Khmer")]
ESCAPES = {r"\#": "#", r"\%": "%", r"\&": "&", r"\$": "$", r"\_": "_", r"\{": "{", r"\}": "}"}
TABULAR = re.compile(r"\\begin\{tabular\}(?:\{[^}]*\})?(.*?)\\end\{tabular\}", re.S)
BOLD_CMD = re.compile(r"\\(?:textbf|thead)\{([^{}]*)\}")
MULTI = re.compile(r"\\multi(?:column|row)\{[^}]*\}\{[^}]*\}\{([^{}]*)\}")


class UnsupportedScript(ValueError):
    pass


def _faces() -> list[tuple[Path, int]]:
    """(file, face index) per weight, in fallback order; the regular list first."""
    return [(FONT_REGULAR, 0), (FONT_FALLBACK, 0), (FONT_BOLD, 0), (FONT_FALLBACK, 1)]


REGULAR_ORDER, BOLD_ORDER = (0, 1), (2, 3, 0, 1)  # indexes into _faces()


@functools.cache
def cmap(path: Path, face: int) -> frozenset[int]:
    """The code points the font face maps to a real glyph (not glyph 0), read from its Unicode cmap."""
    data = path.read_bytes()
    off = struct.unpack(">I", data[12 + 4 * face:16 + 4 * face])[0] if data[:4] == b"ttcf" else 0
    tables = {data[off + 12 + 16 * i:off + 16 + 16 * i]: struct.unpack(">I", data[off + 20 + 16 * i:off + 24 + 16 * i])[0]
              for i in range(struct.unpack(">H", data[off + 4:off + 6])[0])}
    base = tables[b"cmap"]
    found = {}
    for i in range(struct.unpack(">H", data[base + 2:base + 4])[0]):
        pid, eid, sub = struct.unpack(">HHI", data[base + 4 + 8 * i:base + 12 + 8 * i])
        fmt = struct.unpack(">H", data[base + sub:base + sub + 2])[0]
        if (pid == 0 or (pid == 3 and eid in (1, 10))) and fmt in (4, 12):
            found.setdefault(fmt, base + sub)
    codes: set[int] = set()
    if 12 in found:  # segmented coverage: the full Unicode range
        t = found[12]
        for g in range(struct.unpack(">I", data[t + 12:t + 16])[0]):
            start, end, glyph = struct.unpack(">III", data[t + 16 + 12 * g:t + 28 + 12 * g])
            codes.update(c for c in range(start, end + 1) if glyph + c - start)
    elif 4 in found:  # segment mapping to delta values: the Basic Multilingual Plane
        t = found[4]
        n = struct.unpack(">H", data[t + 6:t + 8])[0] // 2
        ends = struct.unpack(f">{n}H", data[t + 14:t + 14 + 2 * n])
        starts, deltas, ranges = (struct.unpack(f">{n}H", data[t + 16 + 2 * n * k:t + 16 + 2 * n * (k + 1)]) for k in (1, 2, 3))
        for i in range(n):
            for c in range(starts[i], min(ends[i], 0xFFFE) + 1):
                if ranges[i] == 0:
                    glyph = (c + deltas[i]) & 0xFFFF
                else:
                    at = t + 16 + 6 * n + 2 * i + ranges[i] + 2 * (c - starts[i])
                    glyph = struct.unpack(">H", data[at:at + 2])[0]
                    glyph = (glyph + deltas[i]) & 0xFFFF if glyph else 0
                if glyph:
                    codes.add(c)
    return frozenset(codes)


def _drawn(ch: str) -> str:
    """What the image draws for a character: whitespace as a space, invisible format characters as nothing."""
    if ch.isspace():
        return " "
    return "" if unicodedata.category(ch) == "Cf" else ch


def _estimate(rows: list[dict]) -> int:
    """Pixels of the render, assuming no glyph is wider than SIZE (an em); draw() measures exactly."""
    width, height = 0, 0
    for row in rows:
        if "line" in row:
            width, height = max(width, SIZE * sum(len(t) for t, _ in row["line"])), height + LINE
        else:
            cols = max(len(r) for r in row["table"])
            chars = [max((sum(len(t) for t, _ in r[c]) if c < len(r) else 0) for r in row["table"]) for c in range(cols)]
            width, height = max(width, SIZE * sum(chars) + 2 * CELL_PAD * cols), height + LINE * len(row["table"])
    return (max(width, 32) + 2 * PAD) * (height + 2 * PAD)


def unshapeable(text: str, shaping: bool = False) -> str | None:
    """Why this renderer cannot draw the text faithfully, or None. shaping: the renderer has raqm."""
    if len(text) > MAX_CHARS:
        return f"text over {MAX_CHARS:,} characters"
    if not shaping:
        for ch in text:
            code = ord(ch)
            for lo, hi, name in COMPLEX_SCRIPTS:
                if lo <= code <= hi:
                    return name
    if all(path.is_file() for path, _ in _faces()):
        covered = set().union(*(cmap(path, face) for path, face in _faces()))
        missing = sorted({ch for ch in text if _drawn(ch).strip() and ord(ch) not in covered})
        if missing:
            return "characters no renderer font has: " + " ".join(f"U+{ord(ch):04X}" for ch in missing[:5])
    if _estimate(layout(text)) > MAX_PIXELS:
        return f"a render over {MAX_PIXELS:,} pixels"
    return None


def spans(text: str) -> list[tuple[str, bool]]:
    """(text, bold) runs: **…**, \\textbf{…} and \\thead{…} are bold; escapes become characters."""
    text = MULTI.sub(r"\1", text)
    text = BOLD_CMD.sub(lambda m: f"**{m.group(1)}**", text)
    for k, v in ESCAPES.items():
        text = text.replace(k, v)
    out, bold = [], False
    for i, piece in enumerate(text.split("**")):
        if piece:
            out.append((piece, bold))
        if i < text.count("**"):
            bold = not bold
    return out


def _table_rows(body: str) -> list[list[list[tuple[str, bool]]]]:
    rows = []
    for raw in re.split(r"\\\\", body.replace(r"\hline", "")):
        raw = raw.strip()
        if raw:
            rows.append([spans(cell.strip()) for cell in re.split(r"(?<!\\)&", raw)])
    return rows


def layout(text: str) -> list[dict]:
    """Rows to draw: {"line": spans} or {"table": rows of cells of spans}."""
    out: list[dict] = []
    pos = 0
    for m in TABULAR.finditer(text):
        out += _lines(text[pos:m.start()])
        table = _table_rows(m.group(1))
        if table:  # a tabular with no rows (or only \hline) prints nothing, so draws nothing
            out.append({"table": table})
        pos = m.end()
    out += _lines(text[pos:])
    return out or [{"line": []}]


def _lines(text: str) -> list[dict]:
    out, pipe = [], []

    def flush():
        if pipe:
            out.append({"table": [[spans(c.strip()) for c in r.strip().strip("|").split("|")] for r in pipe]})
            pipe.clear()

    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|") and len(stripped) > 1:
            if not re.fullmatch(r"\|[\s:|-]+\|", stripped):  # the |---|---| separator row is not drawn
                pipe.append(stripped)
            continue
        flush()
        heading = re.match(r"#{1,6}\s+(.*)", stripped)
        if heading:
            out.append({"line": [(t, True) for t, _ in spans(heading.group(1))]})
        elif stripped or out:
            out.append({"line": spans(line)})
    flush()
    while out and out[-1] == {"line": []}:
        out.pop()
    return out


class Renderer:
    def __init__(self):
        from PIL import ImageFont, features

        files = list(dict.fromkeys(path for path, _ in _faces()))
        missing = [str(p) for p in files if not p.is_file()]
        if missing:
            raise RuntimeError(f"renderer fonts missing: {', '.join(missing)}")
        self.raqm = bool(features.check("raqm"))
        self.fonts = [ImageFont.truetype(str(path), SIZE, index=face) for path, face in _faces()]
        self.cmaps = [cmap(path, face) for path, face in _faces()]
        if [f.getname()[1] for f in self.fonts[1::2]] != ["Regular", "Bold"]:
            raise RuntimeError(f"renderer fallback faces in {FONT_FALLBACK} are not Regular and Bold")
        ids = [f"{p.name}#{hashlib.sha256(p.read_bytes()).hexdigest()[:12]}" for p in files]
        self.identity = f"{VERSION} fonts {' + '.join(ids)}{' raqm' if self.raqm else ' basic-layout'}"

    def _pieces(self, run: list[tuple[str, bool]]):
        """(text, font) pieces: each character in the first font of its weight's order that maps it."""
        out: list[tuple[str, object]] = []
        for t, b in run:
            for ch in "".join(_drawn(ch) for ch in t):
                font = next((self.fonts[i] for i in (BOLD_ORDER if b else REGULAR_ORDER) if ord(ch) in self.cmaps[i]),
                            None)
                if font is None:  # unshapeable() refused it before; never draw an empty box
                    raise UnsupportedScript(f"no renderer font has U+{ord(ch):04X}")
                if out and out[-1][1] is font:
                    out[-1] = (out[-1][0] + ch, font)
                else:
                    out.append((ch, font))
        return out

    def _width(self, run: list[tuple[str, bool]]) -> int:
        return int(sum(font.getlength(t) for t, font in self._pieces(run)))

    def _draw_run(self, pen, x: int, y: int, run):
        for t, font in self._pieces(run):
            pen.text((x, y), t, fill="black", font=font)
            x += int(font.getlength(t))

    def draw(self, text: str):
        from PIL import Image, ImageDraw

        reason = unshapeable(text, self.raqm)
        if reason:
            raise UnsupportedScript(f"the renderer cannot draw this text: {reason}")
        rows = layout(text)
        sizes = []
        for row in rows:
            if "line" in row:
                sizes.append((self._width(row["line"]), LINE))
            else:
                cols = max(len(r) for r in row["table"])
                widths = [max((self._width(r[c]) if c < len(r) else 0) for r in row["table"]) + 2 * CELL_PAD for c in range(cols)]
                row["widths"] = widths
                sizes.append((sum(widths), LINE * len(row["table"])))
        width = max([w for w, _ in sizes] + [32]) + 2 * PAD
        height = sum(h for _, h in sizes) + 2 * PAD
        if width * height > MAX_PIXELS:
            raise UnsupportedScript(f"the renderer cannot draw this text: a render over {MAX_PIXELS:,} pixels")
        image = Image.new("RGB", (width, height), "white")
        pen = ImageDraw.Draw(image)
        y = PAD
        for row, (_, h) in zip(rows, sizes):
            if "line" in row:
                self._draw_run(pen, PAD, y + 4, row["line"])
            else:
                widths, x0 = row["widths"], PAD
                for i, r in enumerate(row["table"]):
                    x = x0
                    for c, w in enumerate(widths):
                        if c < len(r):
                            self._draw_run(pen, x + CELL_PAD, y + i * LINE + 6, r[c])
                        x += w
                xs = [x0 + sum(widths[:c]) for c in range(len(widths) + 1)]
                for x in xs:
                    pen.line((x, y, x, y + h), fill="black", width=1)
                for i in range(len(row["table"]) + 1):
                    pen.line((x0, y + i * LINE, xs[-1], y + i * LINE), fill="black", width=1)
            y += h
        return image
