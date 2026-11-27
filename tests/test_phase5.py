"""Tests de la fase 5: zonas, logos en imagen y OCR de texto en trazos.

El texto "en trazos" se genera de verdad: se exporta una página a SVG con el
texto convertido a trayectos y se vuelve a convertir a PDF. El OCR usa el
Tesseract instalado (eng + spa).
"""

from pathlib import Path
from textwrap import dedent

import pymupdf
import pytest
from PIL import Image, ImageDraw, ImageFont

from sanitizer.logos import canonical_hash, find_image_groups, hamming
from sanitizer.ocr import OcrPolicy, needs_ocr, ocr_page
from sanitizer.pipeline import anonymize, main
from sanitizer.redact import Session, find_matches
from sanitizer.rules import ConfigError, load_config

CONFIGS = Path(__file__).parent.parent / "configs"
EXAMPLE = CONFIGS / "example.yaml"
A3 = (1190.55, 841.89)


def _config(tmp_path: Path, extra: str = "", ocr_rotations: str = "[0]"):
    path = tmp_path / "cfg.yaml"
    path.write_text(
        f"version: 1\nname: T\nextends: {EXAMPLE}\ndisable: [title_block, side_strip, cover_logo]\n"
        f"ocr:\n  rotations: {ocr_rotations}\n" + dedent(extra),
        encoding="utf-8",
    )
    return load_config(path)


def _outlined(build) -> pymupdf.Document:
    """Página cuyo texto se convierte en trayectos (como un PDF de AutoCAD/PostScript)."""
    doc = pymupdf.open()
    page = doc.new_page(width=A3[0], height=A3[1])
    build(page)
    svg = page.get_svg_image(text_as_path=True)
    return pymupdf.open("pdf", pymupdf.open(stream=svg.encode(), filetype="svg").convert_to_pdf())


def _technical_lines(page, y0=160):
    for i in range(6):
        page.insert_text((100, y0 + i * 30), f"LINE {i} FIC-120{i} DISCHARGE TO TK-400{i}", fontsize=14)


def _ocr_text(path: Path, rotations=(0,)) -> str:
    with pymupdf.open(path) as doc:
        return "\n".join(
            line.text for page in doc for line in ocr_page(page, OcrPolicy(rotations=list(rotations)))
        )


def _save(doc: pymupdf.Document, path: Path) -> Path:
    doc.save(path)
    return path


# ---------------------------------------------------------------------------
# Zonas
# ---------------------------------------------------------------------------


@pytest.fixture
def zoned_pdf(tmp_path) -> Path:
    doc = pymupdf.open()
    for rotation in (0, 90):
        page = doc.new_page(width=A3[0], height=A3[1])
        page.insert_text((100, 100), "PUMP P-101A", fontsize=12)
        page.set_rotation(rotation)
        # Cajetín abajo a la derecha (tal como se ve, con la página ya rotada)
        w, h = page.rect.width, page.rect.height
        view_rect = pymupdf.Rect(w - 250, h - 150, w - 10, h - 10)
        rect = (view_rect * page.derotation_matrix).normalize()
        point = pymupdf.Point(view_rect.x0 + 20, view_rect.y0 + 40) * page.derotation_matrix
        page.insert_text(point, "ACME Engineering P-203", fontsize=10, rotate=page.rotation)
        page.draw_rect(rect + (20, 60, -20, -20))  # "logo" vectorial dentro de la zona
        # Línea del marco que cruza el cajetín: entra en la zona pero no está dentro
        page.draw_line((rect.x0 - 300, rect.y0 + 5), (rect.x1, rect.y0 + 5))
    return _save(doc, tmp_path / "zoned.pdf")


