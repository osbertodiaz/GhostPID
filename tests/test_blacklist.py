"""Tests de la lista negra de la industria (CSV de empresas que se buscan siempre).

Los CSV de prueba usan nombres ficticios. El CSV versionado
(configs/blacklist/industria.csv) solo se carga y se comprueba entero.
"""

from pathlib import Path
from textwrap import dedent

import pymupdf
import pytest

from sanitizer.audit import build_blacklist
from sanitizer.blacklist import (
    BlacklistError,
    BlacklistPolicy,
    Entity,
    append_entity,
    build_rules,
    load_entities,
    main,
    read_csv,
    term_problem,
)
from sanitizer.config_edit import ConfigEditor, EditError
from sanitizer.redact import Namer, find_matches, redact_pdf
from sanitizer.rules import ConfigError, check_overlaps, load_config

CONFIGS = Path(__file__).parent.parent / "configs"
INDUSTRY = CONFIGS / "blacklist" / "industria.csv"
EXAMPLE = CONFIGS / "example.yaml"

HEADER = "name,aliases,category,segment,industries,country,active,notes\n"
FICTIONAL = HEADER + dedent(
    """\
    Zorvex Engineering,Zorvex|ZVX,epc,ingenieria_epc,oil_gas,ES,sí,
    Quillon Pumps,Quillon|QP Fluid Systems,manufacturer,bombas,general,DE,sí,
    Brantley Valves,Brantley,manufacturer,valvulas,pharma,US,sí,
    Lindeq,Lindeq Gases,operator,gases,gases,FR,sí,
    Dormant Corp,,manufacturer,,general,US,no,desactivada
    """
)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def kb_config(tmp_path: Path):
    """Config que hereda la base real pero con un CSV ficticio."""
    write(tmp_path / "blacklist" / "test.csv", FICTIONAL)

    def make(policy: str = "", extra: str = "") -> Path:
        """`policy`: líneas dentro del bloque blacklist; `extra`: resto del YAML."""
        path = tmp_path / "client.yaml"
        path.write_text(
            f"version: 1\nname: T\nextends: {CONFIGS / 'base.yaml'}\n"
            f"blacklist:\n  files: [blacklist/*.csv]\n  user_file: blacklist/propia.csv\n"
            + "".join(f"  {line}\n" for line in policy.splitlines())
            + dedent(extra),
            encoding="utf-8",
        )
        return path

    return make


def kb_hits(config, text: str, **kw) -> list[str]:
    matches, _ = find_matches(config, text, **kw)
    return [text[m.start : m.end] for m in matches if m.rule.id.startswith("kb-")]


# ---------------------------------------------------------------------------
# CSV versionado
# ---------------------------------------------------------------------------


def test_shipped_csv_is_valid_and_broad():
    entities = load_entities([INDUSTRY])
    cats = {e.category for e in entities}
    assert cats == {"epc", "licensor", "manufacturer", "operator"}
    assert len(entities) >= 800
    inds = {i for e in entities for i in e.industries}
    assert {"oil_gas", "chemicals", "gases", "pharma"} <= inds


@pytest.mark.parametrize("name", ["base.yaml", "example.yaml"])
def test_shipped_configs_load_blacklist_without_conflicts(name):
    config = load_config(CONFIGS / name)
    ids = {r.id for r in config.substitutions}
    assert {"kb-epc", "kb-licensor", "kb-manufacturer", "kb-operator"} <= ids
    assert [i for i in check_overlaps(config) if i.severity == "error"] == []


# Frases de P&ID que comparten palabras con nombres de empresa
PID_PHRASES = [
    "SHELL SIDE",
    "TUBE SIDE / SHELL SIDE DESIGN 15 barg",
    "TOTAL FLOW FIC-1203",
    "HYDRAULIC CYLINDER",
    "GATE VALVE",
    "CHECK VALVE",
    "GLOBE VALVE",
    "MOL % H2",
    "REV 2 NOV 2025",
    "BMS INTERLOCK",
    "API 610 PUMP",
    "OVERHEAD CRANE",
    "CARRIER GAS",
    "BOURDON TUBE",
    "RASCHIG RINGS",
    "PALL RINGS",
    "NORTH NNE",
    "TCE STORAGE",
    "15 m3/hora",
    "CENTER LINE",
    "POWER ELECTRONICS",
    "INSTRUMENT AIR",
    "PLANT AIR WATER SEPARATOR",
    "DELTA P",
    "UNION",
    "NATIONAL STANDARD",
    "TEMA TYPE AES",
    "WOOD STAVE",
    "MONO PUMP",
    "ECLIPSE",
    "SAFETY RELIEF VALVE",
    "CONTINENTAL",
    "PLC CABINET",
    "SIS ESD DCS",
    "LNG LPG NGL",
    "VÁLVULA DE SEGURIDAD",
    "BOMBA CENTRÍFUGA",
    "TOTAL SAMPLE",
]


