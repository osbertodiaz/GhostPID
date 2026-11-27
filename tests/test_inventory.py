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