def test_zones_remove_everything_inside(zoned_pdf, tmp_path):
    cfg = _config(
        tmp_path,
        """
        zones:
          - id: tb
            rect: [0, 0, 90, 55]
            unit: mm
            anchor: bottom-right
            label: "[CAJETIN]"
        """,
    )
    cfg_zone = cfg.zones[-1]
    assert cfg_zone.id == "tb"
    out = tmp_path / "out.pdf"
    report = anonymize(zoned_pdf, out, cfg)
    with pymupdf.open(out) as doc:
        for page in doc:
            text = page.get_text()
            assert "ACME" not in text and "P-203" not in text
            assert "P-101A" in text  # fuera de la zona
            assert "[CAJETIN]" in text
            strokes = [d for d in page.get_drawings() if d["type"] == "s"]
            sizes = [max(d["rect"].width, d["rect"].height) for d in strokes]
            assert len(strokes) == 1 and sizes[0] > 400  # se va el "logo", queda el marco
    assert len(report.zones) == 2
    # Lo que había en la zona se elimina a propósito: no es una pérdida de datos
    assert report.warnings == []
    assert report.problems == []


def test_zone_label_is_horizontal_as_seen(zoned_pdf, tmp_path):
    cfg = _config(
        tmp_path,
        """
        zones:
          - {id: tb, rect: [0, 0, 90, 55], unit: mm, anchor: bottom-right, label: "[CAJETIN]"}
        """,
    )
    out = tmp_path / "out.pdf"
    anonymize(zoned_pdf, out, cfg)
    with pymupdf.open(out) as doc:
        for page in doc:
            label = next(
                line
                for block in page.get_text("dict")["blocks"]
                for line in block.get("lines", [])
                if "CAJETIN" in "".join(s["text"] for s in line["spans"])
            )
            dx, dy = label["dir"]
            seen = pymupdf.Point(dx, dy) * page.rotation_matrix - pymupdf.Point(0, 0) * page.rotation_matrix
            assert round(seen.x) == 1 and round(seen.y) == 0, page.number


# ---------------------------------------------------------------------------
# Logos en imagen
# ---------------------------------------------------------------------------


def _logo(size=(120, 60), gray=False) -> Image.Image:
    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)
    draw.ellipse((5, 5, 55, 55), fill=(200, 30, 30))
    draw.rectangle((65, 10, 115, 50), fill=(20, 40, 160))
    draw.line((0, 58, 119, 30), fill=(0, 0, 0), width=3)
    return img.convert("L") if gray else img


def _png(img: Image.Image) -> bytes:
    import io

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_perceptual_hash_ignores_rotation_and_colour():
    base = canonical_hash(_logo())
    assert hamming(base, canonical_hash(_logo().rotate(90, expand=True))) == 0
    assert hamming(base, canonical_hash(_logo(gray=True))) <= 6
    other = Image.new("RGB", (120, 60), "white")
    ImageDraw.Draw(other).text((10, 20), "TECHNICAL PHOTO", fill="black")
    assert hamming(base, canonical_hash(other)) > 6


@pytest.fixture
def logo_pdf(tmp_path) -> Path:
    doc = pymupdf.open()
    variants = [_logo(), _logo(), _logo(gray=True), _logo().rotate(90, expand=True)]
    photo = Image.new("RGB", (200, 150), (90, 140, 60))
    ImageDraw.Draw(photo).rectangle((20, 20, 120, 90), fill=(250, 250, 0))
    scan = Image.new("L", (400, 280), 230)
    for i, variant in enumerate(variants):
        page = doc.new_page(width=A3[0], height=A3[1])
        # Cada página guarda su propia copia (como en un set combinado)
        page.insert_image(pymupdf.Rect(1000, 30, 1150, 105), stream=_png(variant))
        page.insert_text((100, 100), f"PUMP P-10{i}A", fontsize=12)
        if i < 3:  # imagen grande repetida: nunca es un logo
            page.insert_image(pymupdf.Rect(0, 200, A3[0], A3[1]), stream=_png(scan), overlay=False)
    doc[0].insert_image(pymupdf.Rect(100, 300, 300, 450), stream=_png(photo))
    return _save(doc, tmp_path / "logos.pdf")


