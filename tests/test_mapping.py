"""Tests de la fase 6: mapa de sustitución y reversión."""

import io
import json
import os
import stat
from pathlib import Path
from textwrap import dedent

import pymupdf
import pytest

from sanitizer.mapping import (
    MapError,
    canonical,
    empty_map,
    entities,
    load_map,
    main,
    revert,
    save_map,
)
from sanitizer.pipeline import anonymize
from sanitizer.redact import Namer
from sanitizer.rules import check_overlaps, load_config

CONFIGS = Path(__file__).parent.parent / "configs"
EXAMPLE = CONFIGS / "example.yaml"


def _config(tmp_path: Path, extra: str = ""):
    path = tmp_path / "cfg.yaml"
    path.write_text(
        f"version: 1\nname: T\nextends: {EXAMPLE}\ndisable: [title_block, side_strip, cover_logo]\n"
        "ocr:\n  enabled: false\n" + dedent(extra),
        encoding="utf-8",
    )
    return load_config(path)


def _pdf(tmp_path: Path, name: str, lines: list[str]) -> Path:
    doc = pymupdf.open()
    page = doc.new_page()
    for i, line in enumerate(lines):
        page.insert_text((50, 80 + 20 * i), line, fontsize=10)
    path = tmp_path / name
    doc.save(path)
    return path


def _entries(**generics) -> dict:
    """Entradas de mapa compactas: VENDOR_1={"Initech": (3, 3)} → count 3, exact 3."""
    out = {}
    for key, originals in generics.items():
        value = key.replace("_", "-")
        out[value] = {
            "template": value,
            "n": None,
            "rule": "r",
            "category": "other",
            "originals": {o: {"count": c, "exact": e} for o, (c, e) in originals.items()},
        }
    return {**empty_map(), "entries": out}


# ---------------------------------------------------------------------------
# Namer
# ---------------------------------------------------------------------------


def test_namer_records_provenance_and_seeds_numbering(tmp_path):
    cfg = _config(tmp_path)
    vendors = next(r for r in cfg.substitutions if r.id == "vendors")
    namer = Namer()
    assert namer.name("VENDOR-{n}", "Initech Pumps", vendors) == "VENDOR-1"
    namer.name("VENDOR-{n}", "INITECH PUMPS", vendors, exact=False)
    entry = namer.entries["VENDOR-1"]
    assert entry["rule"] == "vendors" and entry["category"] == "manufacturer" and entry["n"] == 1
    assert entry["originals"]["Initech Pumps"] == {"count": 1, "exact": 1}
    assert entry["originals"]["INITECH PUMPS"] == {"count": 1, "exact": 0}
    namer.name("VENDOR-{n}", "Initech\nPumps", vendors)  # partido en dos líneas en el plano
    assert entry["originals"]["Initech Pumps"] == {"count": 2, "exact": 2}

    seeded = Namer.from_entries(namer.entries)
    assert seeded.name("VENDOR-{n}", "initech pumps") == "VENDOR-1"  # mismo original
    assert seeded.name("VENDOR-{n}", "Umbrella Valves") == "VENDOR-2"  # la numeración sigue


# ---------------------------------------------------------------------------
# Guardar y cargar
# ---------------------------------------------------------------------------


def test_map_is_only_saved_inside_data(tmp_path):
    with pytest.raises(MapError, match="solo se guarda dentro de data/"):
        save_map(empty_map(), tmp_path / "maps" / "x.map.json")
    path = save_map(empty_map(), tmp_path / "data" / "maps" / "x.map.json")
    if os.name == "posix":  # en Windows chmod solo controla el atributo de solo lectura
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert load_map(path) == empty_map()


@pytest.mark.parametrize(
    ("content", "fragment"),
    [("{", "JSON mal formado"), ('{"version": 9}', "no es un mapa"), ('{"version": 1, "entries": {}}', "documents")],
)
def test_load_map_errors(tmp_path, content, fragment):
    path = tmp_path / "data" / "bad.map.json"
    path.parent.mkdir()
    path.write_text(content)
    with pytest.raises(MapError, match=fragment):
        load_map(path)
    with pytest.raises(MapError, match="no existe"):
        load_map(tmp_path / "data" / "missing.map.json")


# ---------------------------------------------------------------------------
# Reversión
# ---------------------------------------------------------------------------


def test_revert_longest_first_and_word_boundaries():
    data = _entries(VENDOR_1={"Initech Pumps": (2, 2)}, VENDOR_10={"Umbrella Valves": (1, 1)}, EPC={"ACME": (4, 4)})
    text = "VENDOR-10 supplies the valves, Vendor-1 the pumps; EPC is EPC. Not EPCX nor VENDOR-1A."
    result, report = revert(text, data)
    assert result == (
        "Umbrella Valves supplies the valves, Initech Pumps the pumps; ACME is ACME. Not EPCX nor VENDOR-1A."
    )
    assert report.counts == {"VENDOR-10": 1, "VENDOR-1": 1, "EPC": 2}


def test_revert_prefers_exact_original_over_ocr_variants():
    entry = _entries(OPR={"GL0BEX PETR0LEUM": (9, 0), "Globex Petroleum": (2, 2)})["entries"]["OPR"]
    assert canonical(entry) == "Globex Petroleum"
    assert len(entities(entry)) == 1  # la lectura de OCR es la misma entidad


def test_revert_reports_ambiguous_generics():
    data = _entries(SITE={"Springfield": (5, 5), "Shelbyville": (1, 1)}, EPC={"ACME": (1, 1), "ACME Eng.": (1, 1)})
    result, report = revert("Plant at SITE built by EPC", data)
    # SITE: el más frecuente; EPC: a igualdad de apariciones, el más largo
    assert result == "Plant at Springfield built by ACME Eng."
    assert report.ambiguous == {"SITE": ["Springfield", "Shelbyville"]}  # EPC: abreviatura, no ambigua


