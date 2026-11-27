"""Tests de la fase 1: inventario de PDFs sintéticos."""

import json
from pathlib import Path

import pymupdf
import pytest
from PIL import Image
from reportlab.lib.pagesizes import A3, landscape
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from sanitizer.inventory import classify_page, format_markdown, inspect_pdf, main

PAGE = landscape(A3)


def _build_base(path: Path) -> None:
    """Tres páginas: texto vectorial, imagen a página completa y solo trazos."""
    c = canvas.Canvas(str(path), pagesize=PAGE)
    c.setAuthor("Jane Engineer")
    c.setTitle("ACME P&ID Unit 100")
    c.setSubject("PRJ-4711")

    # Pág 1: texto vectorial con tags
    c.setFont("Helvetica", 12)
    c.drawString(100, 700, "P-101A CENTRIFUGAL PUMP")
    c.drawString(100, 670, "FIC-1203 HH ALARM")
    c.showPage()

    # Pág 2: "escaneado" (imagen que cubre toda la página, sin texto)
    img = Image.new("L", (400, 280), color=255)
    c.drawImage(ImageReader(img), 0, 0, width=PAGE[0], height=PAGE[1])
    c.showPage()

    # Pág 3: solo trazos (como texto convertido a curvas)
    for i in range(10):
        c.line(100 + i * 20, 100, 110 + i * 20, 140)
    c.showPage()
    c.save()


@pytest.fixture
def rich_pdf(tmp_path: Path) -> Path:
    """PDF sintético con todo lo que el inventario debe detectar."""
    base = tmp_path / "base.pdf"
    _build_base(base)

    doc = pymupdf.open(base)
    doc.set_xml_metadata(
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
        'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/">'
        "<xmp:CreatorTool>SecretCAD 2024</xmp:CreatorTool>"
        "</rdf:Description></rdf:RDF></x:xmpmeta>"
    )
    annot = doc[0].add_text_annot((50, 50), "Revisar con ACME")
    annot.set_info(title="J. Reviewer")
    annot.update()
    doc.embfile_add("spec", b"confidential spec", filename="spec.txt", desc="Spec interna")
    ocg = doc.add_ocg("ACME-TITLEBLOCK", on=False)
    doc[0].insert_text((100, 600), "LAYERED", oc=ocg)
    doc.xref_set_key(doc.pdf_catalog(), "OpenAction", "<</S/JavaScript/JS(app.alert(1))>>")

    out = tmp_path / "rich.pdf"
    doc.save(out)
    doc.close()
    return out


@pytest.fixture
def plain_pdf(synthetic_pdf: Path) -> Path:
    return synthetic_pdf


def test_metadata_and_ids(rich_pdf):
    r = inspect_pdf(rich_pdf)
    assert r["info"]["Author"] == "Jane Engineer"
    assert r["info"]["Subject"] == "PRJ-4711"
    assert any("SecretCAD" in v for v in r["xmp"].values())
    assert r["document_ids"], "reportlab escribe /ID en el trailer"
    assert r["page_count"] == 3


def test_embedded_annotations_layers_js(rich_pdf):
    r = inspect_pdf(rich_pdf)
    assert [f["filename"] for f in r["embedded_files"]] == ["spec.txt"]
    annots = r["pages"][0]["annotations"]
    assert annots and annots[0]["type"] == "Text"
    assert annots[0]["author"] == "J. Reviewer"
    assert "ACME" in annots[0]["content"]
    assert [l["name"] for l in r["layers"]] == ["ACME-TITLEBLOCK"]
    assert r["layers"][0]["on"] is False
    assert r["javascript"]["present"] and r["javascript"]["open_action"]


def test_page_classification(rich_pdf):
    kinds = [p["kind"] for p in inspect_pdf(rich_pdf)["pages"]]
    assert kinds == ["text", "image_only", "vector_only"]
    assert inspect_pdf(rich_pdf)["summary"]["needs_ocr"] is True


def test_fonts_listed_with_pages(rich_pdf):
    fonts = inspect_pdf(rich_pdf)["fonts"]
    helv = [f for f in fonts if f["name"] == "Helvetica"]
    assert helv and 1 in helv[0]["pages"]
    assert helv[0]["embedded"] is False  # fuente base-14 de reportlab


def test_plain_pdf_has_nothing_extra(plain_pdf):
    r = inspect_pdf(plain_pdf)
    s = r["summary"]
    assert s["embedded_files"] == 0
    assert s["annotations"] == 0
    assert s["layers"] == 0
    assert s["javascript"] is False
    assert s["needs_ocr"] is False
    assert s["page_kinds"] == {"text": 1}


def test_encrypted_pdf_reports_error(plain_pdf, tmp_path):
    enc = tmp_path / "enc.pdf"
    with pymupdf.open(plain_pdf) as doc:
        doc.save(enc, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="x", owner_pw="y")
    r = inspect_pdf(enc)
    assert r["encrypted"] is True
    assert "error" in r
    assert "contraseña" in format_markdown(r)


@pytest.mark.parametrize(
    ("words", "coverage", "drawings", "glyphs", "expected"),
    [
        (200, 0.0, 800, 0, "text"),
        (10, 0.0, 5, 0, "text"),
        (10, 0.0, 500, 49, "text"),
        (10, 0.0, 500, 50, "text_and_stroke_text"),
        (0, 0.0, 500, 200, "stroke_text_only"),
        (10, 0.9, 0, 0, "image_with_text"),
        (0, 0.9, 0, 0, "image_only"),
        (0, 0.9, 500, 200, "image_only"),
        (0, 0.1, 30, 0, "vector_only"),
        (0, 0.0, 0, 0, "empty"),
    ],
)
def test_classify_page(words, coverage, drawings, glyphs, expected):
    assert classify_page(words, coverage, drawings, glyphs) == expected


