"""Tests de la fase 7: verificación y auditoría."""

import json
from pathlib import Path
from textwrap import dedent

import pikepdf
import pymupdf
import pytest

from sanitizer.audit import _crossed_text, audit, build_blacklist, main
from sanitizer.mapping import MapError, load_map
from sanitizer.metadata import clean_metadata
from sanitizer.pipeline import anonymize
from sanitizer.rules import load_config

CONFIGS = Path(__file__).parent.parent / "configs"
EXAMPLE = CONFIGS / "example.yaml"


def _config(tmp_path: Path, extra: str = ""):
    path = tmp_path / "cfg.yaml"
    path.write_text(
        f"version: 1\nname: T\nextends: {EXAMPLE}\ndisable: [title_block, side_strip, cover_logo]\n"
        "ocr:\n  rotations: [0]\n" + dedent(extra),
        encoding="utf-8",
    )
    return load_config(path)


@pytest.fixture
def pdf(tmp_path) -> Path:
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)
    page.insert_text((50, 80), "PUMP P-101A by Initech Pumps, 15 barg", fontsize=11)
    page.insert_text((50, 110), "FIC-1203 HH ALARM to Globex tank", fontsize=11)
    # Cajetín (franja derecha): un nombre que las reglas no conocen
    page.insert_text((660, 520), "Drawn: Wile E. Coyote", fontsize=9)
    page.insert_text((660, 540), "Prepared by ACME Engineering", fontsize=9)
    doc.set_metadata({"author": "Jane Engineer", "title": "Globex plant"})
    path = tmp_path / "in.pdf"
    doc.save(path)
    return path


@pytest.fixture
def pipeline(pdf, tmp_path):
    """Anonimiza, guarda informe y mapa y limpia metadatos: lo que hará la fase 8."""
    cfg = _config(tmp_path)
    data = tmp_path / "data"
    anon = tmp_path / "anon.pdf"
    report = anonymize(pdf, anon, cfg, data / "m.map.json")
    final = tmp_path / "final.pdf"
    clean_metadata(anon, final, cfg.metadata)
    report_json = json.loads(report.to_json())
    return cfg, final, report_json, load_map(data / "m.map.json")


def test_clean_pipeline_passes_audit(pdf, pipeline, tmp_path):
    cfg, final, report, map_data = pipeline
    out = tmp_path / "data" / "audit"
    result = audit(final, cfg, out, pdf, report, map_data)
    assert result.ok, [f for f in result.findings if f.severity == "error"]
    assert set(result.checks) >= {"lista negra", "texto", "objetos", "metadatos", "ocr", "revisión"}
    assert result.checks["ocr"]["páginas"] == 1
    assert (out / "auditoria.md").exists() and (out / "auditoria.json").exists()
    assert (out / "paginas" / "pagina-001.png").exists()


def test_removed_summary(pipeline, tmp_path):
    cfg, final, report, map_data = pipeline
    result = audit(final, cfg, tmp_path / "data" / "a", None, report, map_data)
    rules = result.removed["reglas"]
    assert rules["vendors"]["veces"] == 1 and rules["vendors"]["páginas"] == [1]
    assert rules["operator"]["categoría"] == "operator"
    md = (tmp_path / "data" / "a" / "auditoria.md").read_text(encoding="utf-8")
    assert "## Qué se ha eliminado" in md and "| vendors | manufacturer | 1 | 1 |" in md


def test_unknown_name_listed_for_review(pipeline, tmp_path):
    cfg, final, report, map_data = pipeline
    result = audit(final, cfg, tmp_path / "data" / "a", None, report, map_data)
    texts = [e["texto"] for e in result.review_lines]
    assert any("Coyote" in t for t in texts)
    # Lo que ya es genérico o protegido no se lista
    assert not any(t.strip() in {"EPC", "OPR"} for t in texts)
    assert not any("P-101A" in t for t in texts)


def test_leftovers_are_found_everywhere(pdf, tmp_path):
    """Sin anonimizar: la auditoría debe encontrar los restos en todos los canales."""
    cfg = _config(tmp_path)
    result = audit(pdf, cfg, tmp_path / "data" / "raw")
    checks = {f.check for f in result.findings if f.severity == "error"}
    assert {"texto", "objeto", "metadatos", "ocr"} <= checks
    texts = {f.text.casefold() for f in result.findings if f.check == "texto"}
    assert {"initech pumps", "globex", "acme engineering"} <= texts
    assert not result.ok
    assert "FALLOS" in (tmp_path / "data" / "raw" / "auditoria.md").read_text(encoding="utf-8")