@pytest.mark.parametrize("text", PID_PHRASES)
def test_pid_vocabulary_is_never_matched(text):
    config = load_config(CONFIGS / "base.yaml")
    assert kb_hits(config, text) == []
    assert kb_hits(config, text, relaxed=True) == []
    assert kb_hits(config, text, relaxed=True, fuzzy=True) == []


@pytest.mark.parametrize(
    "text, found",
    [
        ("VENDOR: FISHER 667", "FISHER"),
        ("Rosemount 3051S", "Rosemount"),
        ("Endress+Hauser Promass", "Endress+Hauser"),
        ("LICENSOR: Honeywell UOP", "Honeywell UOP"),
        ("Técnicas Reunidas", "Técnicas Reunidas"),
        ("AIR PRODUCTS", "AIR PRODUCTS"),
        ("Royal Dutch Shell", "Royal Dutch Shell"),
    ],
)
def test_shipped_names_are_found(text, found):
    config = load_config(CONFIGS / "base.yaml")
    assert found in kb_hits(config, text)


# ---------------------------------------------------------------------------
# Lectura y validación del CSV
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "term, ok",
    [
        ("Zorvex", True),
        ("Air Products", True),
        ("ZV", False),
        ("Shell", False),
        ("Crane", False),
        ("Total", False),
        ("Gate Valve", False),
        ("Válvula", False),
        ("1234", False),
    ],
)
def test_term_problem(term, ok):
    assert (term_problem(term) is None) is ok


def test_read_csv_semicolon_bom_and_spanish_categories(tmp_path):
    text = "﻿name;aliases;category\nZorvex;ZVX|Zorvex SA;ingenieria\nQuillon;;fabricante\n"
    entities, errors = read_csv(write(tmp_path / "x.csv", text))
    assert errors == []
    assert [(e.name, e.category) for e in entities] == [("Zorvex", "epc"), ("Quillon", "manufacturer")]
    assert entities[0].aliases == ("ZVX", "Zorvex SA")


@pytest.mark.parametrize(
    "row, fragment",
    [
        ("Zorvex,,banco,,,,,", "categoría"),
        ("Shell,,operator,,,,,", "vocabulario"),
        ("Zorvex,ZV,epc,,,,,", "demasiado corto"),
        ("Zorvex,,epc,,mining,,,", "industrias"),
        ("Zorvex,,epc,,,,quizá,", "active"),
    ],
)
def test_read_csv_reports_line_errors(tmp_path, row, fragment):
    _, errors = read_csv(write(tmp_path / "x.csv", HEADER + row + "\n"))
    assert errors and fragment in errors[0] and "x.csv:2" in errors[0]


def test_duplicate_terms_across_files_are_rejected(tmp_path):
    a = write(tmp_path / "a.csv", HEADER + "Zorvex,ZVX,epc,,,,,\n")
    b = write(tmp_path / "b.csv", HEADER + "Zorvex Group,Z.V.X.,epc,,,,,\n")
    with pytest.raises(BlacklistError) as info:
        load_entities([a, b])
    assert "ya figura en 'Zorvex'" in info.value.messages[0]


def test_bad_csv_is_a_config_error(kb_config, tmp_path):
    write(tmp_path / "blacklist" / "otro.csv", HEADER + "Shell,,operator,,,,,\n")
    with pytest.raises(ConfigError) as info:
        load_config(kb_config())
    assert any("blacklist:" in m and "vocabulario" in m for m in info.value.messages)


# ---------------------------------------------------------------------------
# Reglas generadas
# ---------------------------------------------------------------------------


