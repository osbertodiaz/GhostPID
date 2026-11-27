"""Tests del detector de texto en trazos (simulación de fuentes SHX y contornos).

Los casos negativos reproducen falsos positivos vistos en planos reales:
logos dibujados varias veces, filas de símbolos, sombreados y parcelas.
"""

from pathlib import Path

import pymupdf
import pytest
from reportlab.lib.pagesizes import A1, A3, A4, landscape
from reportlab.pdfgen import canvas

from sanitizer.inventory import inspect_pdf
from sanitizer.stroke_text import find_stroke_text

PAGE = landscape(A3)

# "Letras" distintas como polilíneas (coordenadas relativas a una caja 1×1)
GLYPHS = [
    [(0, 0), (0.5, 1), (1, 0), (0.2, 0.4), (0.8, 0.4)],  # A
    [(0, 0), (0, 1), (0.7, 1), (0.7, 0.5), (0, 0.5)],  # P
    [(0.6, 0), (0, 0), (0, 1), (0.6, 1), (0.6, 0.9), (0.1, 0.9), (0.1, 0.1), (0.6, 0.1)],  # C
    [(0, 1), (0.6, 1), (0.6, 0.9), (0.35, 0.9), (0.35, 0), (0.25, 0), (0.25, 0.9), (0, 0.9)],  # T
    [(0, 0), (0, 1), (0.6, 0), (0.6, 1)],  # N
    [(0.7, 1), (0, 1), (0, 0.5), (0.7, 0.5), (0.7, 0), (0, 0)],  # 5
]


def _glyph(c, x, y, size, variant, vertical=False, filled=False):
    """Dibuja una letra como path independiente, de trazo (SHX) o relleno."""
    w, h = size * 0.6, size
    pts = GLYPHS[variant % len(GLYPHS)]
    p = c.beginPath()
    for i, (u, v) in enumerate(pts):
        # En vertical la letra se gira 90°: la anchura va en y, la altura en x
        px, py = (x + v * h, y + u * w) if vertical else (x + u * w, y + v * h)
        (p.moveTo if i == 0 else p.lineTo)(px, py)
    if filled:
        p.close()
    c.drawPath(p, stroke=0 if filled else 1, fill=1 if filled else 0)


def _label(c, x, y, n, size=9.0, vertical=False, filled=False, start=0):
    advance = size * 0.8
    for i in range(n):
        if vertical:
            _glyph(c, x, y + i * advance, size, start + i, vertical=True, filled=filled)
        else:
            _glyph(c, x + i * advance, y, size, start + i, filled=filled)


def _runs_for(tmp_path: Path, draw, pagesize=PAGE):
    path = tmp_path / "strokes.pdf"
    c = canvas.Canvas(str(path), pagesize=pagesize)
    c.setLineWidth(0.5)
    draw(c)
    c.showPage()
    c.save()
    with pymupdf.open(path) as doc:
        page = doc[0]
        return find_stroke_text(page.get_cdrawings(), page.rect)


# El tamaño de letra de referencia (9 pt) está pensado para A1; en A3 se escala
A3_TEXT = 9.0 * 0.5


def test_horizontal_label_detected(tmp_path):
    runs = _runs_for(tmp_path, lambda c: _label(c, 100, 500, 6, A3_TEXT))
    assert [(r.glyphs, r.vertical) for r in runs] == [(6, False)]


def test_vertical_label_detected(tmp_path):
    runs = _runs_for(tmp_path, lambda c: _label(c, 300, 200, 8, A3_TEXT, vertical=True))
    assert [(r.glyphs, r.vertical) for r in runs] == [(8, True)]


def test_filled_outline_text_detected(tmp_path):
    """Texto TrueType convertido a contornos rellenos."""
    runs = _runs_for(tmp_path, lambda c: _label(c, 100, 500, 5, A3_TEXT, filled=True))
    assert [r.glyphs for r in runs] == [5]


# Letra de 2 pt: en A4 (umbral ~1,1 pt) es texto; en A1 (umbral 3 pt), no
SMALL_TEXT = 2.0


def test_threshold_scales_with_page_size(tmp_path):
    """Un A1 impreso en A4: letras de 2 pt siguen siendo texto en A4…"""
    runs = _runs_for(tmp_path, lambda c: _label(c, 100, 300, 6, SMALL_TEXT), pagesize=A4)
    assert [r.glyphs for r in runs] == [6]