def test_repeated_logo_variants_are_one_group_and_removed(logo_pdf, tmp_path):
    cfg = _config(tmp_path, "ocr:\n  enabled: false\n")
    with pymupdf.open(logo_pdf) as doc:
        groups = find_image_groups(doc, cfg.logos)
    logo = next(g for g in groups if g["size"] in ((120, 60), (60, 120)))
    assert logo["pages"] == [1, 2, 3, 4] and logo["removed"]
    big = next(g for g in groups if g["size"] == (400, 280))
    assert not big["removed"]  # ocupa casi toda la página
    photo = next(g for g in groups if g["size"] == (200, 150))
    assert not photo["removed"]  # una sola página

    out = tmp_path / "out.pdf"
    anonymize(logo_pdf, out, cfg)
    with pymupdf.open(out) as doc:
        for page in doc:
            clip = pymupdf.Rect(1000, 30, 1150, 105)
            pix = page.get_pixmap(clip=clip, dpi=36)
            assert set(pix.samples) <= {255}, page.number  # el logo ya no se ve
            assert f"P-10{page.number}A" in page.get_text()


def test_logo_keep_and_remove_lists(logo_pdf, tmp_path):
    with pymupdf.open(logo_pdf) as doc:
        cfg = _config(tmp_path, "ocr:\n  enabled: false\n")
        groups = find_image_groups(doc, cfg.logos)
        logo_hash = next(g["hash"] for g in groups if g["removed"])
        photo_hash = next(g["hash"] for g in groups if g["size"] == (200, 150))
        cfg = _config(
            tmp_path,
            f"ocr:\n  enabled: false\nlogos:\n  keep: ['{logo_hash}']\n  remove: ['{photo_hash}']\n",
        )
        groups = {g["hash"]: g for g in find_image_groups(doc, cfg.logos)}
    assert groups[logo_hash]["removed"] is False and groups[logo_hash]["reason"] == "en logos.keep"
    assert groups[photo_hash]["removed"] is True


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------


def test_fuzzy_terms_tolerate_ocr_confusions(tmp_path):
    cfg = _config(tmp_path)
    text = "SUPPLIED BY GL0BEX PETR0LEUM, P-101A"
    plain, _ = find_matches(cfg, text, relaxed=True)
    fuzzy, _ = find_matches(cfg, text, relaxed=True, fuzzy=True)
    assert plain == []
    assert [text[m.start : m.end] for m in fuzzy] == ["GL0BEX PETR0LEUM"]


@pytest.fixture
def stroke_pdf(tmp_path) -> Path:
    def build(page):
        page.insert_text((100, 100), "PUMP P-101A SUPPLIED BY GLOBEX PETROLEUM", fontsize=16)
        _technical_lines(page)
        page.draw_line((80, 103), (1100, 103))  # tubería que pasa bajo la etiqueta

    return _save(_outlined(build), tmp_path / "strokes.pdf")


def test_stroke_page_needs_ocr(stroke_pdf, tmp_path):
    cfg = _config(tmp_path)
    with Session(stroke_pdf, cfg) as session, session.view() as view:
        assert needs_ocr(view[0], session) == "texto en trazos"


def test_ocr_redacts_stroke_text(stroke_pdf, tmp_path):
    cfg = _config(tmp_path)
    out = tmp_path / "out.pdf"
    report = anonymize(stroke_pdf, out, cfg)
    assert report.ocr[1]["coincidencias"] == 1
    assert [r.original for r in report.replacements if r.source == "ocr"] == ["GLOBEX PETROLEUM"]
    after = _ocr_text(out)
    assert "GLOBEX" not in after and "PETROLEUM" not in after
    for i in range(6):
        assert f"FIC-120{i}" in after  # datos técnicos intactos
    assert "P-101A" in after
    with pymupdf.open(out) as doc:
        assert "OPR" in doc[0].get_text()  # la sustitución es texto real
        long_lines = [d for d in doc[0].get_drawings() if d["rect"].width > 900]
        assert len(long_lines) == 1  # la tubería que cruza se conserva


def test_ocr_vertical_stroke_text(tmp_path):
    def build(page):
        _technical_lines(page)
        page.insert_text((60, 700), "DRAWN BY JOHN DOE", fontsize=14, rotate=90)

    pdf = _save(_outlined(build), tmp_path / "vertical.pdf")
    cfg = _config(tmp_path, ocr_rotations="[0, 90, 270]")
    out = tmp_path / "out.pdf"
    report = anonymize(pdf, out, cfg)
    assert [r.original for r in report.replacements if r.source == "ocr"] == ["JOHN DOE"]
    assert "JOHN DOE" not in _ocr_text(out, rotations=(0, 90, 270))