def test_rules_from_csv(kb_config):
    config = load_config(kb_config())
    rules = {r.id: r for r in config.substitutions if r.id.startswith("kb-")}
    assert set(rules) == {"kb-epc", "kb-manufacturer", "kb-operator"}
    assert rules["kb-epc"].replacement == "EPC-{n}"
    assert rules["kb-manufacturer"].category.value == "manufacturer"
    assert "Dormant Corp" not in rules["kb-manufacturer"].terms  # active: no
    assert rules["kb-epc"].entity_of("ZVX") == "Zorvex Engineering"


def test_word_boundaries_everywhere(kb_config):
    config = load_config(kb_config())
    assert kb_hits(config, "LINDEQ GASES") == ["LINDEQ GASES"]
    # Ni dentro de otra palabra, ni en modo relajado (OCR pegado), ni en fuzzy
    for kw in ({}, {"relaxed": True}, {"relaxed": True, "fuzzy": True}):
        assert kb_hits(config, "CYLINDEQUE", **kw) == []
    # El OCR confunde 1/I y 0/O: se tolera
    assert kb_hits(config, "QU1LLON", relaxed=True, fuzzy=True) == ["QU1LLON"]


def test_acronyms_are_case_sensitive(kb_config):
    config = load_config(kb_config())
    assert kb_hits(config, "VENDOR ZVX") == ["ZVX"]
    assert kb_hits(config, "zvx") == []
    assert kb_hits(config, "zorvex") == ["zorvex"]


def test_policy_filters(kb_config):
    only_pharma = load_config(kb_config("industries: [pharma]\n"))
    terms = {t for r in only_pharma.substitutions if r.word_only for t in r.terms}
    assert "Brantley" in terms and "Quillon" in terms  # general siempre entra
    assert "Zorvex" not in terms

    excluded = load_config(kb_config("exclude: [Brantley]\ncategories: [manufacturer]\n"))
    kb = [r for r in excluded.substitutions if r.word_only]
    assert [r.id for r in kb] == ["kb-manufacturer"]
    assert "Brantley" not in kb[0].terms and "Brantley Valves" in kb[0].terms

    custom = load_config(kb_config("replacements: {manufacturer: 'SUPPLIER-{n}'}\n"))
    assert next(r for r in custom.substitutions if r.id == "kb-manufacturer").replacement == "SUPPLIER-{n}"

    off = load_config(kb_config("enabled: false\n"))
    assert not any(r.word_only for r in off.substitutions)


def test_client_override_adds_exceptions(kb_config):
    path = kb_config(
        extra="""\
        substitutions:
          - id: kb-manufacturer
            exceptions: [Quillon]
        """
    )
    config = load_config(path)
    rule = next(r for r in config.substitutions if r.id == "kb-manufacturer")
    assert "Quillon" not in rule.terms and "Quillon Pumps" in rule.terms
    assert kb_hits(config, "QUILLON") == []
    assert [r.id for r in config.substitutions].count("kb-manufacturer") == 1


def test_unknown_policy_keys_are_rejected(kb_config):
    with pytest.raises(ConfigError):
        load_config(kb_config("categories: [bank]\n"))


# ---------------------------------------------------------------------------
# Denominaciones: los alias de una entidad comparten número
# ---------------------------------------------------------------------------


def test_aliases_share_generic_name(kb_config):
    config = load_config(kb_config())
    rule = next(r for r in config.substitutions if r.id == "kb-manufacturer")
    namer = Namer()
    a = namer.name(rule.replacement, "Quillon Pumps", rule)
    b = namer.name(rule.replacement, "QP FLUID SYSTEMS", rule)
    c = namer.name(rule.replacement, "Brantley", rule)
    assert a == b == "VENDOR-1" and c == "VENDOR-2"
    assert namer.entries["VENDOR-1"]["entity"] == "Quillon Pumps"
    # Con el mapa de un documento anterior, otro alias recibe el mismo número
    again = Namer.from_entries(namer.entries)
    assert again.name(rule.replacement, "Quillon", rule) == "VENDOR-1"


# ---------------------------------------------------------------------------
# Redacción y auditoría
# ---------------------------------------------------------------------------


