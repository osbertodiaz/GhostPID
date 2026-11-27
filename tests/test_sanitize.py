"""Tests de la fase 8a: CLI `sanitize` y autocorrección."""

import json
from pathlib import Path
from textwrap import dedent

import pymupdf
import pytest

from sanitizer.cli import collect_inputs, main
from sanitizer.mapping import load_map
from sanitizer.metadata import verify_metadata
from sanitizer.ocr import OcrPolicy, ocr_page

CONFIGS = Path(__file__).parent.parent / "configs"
EXAMPLE = CONFIGS / "example.yaml"


def _config(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / "cfg.yaml"
    path.write_text(
        f"version: 1\nname: T\nextends: {EXAMPLE}\ndisable: [title_block, side_strip, cover_logo]\n"
        "ocr:\n  rotations: [0]\n" + dedent(extra),
        encoding="utf-8",
    )
    return path


def _text_pdf(path: Path, lines: list[str]) -> Path:
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)
    for i, line in enumerate(lines):
        page.insert_text((50, 80 + 25 * i), line, fontsize=11)
    doc.set_metadata({"author": "Jane Engineer", "title": "Globex plant"})
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    return path


def _stroke_pdf(path: Path) -> Path:
    """Texto convertido a trayectos (sin capa de texto), como un plano AutoCAD."""
    doc = pymupdf.open()
    page = doc.new_page(width=1190.55, height=841.89)
    page.insert_text((100, 100), "PUMP P-101A SUPPLIED BY GLOBEX PETROLEUM", fontsize=16)
    for i in range(6):
        page.insert_text((100, 160 + i * 30), f"LINE {i} FIC-120{i} DISCHARGE TO TK-400{i}", fontsize=14)
    svg = page.get_svg_image(text_as_path=True)
    outlined = pymupdf.open("pdf", pymupdf.open(stream=svg.encode(), filetype="svg").convert_to_pdf())
    path.parent.mkdir(parents=True, exist_ok=True)
    outlined.save(path)
    return path


def _args(tmp_path: Path, cfg: Path, *inputs: Path, extra: tuple = ()) -> list[str]:
    data = tmp_path / "data"
    return [
        *map(str, inputs),
        "--config",
        str(cfg),
        "--out",
        str(data / "out"),
        "--reports",
        str(data / "reports"),
        "--maps",
        str(data / "maps"),
        *extra,
    ]


def test_end_to_end(tmp_path, capsys):
    pdf = _text_pdf(tmp_path / "in" / "plano.pdf", ["PUMP P-101A by Initech Pumps, 15 barg", "Owner: Globex Petroleum"])
    assert main(_args(tmp_path, _config(tmp_path), pdf)) == 0
    data = tmp_path / "data"
    out = data / "out" / "plano.sanitized.pdf"
    report_dir = data / "reports" / "plano"
    assert out.exists() and verify_metadata(out) == []
    with pymupdf.open(out) as doc:
        text = doc[0].get_text()
    assert "Initech" not in text and "Globex" not in text and "P-101A" in text
    for name in ("anonimizado.json", "metadatos.json", "auditoria.md", "auditoria.json"):
        assert (report_dir / name).exists(), name
    assert not (report_dir / "intermedio").exists()  # los intermedios se borran
    data_map = load_map(data / "maps" / "plano.map.json")
    assert data_map["documents"][-1]["output"] == "plano.sanitized.pdf"
    printed = capsys.readouterr().out
    assert "| plano.pdf | OK |" in printed


def test_repairs_leftovers_found_by_audit_ocr(tmp_path, capsys):
    # El anonimizado no pasa OCR; la auditoría sí y encuentra el nombre en trazos
    cfg = _config(tmp_path, "ocr:\n  enabled: false\n  rotations: [0]\n")
    pdf = _stroke_pdf(tmp_path / "in" / "trazos.pdf")
    assert main(_args(tmp_path, cfg, pdf)) == 0
    data = tmp_path / "data"
    audit_json = json.loads((data / "reports" / "trazos" / "auditoria.json").read_text(encoding="utf-8"))
    assert audit_json["ok"] is True
    assert audit_json["checks"]["autocorrección"]["páginas"] == [1]
    out = data / "out" / "trazos.sanitized.pdf"
    with pymupdf.open(out) as doc:
        ocr = " ".join(line.text for line in ocr_page(doc[0], OcrPolicy(rotations=[0])))
        assert "GLOBEX" not in ocr and "FIC-1203" in ocr
        assert "OPR" in doc[0].get_text()  # misma denominación que en las reglas
    assert verify_metadata(out) == []
    assert "| trazos.pdf | OK | 0 | 1 |" in capsys.readouterr().out