def test_tiny_marks_on_a1_are_not_text(tmp_path):
    """…pero ese mismo tamaño en un A1 es demasiado pequeño para ser texto."""
    runs = _runs_for(
        tmp_path, lambda c: _label(c, 100, 300, 6, SMALL_TEXT), pagesize=landscape(A1)
    )
    assert runs == []


def test_separate_labels_are_separate_runs(tmp_path):
    def draw(c):
        _label(c, 100, 500, 4, A3_TEXT)
        _label(c, 300, 500, 4, A3_TEXT)  # misma línea base, lejos
        _label(c, 100, 400, 4, A3_TEXT)  # otra línea

    assert sorted(r.glyphs for r in _runs_for(tmp_path, draw)) == [4, 4, 4]


def test_two_glyphs_are_not_a_run(tmp_path):
    assert _runs_for(tmp_path, lambda c: _label(c, 100, 500, 2, A3_TEXT)) == []


def test_stacked_duplicates_count_once(tmp_path):
    """Logo dibujado 10 veces en el mismo sitio: no son 10 letras."""

    def draw(c):
        for _ in range(10):
            _label(c, 100, 500, 4, A3_TEXT)

    assert [r.glyphs for r in _runs_for(tmp_path, draw)] == [4]


def _row_of_same_symbol(c):
    for i in range(12):
        _glyph(c, 100 + i * 4, 300, A3_TEXT, variant=0)


def _touching_boxes(c):
    # polígonos contiguos (parcelas): cada uno empieza donde acaba el anterior
    for i in range(12):
        x = 100 + i * 3.5
        p = c.beginPath()
        p.moveTo(x, 300)
        p.lineTo(x + 3.5, 300)
        p.lineTo(x + 3.5, 300 + 4 + (i % 3))
        p.lineTo(x, 304)
        p.close()
        c.drawPath(p)


@pytest.mark.parametrize(
    "draw",
    [
        # línea discontinua horizontal: segmentos sueltos de altura 0
        lambda c: [c.line(100 + i * 8, 300, 105 + i * 8, 300) for i in range(40)],
        # línea discontinua vertical: segmentos apilados, anchura 0
        lambda c: [c.line(200, 100 + i * 8, 200, 105 + i * 8) for i in range(40)],
        # fila de globos de instrumento: demasiado grandes para ser letras
        lambda c: [c.circle(100 + i * 40, 600, 9) for i in range(8)],
        # sombreado fino: diagonales cortas
        lambda c: [c.line(400 + i * 1.5, 400, 401 + i * 1.5, 401) for i in range(60)],
        # fila de símbolos idénticos con tamaño de letra
        _row_of_same_symbol,
        # casillas rectangulares alineadas
        lambda c: [c.rect(100 + i * 4, 300, 3, 4) for i in range(12)],
        _touching_boxes,
    ],
    ids=["dashed-h", "dashed-v", "bubbles", "hatch", "same-symbol", "boxes", "parcels"],
)
def test_non_text_geometry_ignored(tmp_path, draw):
    assert _runs_for(tmp_path, draw) == []


def test_inventory_flags_stroke_text_pages(tmp_path):
    """Página 1: P&ID con tags en trazos + cajetín en texto real.
    Página 2: solo tags en trazos. Página 3: texto real con un logo pequeño."""
    path = tmp_path / "pid.pdf"
    c = canvas.Canvas(str(path), pagesize=PAGE)
    c.setLineWidth(0.3)
    for row in range(12):  # 12 etiquetas de 6 glifos = 72 glifos
        _label(c, 100, 100 + row * 30, 6, A3_TEXT, start=row)
    c.drawString(1000, 50, "TITLE BLOCK ACME")
    c.showPage()
    c.setLineWidth(0.3)
    for row in range(12):
        _label(c, 100, 100 + row * 30, 6, A3_TEXT, start=row)
    c.showPage()
    _label(c, 1000, 700, 5, A3_TEXT)  # logo con letras en curvas
    c.drawString(100, 700, "GENERAL NOTES")
    c.showPage()
    c.save()

    report = inspect_pdf(path)
    kinds = [p["kind"] for p in report["pages"]]
    assert kinds == ["text_and_stroke_text", "stroke_text_only", "text"]
    assert report["pages"][0]["stroke_text_glyphs"] == 72
    assert report["summary"]["needs_ocr"] is True
