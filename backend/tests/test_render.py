"""The Jev render layout (standard library) and, where Pillow is present, the drawing."""

import importlib.util

import pytest

from tinybar_models import render


def test_bold_markup_becomes_bold_runs_and_escapes_become_characters():
    assert render.spans("**TAX INVOICE** no \\# 5\\%") == [("TAX INVOICE", True), (" no # 5%", False)]
    assert render.spans("\\textbf{Total}: 3") == [("Total", True), (": 3", False)]


def test_a_latex_tabular_becomes_a_grid_with_bold_headers():
    rows = render.layout("\\begin{tabular}{ccc}\n\\thead{\\#} & \\thead{Qty} & Rate\\\\\n1 & 10 & 125.00\\\\\n\\end{tabular}")
    assert rows == [{"table": [[[("#", True)], [("Qty", True)], [("Rate", False)]],
                               [[("1", False)], [("10", False)], [("125.00", False)]]]}]


def test_a_markdown_pipe_table_becomes_a_grid_without_its_separator_row():
    rows = render.layout("| HSN | Qty |\n|---|---|\n| 7318 | 10 |")
    assert rows == [{"table": [[[("HSN", False)], [("Qty", False)]], [[("7318", False)], [("10", False)]]]}]


def test_headings_are_bold_and_lines_keep_their_order():
    assert render.layout("# Bill\nTotal: 3") == [{"line": [("Bill", True)]}, {"line": [("Total: 3", False)]}]


def test_text_around_a_table_is_kept_in_order():
    rows = render.layout("before\n\\begin{tabular}{c}\nx\\\\\n\\end{tabular}\nafter")
    assert [next(iter(r)) for r in rows] == ["line", "table", "line"]


@pytest.mark.parametrize("text, script", [("नमस्ते", "Indic"), ("નમસ્તે", "Indic"), ("مرحبا", "Arabic"), ("Total ₹ 3,471.56", None)])
def test_scripts_that_need_shaping_are_named(text, script):
    found = render.unshapeable(text)
    assert (found is None) if script is None else (script in found)


@pytest.mark.parametrize("text", ["\\begin{tabular}{cc}\\end{tabular}", "\\begin{tabular}{cc}\n\\hline\n\\hline\n\\end{tabular}"])
def test_an_empty_tabular_draws_nothing_and_the_text_around_it_stays(text):
    assert render.layout(text) == [{"line": []}]
    assert render.layout(f"Total\n{text}\n3") == [{"line": [("Total", False)]}, {"line": [("3", False)]}]
    assert render.unshapeable(text) is None


def test_text_too_big_to_draw_is_refused_before_layout_and_quickly():
    import time

    started = time.monotonic()
    assert "characters" in render.unshapeable("x" * 30_000 + "\n" * 3_000)
    assert "characters" in render.unshapeable("\\begin{tabular}" * 20_000)  # 15 s in layout() before the bound
    assert "pixels" in render.unshapeable("y" * 4_000 + "\nz" * 2_900)  # under the character bound: 100k by 116k pixels
    assert time.monotonic() - started < 2
    assert render.unshapeable(("| 7318 | 10 | 125.00 |\n") * 60) is None  # a long invoice table is not refused


def test_the_cmap_reader_skips_codes_mapped_to_glyph_0(tmp_path):
    import struct

    group = struct.pack(">III", 0x41, 0x43, 0)  # A..C map to glyphs 0, 1, 2: A is .notdef, so not covered
    sub = struct.pack(">HHIII", 12, 0, 16 + len(group), 0, 1) + group
    table = struct.pack(">HH", 0, 1) + struct.pack(">HHI", 3, 10, 12) + sub
    font = struct.pack(">IHHHH", 0x00010000, 1, 16, 0, 0) + b"cmap" + struct.pack(">III", 0, 28, len(table)) + table
    (tmp_path / "f.ttf").write_bytes(font)
    assert render.cmap(tmp_path / "f.ttf", 0) == {0x42, 0x43}


needs_pillow = pytest.mark.skipif(importlib.util.find_spec("PIL") is None, reason="needs Pillow (full environment)")
macos_fonts = pytest.mark.skipif(not all(path.is_file() for path, _ in render._faces()), reason="macOS fonts")