def test_without_repair_the_document_fails(tmp_path):
    cfg = _config(tmp_path, "ocr:\n  enabled: false\n  rotations: [0]\n")
    pdf = _stroke_pdf(tmp_path / "in" / "trazos.pdf")
    assert main(_args(tmp_path, cfg, pdf, extra=("--repair", "0"))) == 1


def test_text_layer_leftovers_are_reported_not_repaired(tmp_path):
    # "ACME" dentro de "ACMEX": la regla exige palabra completa, la auditoría no
    pdf = _text_pdf(tmp_path / "in" / "x.pdf", ["Valve ACMEX supplied by Initech Pumps"])
    assert main(_args(tmp_path, _config(tmp_path), pdf)) == 1
    audit_json = json.loads((tmp_path / "data" / "reports" / "x" / "auditoria.json").read_text(encoding="utf-8"))
    checks = {f["check"] for f in audit_json["findings"] if f["severity"] == "error"}
    # El mismo resto se ve en el texto, en el stream de contenido y por OCR (es visible)
    assert "texto" in checks and checks <= {"texto", "objeto", "ocr"}
    assert audit_json["checks"]["autocorrección"]["páginas"] == "-"


def test_batch_with_shared_map(tmp_path, capsys):
    folder = tmp_path / "in"
    _text_pdf(folder / "a.pdf", ["Pump by Initech Pumps"])
    _text_pdf(folder / "sub" / "b.pdf", ["Valve by Umbrella Valves", "Pump by Initech Pumps"])
    shared = tmp_path / "data" / "maps" / "proyecto.map.json"
    assert main(_args(tmp_path, _config(tmp_path), folder, extra=("--map", str(shared)))) == 0
    data_map = load_map(shared)
    assert sorted(d["source"] for d in data_map["documents"]) == ["a.pdf", "b.pdf"]
    assert list(data_map["entries"]["VENDOR-1"]["originals"]) == ["Initech Pumps"]
    assert list(data_map["entries"]["VENDOR-2"]["originals"]) == ["Umbrella Valves"]
    printed = capsys.readouterr().out
    assert "| a.pdf | OK |" in printed and "| b.pdf | OK |" in printed


def test_collect_inputs_skips_own_outputs(tmp_path):
    data = tmp_path / "data"
    for name in ("plano.pdf", "otro.PDF", "plano.sanitized.pdf", "x.anon.pdf"):
        _text_pdf(data / name, ["x"])
    _text_pdf(data / "out" / "y.pdf", ["x"])
    found = collect_inputs([data], [data / "out"])
    assert sorted(p.name for p in found) == ["otro.PDF", "plano.pdf"]


@pytest.mark.parametrize("flag", ["--reports", "--maps", "--map"])
def test_sensitive_outputs_must_be_inside_data(tmp_path, flag):
    pdf = _text_pdf(tmp_path / "in" / "a.pdf", ["x"])
    args = _args(tmp_path, _config(tmp_path), pdf)
    value = str(tmp_path / "fuera" / ("m.map.json" if flag == "--map" else "dir"))
    if flag in args:
        args[args.index(flag) + 1] = value
    else:
        args += [flag, value]
    with pytest.raises(SystemExit):
        main(args)


def test_config_with_errors_is_rejected(tmp_path, capsys):
    cfg = _config(
        tmp_path,
        """
        substitutions:
          - id: project_code
            category: project
            replacement: PRJ
            patterns: ['PRJ-\\d{4}']
            examples: [PRJ-4711]
        whitelist:
          - id: instrument_tag
            exceptions: []
          - id: equipment_tag
            exceptions: []
        """,
    )
    pdf = _text_pdf(tmp_path / "in" / "a.pdf", ["x"])
    assert main(_args(tmp_path, cfg, pdf)) == 1
    assert "ERROR en la config" in capsys.readouterr().err
