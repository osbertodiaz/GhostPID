"""Tests de la eliminación precisa de trazos (lineart.remove_paths)."""

import io

import pikepdf
import pymupdf

from sanitizer.lineart import remove_paths

# Área de prueba en espacio PDF (origen abajo a la izquierda)
AREA = (100.0, 100.0, 140.0, 115.0)


def _pdf(content: bytes) -> pikepdf.Pdf:
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(400, 300))
    pdf.pages[0].Contents = pdf.make_stream(content)
    return pdf


def _drawings(pdf: pikepdf.Pdf) -> list[tuple]:
    buf = io.BytesIO()
    pdf.save(buf)
    with pymupdf.open("pdf", buf.getvalue()) as doc:
        page = doc[0]
        to_pdf = ~page.transformation_matrix
        out = []
        for d in page.get_drawings():
            r = (pymupdf.Rect(d["rect"]) * to_pdf).normalize()
            out.append(tuple(round(v) for v in r))
        return sorted(out)


def test_removes_only_subpaths_inside_area():
    # Un único trazo con tres "letras" dentro del área y una fuera (estilo AutoCAD)
    content = b"""
    1 w
    101 102 m 103 110 l
    105 102 m 107 110 l
    109 102 m 111 110 l
    200 102 m 202 110 l
    S
    """
    pdf = _pdf(content)
    result = remove_paths(pdf, 0, [AREA])
    assert result.removed_subpaths == 3
    assert _drawings(pdf) == [(200, 102, 202, 110)]


def test_keeps_lines_crossing_and_shapes_around():
    content = b"""
    1 w
    50 108 m 300 108 l S
    120 108 m 130 108 l S
    120 108 m 120 120 l S
    0 0 1 RG 120 107.5 m 140 107.5 30 30 re S
    """
    pdf = _pdf(content)
    remove_paths(pdf, 0, [AREA])
    remaining = _drawings(pdf)
    assert (50, 108, 300, 108) in remaining  # tubería que cruza el área
    assert (120, 108, 120, 120) in remaining  # sale del área por arriba
    assert (120, 108, 130, 108) not in remaining  # entera dentro: se va
    assert any(r[2] - r[0] >= 30 for r in remaining if r[1] >= 107)  # recuadro mayor que el área


def test_respects_ctm_and_graphics_state():
    # Las coordenadas locales (0..5) caen dentro del área tras el cm
    content = b"""
    q 1 0 0 1 110 104 cm 0 0 m 5 5 l S Q
    0 0 m 5 5 l S
    """
    pdf = _pdf(content)
    result = remove_paths(pdf, 0, [AREA])
    assert result.removed_subpaths == 1
    assert _drawings(pdf) == [(0, 0, 5, 5)]


def test_rectangles_and_fills():
    content = b"102 103 6 6 re f 300 200 6 6 re f"
    pdf = _pdf(content)
    remove_paths(pdf, 0, [AREA])
    assert _drawings(pdf) == [(300, 200, 306, 206)]


def test_clipping_paths_are_never_removed():
    content = b"q 101 101 10 10 re W n 50 50 m 60 60 l S Q"
    pdf = _pdf(content)
    result = remove_paths(pdf, 0, [AREA])
    assert result.removed_subpaths == 0
    data = pdf.pages[0].Contents.read_bytes()
    assert b"re" in data and b"W" in data


def test_touched_mode():
    content = b"1 w 50 108 m 300 108 l S 300 200 m 310 210 l S"
    pdf = _pdf(content)
    remove_paths(pdf, 0, [AREA], touched=True)
    assert _drawings(pdf) == [(300, 200, 310, 210)]


def test_text_is_untouched():
    content = b"BT /F1 12 Tf 105 105 Td (ABC) Tj ET 101 102 m 103 110 l S"
    pdf = _pdf(content)
    pdf.pages[0].Resources = pikepdf.Dictionary(
        Font=pikepdf.Dictionary(F1=pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica))
    )
    remove_paths(pdf, 0, [AREA])
    assert b"(ABC) Tj" in pdf.pages[0].Contents.read_bytes()
    assert _drawings(pdf) == []


def test_form_xobjects_are_reported():
    pdf = _pdf(b"q /X0 Do Q")
    form = pdf.make_stream(b"101 102 m 103 110 l S", Type=pikepdf.Name.XObject, Subtype=pikepdf.Name.Form, BBox=[0, 0, 400, 300])
    pdf.pages[0].Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(X0=form))
    result = remove_paths(pdf, 0, [AREA])
    assert result.xobject_paths is True
    assert result.removed_subpaths == 0


def test_curves_judged_by_points_on_the_stroke():
    # Una "C": curva cuyos puntos de control salen del área, pero el trazo no
    content = b"1 w 115 103 m 95 101 95 117 115 113 c S"
    pdf = _pdf(content)
    result = remove_paths(pdf, 0, [AREA])
    assert result.removed_subpaths == 1