def test_markdown_report_is_readable(rich_pdf):
    md = format_markdown(inspect_pdf(rich_pdf))
    for fragment in (
        "# Inventario: rich.pdf",
        "Jane Engineer",
        "ACME-TITLEBLOCK",
        "spec.txt",
        "solo imagen",
        "| Helvetica |",
    ):
        assert fragment in md


def test_cli_writes_reports(rich_pdf, tmp_path, capsys):
    out = tmp_path / "data" / "reports"
    assert main([str(rich_pdf.parent), "--out", str(out), "--json"]) == 0
    assert (out / "rich.inventory.md").exists()
    data = json.loads((out / "rich.inventory.json").read_text(encoding="utf-8"))
    assert data["summary"]["javascript"] is True
    assert "AVISO" not in capsys.readouterr().err  # está bajo data/


def test_cli_warns_outside_data(plain_pdf, tmp_path, capsys):
    assert main([str(plain_pdf), "--out", str(tmp_path / "elsewhere")]) == 0
    assert "AVISO" in capsys.readouterr().err


def test_invisible_ocr_text_is_counted(tmp_path):
    """Escaneado con capa OCR invisible (modo de render 3)."""
    base = tmp_path / "scan.pdf"
    _build_base(base)
    doc = pymupdf.open(base)
    doc[1].insert_text((100, 100), "ACME PLANT OCR", render_mode=3)
    out = tmp_path / "scan_ocr.pdf"
    doc.save(out)
    doc.close()

    r = inspect_pdf(out)
    page = r["pages"][1]
    assert page["invisible_chars"] == len("ACME PLANT OCR")
    assert page["kind"] == "image_with_text"
    assert r["pages"][0]["invisible_chars"] == 0
    assert r["pages"][0]["visible_chars"] > 0
    assert r["summary"]["invisible_text_pages"] == 1


def test_outline_titles_listed(tmp_path, synthetic_pdf):
    doc = pymupdf.open(synthetic_pdf)
    doc.set_toc([[1, "ACME confidential set", 1], [2, "Rev 05 internal", 1]])
    out = tmp_path / "toc.pdf"
    doc.save(out)
    doc.close()

    r = inspect_pdf(out)
    assert [o["title"] for o in r["outline"]] == ["ACME confidential set", "Rev 05 internal"]
    assert "ACME confidential set" in format_markdown(r)


def test_repeated_image_flagged_as_logo_candidate(tmp_path):
    path = tmp_path / "logo.pdf"
    logo = ImageReader(Image.new("RGB", (40, 20), color=(200, 0, 0)))
    c = canvas.Canvas(str(path), pagesize=PAGE)
    for _ in range(4):
        c.drawImage(logo, 1000, 50, width=80, height=40)
        c.drawString(100, 700, "P-101")
        c.showPage()
    c.save()

    r = inspect_pdf(path)
    assert len(r["images"]) == 1
    assert r["images"][0]["pages"] == [1, 2, 3, 4]
    assert len(r["images"][0]["xrefs"]) == 1
    assert r["images"][0]["repeated"] is True
    assert r["summary"]["repeated_images"] == 1


def test_same_logo_with_different_xrefs_grouped(tmp_path):
    """Cada página guarda su propia copia del logo: mismo hash, distinto xref."""
    path = tmp_path / "logos.pdf"
    doc = pymupdf.open()
    png = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 30, 15), False)
    png.set_rect(png.irect, (0, 90, 200))
    data = png.tobytes("png")
    for _ in range(3):
        # Documento aparte por página: al unirlos cada uno aporta su propia copia
        single = pymupdf.open()
        page = single.new_page()
        page.insert_image(pymupdf.Rect(10, 10, 70, 40), stream=data)
        page.insert_text((100, 100), "P-101")
        doc.insert_pdf(single)
        single.close()
    doc.save(path)
    doc.close()

    r = inspect_pdf(path)
    assert len(r["images"]) == 1
    assert len(r["images"][0]["xrefs"]) == 3
    assert r["images"][0]["repeated"] is True


def test_invisible_text_over_vectors(tmp_path):
    """Como AutoCAD con SHX: trazos visibles y texto invisible encima."""
    path = tmp_path / "shx.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    for i in range(30):
        page.draw_line((50 + i * 10, 400), (55 + i * 10, 410))
    page.insert_text((50, 300), "FIC-1203 PUMP P-101A ACME PLANT", render_mode=3)
    doc.save(path)
    doc.close()

    r = inspect_pdf(path)
    assert r["pages"][0]["kind"] == "invisible_text_over_vectors"
    assert r["summary"]["invisible_text_over_vectors"] is True
    assert r["summary"]["needs_ocr"] is False  # el texto ya está en la capa de texto


@pytest.mark.parametrize(
    ("visible", "invisible", "expected"),
    [
        (5000, 40, "text"),  # unas pocas marcas invisibles ("XXX" de una valla)
        (0, 3000, "invisible_text_over_vectors"),
        (0, 10, "text"),
    ],
)
def test_classify_invisible_text(visible, invisible, expected):
    assert classify_page(100, 0.0, 800, 0, visible, invisible) == expected