def test_map_originals_join_the_blacklist(pipeline):
    cfg, _, _, map_data = pipeline
    blacklist = build_blacklist(cfg, map_data)
    assert "Initech Pumps" in blacklist.literals
    assert "vendor-1" in blacklist.generics
    assert not any(lit.casefold() in {"epc", "opr"} for lit in blacklist.literals)


def test_leftover_inside_any_object_is_found(pipeline, tmp_path):
    cfg, final, report, map_data = pipeline
    # Un nombre escondido donde no hay texto visible: un nombre de fuente
    with pikepdf.open(final, allow_overwriting_input=True) as pdf:
        pdf.Root.Hidden = pikepdf.Dictionary(Note=pikepdf.String("built for Globex Petroleum"))
        pdf.save(final)
    result = audit(final, cfg, tmp_path / "data" / "obj", None, report, map_data)
    assert any(f.check == "objeto" and f.text == "Globex Petroleum" for f in result.findings)


def test_ocr_finds_names_in_images(tmp_path):
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("L", (1600, 400), 255)
    ImageDraw.Draw(img).text((50, 150), "LOGO GLOBEX PETROLEUM", fill=0, font=ImageFont.load_default(size=70))
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)
    page.insert_text((50, 80), "PUMP P-101A", fontsize=11)
    import io

    buf = io.BytesIO()
    img.save(buf, "PNG")
    page.insert_image(pymupdf.Rect(50, 300, 450, 400), stream=buf.getvalue())
    path = tmp_path / "img.pdf"
    doc.save(path)
    cfg = _config(tmp_path, "audit:\n  ocr_pages: all\n")
    result = audit(path, cfg, tmp_path / "data" / "img")
    assert any(f.check == "ocr" and "GLOBEX" in f.text.upper() for f in result.findings)


def test_protected_overlap_is_a_warning(tmp_path):
    cfg = _config(
        tmp_path,
        """
        substitutions:
          - id: pump_series
            category: other
            replacement: SERIES
            terms: [P-101A]
        """,
    )
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 80), "PUMP P-101A", fontsize=11)
    path = tmp_path / "w.pdf"
    doc.save(path)
    result = audit(path, cfg.model_copy(update={"audit": cfg.audit.__class__(ocr_pages="none")}), tmp_path / "data" / "w")
    text_findings = [f for f in result.findings if f.check == "texto"]
    assert text_findings and all(f.severity == "warning" for f in text_findings)


def test_report_only_inside_data(pdf, tmp_path):
    with pytest.raises(MapError, match="solo dentro de data/"):
        audit(pdf, _config(tmp_path), tmp_path / "reports")


def test_cli(pdf, pipeline, tmp_path, capsys):
    cfg, final, report, _ = pipeline
    report_path = tmp_path / "data" / "anon.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    cfg_path = tmp_path / "cfg.yaml"
    args = [str(final), "--config", str(cfg_path), "--out", str(tmp_path / "data" / "cli"), "--original", str(pdf),
            "--anon-report", str(report_path), "--map", str(tmp_path / "data" / "m.map.json")]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert ": OK" in out and "texto restante para revisar" in out
    assert main([str(pdf), "--config", str(cfg_path), "--out", str(tmp_path / "data" / "cli2")]) == 1


def test_review_ignores_title_block_vocabulary(tmp_path):
    from sanitizer.audit import Blacklist, _is_reviewable

    cfg = _config(tmp_path, "audit:\n  review_ignore_words: [membrane]\n")
    blacklist = Blacklist([], [], {"epc", "opr"})
    assert not _is_reviewable(cfg, blacklist, "DRAWN: CHECKED: APPROVED:")
    assert not _is_reviewable(cfg, blacklist, "SHEET 3 OF 25 REV 0")
    assert not _is_reviewable(cfg, blacklist, "PREPARED BY EPC FOR OPR")
    assert not _is_reviewable(cfg, blacklist, "MEMBRANE P-101A 15 barg")
    assert _is_reviewable(cfg, blacklist, "Drawn: Wile E. Coyote")


def test_rotated_ocr_reading_of_real_text_is_a_warning():
    # Texto real del revés: el OCR puede leer "DOC-REF-108" como "801-..."
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)
    page.insert_text((400, 300), "DOC-REF-108", fontsize=10, rotate=180)
    line = page.get_text("rawdict")["blocks"][0]["lines"][0]
    area = tuple(pymupdf.Rect(line["bbox"]))
    assert line["dir"][0] < 0
    assert _crossed_text(page, area, (1.0, 0.0)) == "DOC-REF-108"
    # Misma dirección: podría ser una fuga bajo la sustitución, sigue siendo error
    assert _crossed_text(page, area, (-1.0, 0.0)) is None
    # Sin texto real que cubra el área, tampoco
    assert _crossed_text(page, (50, 50, 120, 60), (1.0, 0.0)) is None