@pytest.fixture
def scanned_pdf(tmp_path) -> Path:
    img = Image.new("L", (3300, 2330), 255)
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=60)
    draw.text((200, 200), "CLIENT: GLOBEX PETROLEUM", fill=0, font=font)
    draw.text((200, 400), "PUMP P-101A  FIC-1203  15 barg", fill=0, font=font)
    draw.text((200, 600), "DESIGNED BY ACME ENGINEERING", fill=0, font=font)
    doc = pymupdf.open()
    page = doc.new_page(width=A3[0], height=A3[1])
    page.insert_image(page.rect, stream=_png(img))
    return _save(doc, tmp_path / "scan.pdf")


def test_ocr_redacts_scanned_page(scanned_pdf, tmp_path):
    cfg = _config(tmp_path)
    out = tmp_path / "out.pdf"
    report = anonymize(scanned_pdf, out, cfg)
    assert report.ocr[1]["motivo"] == "escaneada"
    found = sorted(r.original for r in report.replacements if r.source == "ocr")
    assert found == ["ACME ENGINEERING", "GLOBEX PETROLEUM"]
    after = _ocr_text(out)
    assert "GLOBEX" not in after and "ACME" not in after
    assert "P-101A" in after and "FIC-1203" in after


def test_ocr_disabled(stroke_pdf, tmp_path):
    cfg = _config(tmp_path, "ocr:\n  enabled: false\n")
    report = anonymize(stroke_pdf, tmp_path / "out.pdf", cfg)
    assert report.ocr == {}


def test_ocr_policy_validation(tmp_path):
    with pytest.raises(ConfigError, match="rotations"):
        _config(tmp_path, ocr_rotations="[45]")


# ---------------------------------------------------------------------------
# Pipeline y CLI
# ---------------------------------------------------------------------------


def test_cli(stroke_pdf, tmp_path, capsys):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"version: 1\nname: T\nextends: {EXAMPLE}\nocr:\n  rotations: [0]\n", encoding="utf-8")
    out = tmp_path / "cli.pdf"
    report = tmp_path / "data" / "r.json"
    assert main([str(stroke_pdf), "-o", str(out), "--config", str(cfg), "--report", str(report)]) == 0
    printed = capsys.readouterr().out
    assert "OCR: 1 páginas, 1 coincidencias" in printed
    assert '"source": "ocr"' in report.read_text(encoding="utf-8")


def test_undecodable_or_mask_images_do_not_crash(tmp_path):
    import pikepdf

    # Máscara de 1 bit (/ImageMask, sin espacio de color), como en un set real
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(400, 300))
    mask = pdf.make_stream(
        bytes([0b10101010, 0b01010101] * 16),
        Type=pikepdf.Name.XObject,
        Subtype=pikepdf.Name.Image,
        Width=16,
        Height=16,
        ImageMask=True,
        BitsPerComponent=1,
    )
    page = pdf.pages[0]
    page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=mask))
    page.Contents = pdf.make_stream(b"q 50 0 0 50 10 10 cm /Im0 Do Q")
    path = tmp_path / "mask.pdf"
    pdf.save(path)
    with pymupdf.open(path) as d:
        find_image_groups(d, _config(tmp_path).logos)  # no debe lanzar excepción


def test_partial_multiword_term_reported_in_ocr(tmp_path):
    def build(page):
        _technical_lines(page)
        page.insert_text((800, 80), "GLOBEX", fontsize=20)  # logo apilado: solo la primera palabra
        page.insert_text((800, 105), "OIL CO", fontsize=20)

    pdf = _save(_outlined(build), tmp_path / "stacked.pdf")
    cfg = _config(
        tmp_path,
        """
        substitutions:
          - id: operator
            terms: [Globex Petroleum]
        """,
    )
    report = anonymize(pdf, tmp_path / "out.pdf", cfg)
    assert any("GLOBEX" in n.text and n.source == "ocr" for n in report.near_misses)