def test_revert_ignores_deleted_values():
    data = _entries(**{"": {"John Doe": (3, 3)}})
    assert revert("drawn by nobody", data)[0] == "drawn by nobody"


# ---------------------------------------------------------------------------
# Pipeline con mapa
# ---------------------------------------------------------------------------


def test_numbering_is_shared_across_documents(tmp_path):
    cfg = _config(tmp_path)
    map_path = tmp_path / "data" / "maps" / "project.map.json"
    a = _pdf(tmp_path, "a.pdf", ["Pump P-101A by Initech Pumps", "Drawing ACM-2041-PID-001"])
    b = _pdf(tmp_path, "b.pdf", ["Valve by Umbrella Valves", "Pump by Initech Pumps", "Drawing ACM-2041-PID-002"])
    anonymize(a, tmp_path / "a.out.pdf", cfg, map_path)
    anonymize(b, tmp_path / "b.out.pdf", cfg, map_path)

    with pymupdf.open(tmp_path / "b.out.pdf") as doc:
        text = doc[0].get_text()
    assert "VENDOR-1" in text and "VENDOR-2" in text and "DOC-REF-2" in text

    data = load_map(map_path)
    # VENDOR-1 sigue siendo Initech en el segundo documento; la numeración continúa
    assert list(data["entries"]["VENDOR-1"]["originals"]) == ["Initech Pumps"]
    assert list(data["entries"]["VENDOR-2"]["originals"]) == ["Umbrella Valves"]
    assert list(data["entries"]["DOC-REF-2"]["originals"]) == ["ACM-2041-PID-002"]
    assert [d["source"] for d in data["documents"]] == ["a.pdf", "b.pdf"]
    assert data["entries"]["VENDOR-1"]["originals"]["Initech Pumps"]["count"] == 2

    # Reprocesar un documento sustituye su registro en vez de duplicarlo
    anonymize(a, tmp_path / "a2.out.pdf", cfg, map_path)
    assert [d["source"] for d in load_map(map_path)["documents"]] == ["b.pdf", "a.pdf"]


def test_map_outside_data_is_rejected_before_working(tmp_path):
    cfg = _config(tmp_path)
    pdf = _pdf(tmp_path, "a.pdf", ["Pump by Initech Pumps"])
    out = tmp_path / "out.pdf"
    with pytest.raises(MapError):
        anonymize(pdf, out, cfg, tmp_path / "maps" / "x.map.json")
    assert not out.exists()


def test_round_trip(tmp_path):
    cfg = _config(tmp_path)
    map_path = tmp_path / "data" / "rt.map.json"
    lines = ["Prepared by ACME Engineering for Globex Petroleum", "Pump P-101A by Initech Pumps, 15 barg"]
    pdf = _pdf(tmp_path, "rt.pdf", lines)
    out = tmp_path / "rt.out.pdf"
    anonymize(pdf, out, cfg, map_path)
    with pymupdf.open(out) as doc:
        anonymized = doc[0].get_text()
    assert "ACME" not in anonymized and "Globex" not in anonymized
    # Simula la respuesta de un LLM escrita con las denominaciones genéricas
    answer = "P-101A is supplied by VENDOR-1; the EPC is EPC and the owner OPR."
    restored, report = revert(answer, load_map(map_path))
    assert restored == (
        "P-101A is supplied by Initech Pumps; the ACME Engineering is ACME Engineering "
        "and the owner Globex Petroleum."
    )
    assert report.ambiguous == {}


# ---------------------------------------------------------------------------
# Validación y CLI
# ---------------------------------------------------------------------------


def test_validate_warns_when_reversion_would_merge_entities(tmp_path):
    cfg = _config(
        tmp_path,
        """
        substitutions:
          - id: site
            category: location
            replacement: SITE
            terms: [Springfield Refinery, Shelbyville Terminal]
        """,
    )
    warnings = [i.message for i in check_overlaps(cfg) if i.severity == "warning"]
    assert any("'site'" in w and "entidades distintas" in w for w in warnings)
    assert not any("entidades distintas" in w for w in (i.message for i in check_overlaps(load_config(EXAMPLE))))


def test_revert_cli(tmp_path, monkeypatch, capsys):
    data = _entries(VENDOR_1={"Initech Pumps": (1, 1)}, SITE={"Springfield": (2, 2), "Shelbyville": (1, 1)})
    map_path = save_map(data, tmp_path / "data" / "m.map.json")
    monkeypatch.setattr("sys.stdin", io.StringIO("VENDOR-1 at SITE"))
    assert main(["--map", str(map_path)]) == 0
    captured = capsys.readouterr()
    assert captured.out == "Initech Pumps at Springfield"
    assert "revertidas: 2" in captured.err and "AVISO: 'SITE'" in captured.err

    src = tmp_path / "answer.txt"
    src.write_text("VENDOR-1", encoding="utf-8")
    dst = tmp_path / "answer.out.txt"
    assert main(["--map", str(map_path), str(src), "-o", str(dst)]) == 0
    assert dst.read_text(encoding="utf-8") == "Initech Pumps"
    assert main(["--map", str(tmp_path / "data" / "none.json")]) == 1


def test_map_json_is_sorted_and_readable(tmp_path):
    path = save_map(_entries(EPC={"ACME": (1, 1)}), tmp_path / "data" / "s.map.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert list(data) == sorted(data)