def test_redaction_end_to_end(kb_config, tmp_path):
    config = load_config(kb_config())
    src = tmp_path / "pid.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)
    page.insert_text((50, 100), "P-101A QUILLON PUMPS", fontsize=10)
    page.insert_text((50, 130), "PV-2001 QP FLUID SYSTEMS", fontsize=10)
    page.insert_text((50, 160), "HYDRAULIC CYLINDEQUE SHELL SIDE", fontsize=10)
    page.insert_text((50, 190), "OWNER LINDEQ GASES", fontsize=10)
    doc.save(src)
    out = tmp_path / "out.pdf"
    report = redact_pdf(src, out, config)
    with pymupdf.open(out) as d:
        text = d[0].get_text()
    assert "QUILLON" not in text and "QP FLUID" not in text and "OWNER LINDEQ" not in text
    assert "P-101A" in text and "PV-2001" in text and "CYLINDEQUE SHELL SIDE" in text
    assert text.count("VENDOR-1") == 2 and "OPR-1" in text
    assert {r.rule for r in report.replacements} == {"kb-manufacturer", "kb-operator"}


def test_audit_blacklist_uses_word_boundaries(kb_config):
    config = load_config(kb_config())
    bl = build_blacklist(config, None)
    assert "Lindeq" in bl.words and "Lindeq" not in bl.literals
    assert [h[2] for h in bl.text_hits("HYDRAULIC CYLINDEQUE")] == []
    assert [h[2] for h in bl.text_hits("Owner: Lindeq")] == ["Lindeq"]
    assert [h[2] for h in bl.ocr_hits("OWNER L1NDEQ")] == ["L1NDEQ"]
    # Los originales del mapa de reglas kb- también van con límites de palabra
    map_data = {"entries": {"OPR-1": {"template": "OPR-{n}", "rule": "kb-operator", "originals": {"LINDEQ": {}}}}}
    bl = build_blacklist(config, map_data)
    assert bl.text_hits("CYLINDEQUE") == []


# ---------------------------------------------------------------------------
# Añadir entidades (UI y CLI)
# ---------------------------------------------------------------------------


def test_append_entity_creates_file_and_checks_duplicates(tmp_path):
    target = tmp_path / "propia.csv"
    known = load_entities([write(tmp_path / "a.csv", FICTIONAL)])
    append_entity(target, Entity("Nombrex Ingeniería", ("Nombrex",), "epc"), known)
    entities, errors = read_csv(target)
    assert errors == [] and entities[0].aliases == ("Nombrex",)
    with pytest.raises(BlacklistError):
        append_entity(target, Entity("Zorvex SL", ("ZVX",), "epc"), known)
    with pytest.raises(BlacklistError):
        append_entity(target, Entity("Shell", (), "operator"), known)


def test_editor_adds_blacklist_terms_to_user_csv(kb_config, tmp_path):
    path = kb_config()
    editor = ConfigEditor(path)
    before = path.read_text(encoding="utf-8")
    config = editor.add_term("kb-manufacturer", "Vexlor  Instruments")
    assert path.read_text(encoding="utf-8") == before  # el YAML no cambia
    user = tmp_path / "blacklist" / "propia.csv"
    assert "Vexlor Instruments" in user.read_text(encoding="utf-8")
    assert kb_hits(config, "VEXLOR INSTRUMENTS") == ["VEXLOR INSTRUMENTS"]
    with pytest.raises(EditError):
        editor.add_term("kb-manufacturer", "Gate Valve")
    with pytest.raises(EditError):
        editor.add_term("kb-epc", "Zorvex")  # ya existe


def test_editor_exception_on_blacklist_rule(kb_config):
    path = kb_config()
    config = ConfigEditor(path).add_exception("kb-manufacturer", "Brantley")
    assert kb_hits(config, "BRANTLEY") == []
    assert "kb-manufacturer" in path.read_text(encoding="utf-8")


def test_cli(kb_config, tmp_path, capsys):
    cfg = str(kb_config())
    assert main(["--config", cfg, "stats"]) == 0
    assert "epc" in capsys.readouterr().out
    assert main(["--config", cfg, "check", "OWNER", "LINDEQ", "GASES"]) == 0
    assert "Lindeq" in capsys.readouterr().out
    assert main(["--config", cfg, "add", "Corvanta Labs", "--category", "operador", "--industries", "pharma"]) == 0
    assert "Corvanta Labs" in (tmp_path / "blacklist" / "propia.csv").read_text(encoding="utf-8")
    assert main(["--config", cfg, "find", "corvanta"]) == 0
    assert "operator" in capsys.readouterr().out
    assert main(["--config", cfg, "add", "Shell", "--category", "operator"]) == 1


def test_build_rules_without_files_is_empty():
    assert build_rules(BlacklistPolicy()) == []
