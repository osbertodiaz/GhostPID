"""Tests del detector de texto en trazos (simulación de fuentes SHX de CAD)."""

from pathlib import Path

import pymupdf
import pytest
from reportlab.lib.pagesizes import A3, landscape
from reportlab.pdfgen import canvas

from sanitizer.inventory import inspect_pdf
from sanitizer.stroke_text import find_stroke_text

PAGE = landscape(A3)
GLYPH_W, GLYPH_H, ADVANCE = 5.0, 9.0, 7.0  # como un SHX de ~3 mm


def _glyph(c: canvas.Canvas, x: float, y: float, vertical: bool = False) -> None:
    """Dibuja una "letra" como path de trazo independiente (zigzag tipo SHX)."""
    w, h = (GLYPH_H, GLYPH_W) if vertical else (GLYPH_W, GLYPH_H)
    p = c.beginPath()
    p.moveTo(x, y)
    p.lineTo(x + w * 0.5, y + h)
    p.lineTo(x + w, y)
    p.lineTo(x + w * 0.2, y + h * 0.4)
    c.drawPath(p, stroke=1, fill=0)


def _label(c: canvas.Canvas, x: float, y: float, n: int, vertical: bool = False) -> None:
    for i in range(n):
        if vertical:
            _glyph(c, x, y + i * ADVANCE, vertical=True)
        else:
            _glyph(c, x + i * ADVANCE, y)


def _page_drawings(path: Path, page: int = 0) -> list:
    with pymupdf.open(path) as doc:
        return doc[page].get_cdrawings()


def _make(tmp_path: Path, draw) -> Path:
    path = tmp_path / "strokes.pdf"
    c = canvas.Canvas(str(path), pagesize=PAGE)
    c.setLineWidth(0.5)
    draw(c)
    c.showPage()
    c.save()
    return path


def test_horizontal_label_detected(tmp_path):
    path = _make(tmp_path, lambda c: _label(c, 100, 500, 6))
    runs = find_stroke_text(_page_drawings(path))
    assert len(runs) == 1
    assert runs[0].glyphs == 6 and not runs[0].vertical


def test_vertical_label_detected(tmp_path):
    path = _make(tmp_path, lambda c: _label(c, 300, 200, 8, vertical=True))
    runs = find_stroke_text(_page_drawings(path))
    assert [(r.glyphs, r.vertical) for r in runs] == [(8, True)]


def test_separate_labels_are_separate_runs(tmp_path):
    def draw(c):
        _label(c, 100, 500, 4)
        _label(c, 300, 500, 4)  # misma línea base, lejos
        _label(c, 100, 400, 4)  # otra línea base

    runs = find_stroke_text(_page_drawings(_make(tmp_path, draw)))
    assert sorted(r.glyphs for r in runs) == [4, 4, 4]


def test_two_glyphs_are_not_a_run(tmp_path):
    path = _make(tmp_path, lambda c: _label(c, 100, 500, 2))
    assert find_stroke_text(_page_drawings(path)) == []


@pytest.mark.parametrize(
    "draw",
    [
        # línea discontinua horizontal: segmentos sueltos de altura 0
        lambda c: [c.line(100 + i * 8, 300, 105 + i * 8, 300) for i in range(40)],
        # línea discontinua vertical: segmentos apilados, anchura 0
        lambda c: [c.line(200, 100 + i * 8, 200, 105 + i * 8) for i in range(40)],
        # fila de globos de instrumento (36 pt): símbolos, no letras
        lambda c: [c.circle(100 + i * 40, 600, 18) for i in range(8)],
        # sombreado fino: diagonales cortas de 2 pt
        lambda c: [c.line(400 + i * 3, 400, 402 + i * 3, 402) for i in range(60)],
    ],
    ids=["dashed-h", "dashed-v", "bubbles", "hatch"],
)
def test_non_text_geometry_ignored(tmp_path, draw):
    assert find_stroke_text(_page_drawings(_make(tmp_path, draw))) == []


def test_inventory_flags_stroke_text_pages(tmp_path):
    """Página 1: P&ID con tags en trazos + cajetín en texto real.
    Página 2: solo tags en trazos. Página 3: texto real con un logo pequeño."""
    path = tmp_path / "pid.pdf"
    c = canvas.Canvas(str(path), pagesize=PAGE)
    c.setLineWidth(0.5)
    for row in range(12):  # 12 etiquetas de 6 glifos = 72 glifos
        _label(c, 100, 100 + row * 30, 6)
    c.drawString(1000, 50, "TITLE BLOCK ACME")
    c.showPage()
    for row in range(12):
        _label(c, 100, 100 + row * 30, 6)
    c.showPage()
    _label(c, 1000, 700, 5)  # logo con letras en curvas
    c.drawString(100, 700, "GENERAL NOTES")
    c.showPage()
    c.save()

    report = inspect_pdf(path)
    kinds = [p["kind"] for p in report["pages"]]
    assert kinds == ["text_and_stroke_text", "stroke_text_only", "text"]
    assert report["pages"][0]["stroke_text_glyphs"] == 72
    assert report["summary"]["needs_ocr"] is True