@needs_pillow
@macos_fonts
@pytest.mark.parametrize("ch", ["A", "₹", "\u202f", "✓", "中", "\ue000", "\U0001f600"])
def test_the_cmap_reader_agrees_with_what_each_font_draws(ch):
    from PIL import ImageFont

    for path, face in render._faces():
        font = ImageFont.truetype(str(path), render.SIZE, index=face)
        box = font.getmask("\U0010fffd")
        glyph = font.getmask(ch)
        is_box = glyph.size == box.size and list(glyph) == list(box)
        assert (ord(ch) in render.cmap(path, face)) == (not is_box), (path.name, face, ch)


@needs_pillow
@macos_fonts
@pytest.mark.parametrize("text, fallback_face", [("₹", 0), ("**₹**", 1)])
def test_the_rupee_sign_draws_as_its_glyph_not_an_empty_box(text, fallback_face):
    from PIL import Image, ImageDraw, ImageFont

    image = render.Renderer().draw(text)

    def drawn_with(path, face):
        expected = Image.new("RGB", image.size, "white")
        ImageDraw.Draw(expected).text((render.PAD, render.PAD + 4), "₹", fill="black",
                                      font=ImageFont.truetype(str(path), render.SIZE, index=face))
        return expected.tobytes()

    arial = render.FONT_BOLD if text.startswith("**") else render.FONT_REGULAR
    assert image.tobytes() == drawn_with(render.FONT_FALLBACK, fallback_face)
    assert image.tobytes() != drawn_with(arial, 0)  # Arial's empty box


@needs_pillow
@macos_fonts
def test_a_character_no_font_has_is_refused_and_the_rest_still_draws():
    r = render.Renderer()
    assert "U+E000" in render.unshapeable("Total \ue000 3")
    with pytest.raises(render.UnsupportedScript, match="U\\+E000"):
        r.draw("Total \ue000 3")
    for text in ["**中 ✓ Total**", "a\u202fb\tc\u200bd", "₹ 3,471.56"]:  # bold falls back to regular; spaces draw
        assert render.unshapeable(text) is None
        assert r.draw(text).size[0] > 2 * render.PAD


@needs_pillow
@macos_fonts
def test_invisible_format_characters_are_not_drawn():
    r = render.Renderer()  # Arial draws U+00AD (soft hyphen) as a hyphen and has no U+2060
    assert r.draw("ab­c⁠d").tobytes() == r.draw("abcd").tobytes()


@needs_pillow
@macos_fonts
def test_draw_refuses_on_its_own_measure_when_the_checks_before_it_pass(monkeypatch):
    r = render.Renderer()
    monkeypatch.setattr(render, "unshapeable", lambda text, shaping=False: None)
    with pytest.raises(render.UnsupportedScript, match="U\\+E000"):  # never an empty box
        r.draw("Total ")
    monkeypatch.setattr(render, "MAX_PIXELS", 1_000)
    with pytest.raises(render.UnsupportedScript, match="pixels"):  # the exact size, not only the estimate
        r.draw("Total")


@needs_pillow
@macos_fonts
def test_an_empty_tabular_draws_without_failing():
    r = render.Renderer()
    assert r.draw("\\begin{tabular}{cc}\\end{tabular}").size == (32 + 2 * render.PAD, render.LINE + 2 * render.PAD)
    assert r.draw("Total\n\\begin{tabular}{c}\\hline\\end{tabular}").size[1] == render.LINE + 2 * render.PAD


@needs_pillow
def test_a_missing_font_fails_loudly(monkeypatch, tmp_path):
    monkeypatch.setattr(render, "FONT_BOLD", tmp_path / "missing.ttf")
    with pytest.raises(RuntimeError, match="fonts missing"):
        render.Renderer()


@needs_pillow
@macos_fonts
def test_drawing_names_its_fonts_and_refuses_unshaped_scripts():
    r = render.Renderer()
    assert render.VERSION == "draftbook-render/2" and r.identity.startswith(render.VERSION)
    assert all(f"{name}#" in r.identity for name in ("Arial Unicode.ttf", "Arial Bold.ttf", "Helvetica.ttc"))
    assert r.draw("**TAX INVOICE**\n\\begin{tabular}{cc}\na & b\\\\\n\\end{tabular}").size[0] > 100
    if not r.raqm:
        with pytest.raises(render.UnsupportedScript):
            r.draw("कुल")
