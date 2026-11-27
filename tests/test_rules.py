"""Tests de la fase 2: esquema de reglas, herencia, zonas y solapes."""

from pathlib import Path
from textwrap import dedent

import pytest

from sanitizer.inventory import DetectionParams
from sanitizer.rules import (
    Config,
    ConfigError,
    Zone,
    check_overlaps,
    explain,
    load_config,
    main,
    term_regex,
)

CONFIGS = Path(__file__).parent.parent / "configs"
BASE = CONFIGS / "base.yaml"


def write(tmp_path: Path, text: str, name: str = "client.yaml") -> Path:
    path = tmp_path / name
    path.write_text(dedent(text), encoding="utf-8")
    return path


def client(tmp_path: Path, body: str) -> Config:
    """Config de cliente que hereda de la base real."""
    return load_config(write(tmp_path, f"version: 1\nname: Test\nextends: {BASE}\n" + dedent(body)))


def errors_of(tmp_path: Path, text: str) -> list[str]:
    with pytest.raises(ConfigError) as info:
        load_config(write(tmp_path, text))
    return info.value.messages


# ---------------------------------------------------------------------------
# Configs versionadas
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["base.yaml", "example.yaml"])
def test_shipped_configs_are_valid_and_conflict_free(name):
    config = load_config(CONFIGS / name)
    assert [i for i in check_overlaps(config) if i.severity == "error"] == []


def test_example_inherits_base_and_adds_its_rules():
    config = load_config(CONFIGS / "example.yaml")
    ids = [r.id for r in config.whitelist]
    assert ids == [r.id for r in load_config(BASE).whitelist]  # nada nuevo, solo ajustes
    tag = next(r for r in config.whitelist if r.id == "instrument_tag")
    assert tag.exceptions == [r"PRJ-\d{4}"]
    assert tag.examples  # heredados de la base
    assert {s.category.value for s in config.substitutions} >= {"company", "operator", "manufacturer"}


# ---------------------------------------------------------------------------
# Lista blanca
# ---------------------------------------------------------------------------


def test_token_boundary_protects_tags_but_not_fragments():
    rx = next(r for r in load_config(BASE).whitelist if r.id == "instrument_tag").regex
    assert rx.search("VALVE FIC-1203, OPEN")
    assert rx.search("(FIC-1203)")
    assert not rx.search("ACM-2041-PID-001")  # fragmento de una referencia
    assert not rx.search("x1FIC-1203")  # pegado a otro token


def test_base_whitelist_keeps_process_data():
    config = load_config(BASE)
    text = 'P-101A 6"-PL-1001-A1A 3/4" 15 barg 120 °C HH ESD I-101 NOTE 2 V4-650'
    protected = {line.split("'")[1] for line in explain(config, text)}
    assert protected >= {"P-101A", '6"-PL-1001-A1A', '3/4"', "15 barg", "120 °C", "HH", "I-101", "NOTE 2", "V4-650"}


def test_metric_line_number_does_not_swallow_document_refs():
    config = load_config(BASE)
    assert explain(config, "12345-ABC-PRO-PID-001") == ["  (ninguna regla encaja)"]


def test_invalid_regex_reports_rule_id(tmp_path):
    msgs = errors_of(
        tmp_path,
        """
        version: 1
        name: X
        whitelist:
          - id: broken
            pattern: '[A-Z'
            examples: [A]
        """,
    )
    assert any("whitelist[0:broken]" in m and "regex inválida" in m for m in msgs)


def test_examples_must_match_whole(tmp_path):
    msgs = errors_of(
        tmp_path,
        """
        version: 1
        name: X
        whitelist:
          - id: tag
            pattern: '[A-Z]-\\d+'
            examples: [P-101, P-101A]
        """,
    )
    assert any("P-101A" in m for m in msgs)


def test_counter_examples_must_not_match(tmp_path):
    msgs = errors_of(
        tmp_path,
        """
        version: 1
        name: X
        whitelist:
          - id: tag
            pattern: '[A-Z]-\\d+'
            examples: [P-101]
            counter_examples: [V-200]
        """,
    )
    assert any("contraejemplos" in m for m in msgs)


def test_unknown_keys_are_rejected(tmp_path):
    msgs = errors_of(
        tmp_path,
        """
        version: 1
        name: X
        whitelst: []
        """,
    )
    assert any("whitelst" in m for m in msgs)


def test_exceptions_unprotect_lookalikes(tmp_path):
    config = client(
        tmp_path,
        """
        whitelist:
          - id: instrument_tag
            exceptions: ['PRJ-\\d{4}']
          - id: equipment_tag
            exceptions: ['PRJ-\\d{4}']
        """,
    )
    assert not any("protegido" in line for line in explain(config, "PRJ-4711"))
    assert any("protegido" in line for line in explain(config, "FIC-4711"))


# ---------------------------------------------------------------------------
# Sustituciones
# ---------------------------------------------------------------------------


def test_term_regex_flexible_whitespace_and_word_boundaries():
    import re

    rx = re.compile(term_regex("ACME Engineering"), re.IGNORECASE)
    assert rx.search("by ACME\nEngineering Ltd")
    assert rx.search("acme   engineering")
    assert not rx.search("ACMEX Engineering")
    assert re.compile(term_regex("B&C")).search("(B&C)")


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ("terms: []", "al menos un término"),
        ("patterns: ['DOC-\\d+']", "necesitan ejemplos"),
        ("patterns: ['DOC-\\d+']\n    examples: [XYZ]", "no encajan"),
        ("terms: [ACME]\n    replacement: 'EPC-{x}'", "solo admite {n}"),
        ("terms: ['  ']", "términos vacíos"),
    ],
)
def test_invalid_substitutions(tmp_path, body, fragment):
    if "replacement" not in body:
        body += "\n    replacement: EPC"
    msgs = errors_of(
        tmp_path,
        f"""
version: 1
name: X
substitutions:
  - id: s
    category: company
    {body}
""",
    )
    assert any(fragment in m for m in msgs), msgs


def test_unknown_category_rejected(tmp_path):
    msgs = errors_of(
        tmp_path,
        """
        version: 1
        name: X
        substitutions:
          - id: s
            category: competitor
            replacement: X
            terms: [Foo]
        """,
    )
    assert any("category" in m for m in msgs)


# ---------------------------------------------------------------------------
# Solapes
# ---------------------------------------------------------------------------


def test_substitution_touching_protected_example_is_error(tmp_path):
    config = client(
        tmp_path,
        """
        substitutions:
          - id: bad
            category: other
            replacement: X
            patterns: ['FIC']
            examples: [FIC]
            boundary: none
        """,
    )
    errors = [i.message for i in check_overlaps(config) if i.severity == "error"]
    assert any("'bad' modificaría 'FIC'" in m for m in errors)


def test_fully_protected_sample_is_error(tmp_path):
    config = client(
        tmp_path,
        """
        substitutions:
          - id: project_code
            category: project
            replacement: PRJ-{n}
            patterns: ['PRJ-\\d{4}']
            examples: [PRJ-4711]
        """,
    )
    errors = [i.message for i in check_overlaps(config) if i.severity == "error"]
    assert any("PRJ-4711" in m and "nunca se sustituiría" in m for m in errors)


def test_partially_protected_sample_is_warning(tmp_path):
    config = client(
        tmp_path,
        """
        substitutions:
          - id: vendor
            category: manufacturer
            replacement: VENDOR-{n}
            terms: [Initech P-101 Series]
        """,
    )
    issues = check_overlaps(config)
    assert [i.severity for i in issues if "Initech" in i.message] == ["warning"]
    assert "a medias" in issues[0].message


def test_duplicate_and_short_terms_warn(tmp_path):
    config = client(
        tmp_path,
        """
        substitutions:
          - id: a
            category: company
            replacement: EPC
            terms: [Globex, GX]
          - id: b
            category: operator
            replacement: OPR
            terms: [globex]
        """,
    )
    messages = [i.message for i in check_overlaps(config) if i.severity == "warning"]
    assert any("'globex' está en 'a' y en 'b'" in m for m in messages)
    assert any("'GX'" in m and "muy corto" in m for m in messages)


def test_explain_marks_blocked_substitutions(tmp_path):
    config = client(
        tmp_path,
        """
        substitutions:
          - id: project_code
            category: project
            replacement: PRJ-{n}
            patterns: ['PRJ-\\d{4}']
            examples: [PRJ-4711]
        """,
    )
    assert any("BLOQUEADO" in line for line in explain(config, "see PRJ-4711"))


# ---------------------------------------------------------------------------
# Zonas
# ---------------------------------------------------------------------------

A3_W, A3_H = 1190.55, 841.89  # A3 apaisado en pt


def test_zone_fraction_bottom_right():
    zone = Zone(id="tb", rect=(0.0, 0.0, 0.25, 0.2), anchor="bottom-right")
    x0, y0, x1, y1 = zone.resolve(A3_W, A3_H)
    assert x0 == pytest.approx(A3_W * 0.75) and x1 == pytest.approx(A3_W)
    assert y0 == pytest.approx(A3_H * 0.8) and y1 == pytest.approx(A3_H)


def test_zone_mm_keeps_physical_size_on_any_sheet():
    zone = Zone(id="strip", rect=(0, 0, 15, 200), unit="mm", anchor="bottom-left")
    for w, h in ((A3_W, A3_H), (2384, 1684)):
        x0, y0, x1, y1 = zone.resolve(w, h)
        assert (x0, x1) == (0, pytest.approx(15 * 72 / 25.4))
        assert y1 == pytest.approx(h) and y1 - y0 == pytest.approx(200 * 72 / 25.4)


def test_zone_is_clipped_to_page():
    zone = Zone(id="big", rect=(0, 0, 1000, 1000), unit="mm")
    assert zone.resolve(A3_W, A3_H) == (0, 0, A3_W, A3_H)


def test_zone_pages_and_orientation():
    zone = Zone(id="z", rect=(0, 0, 1, 1), pages=[1, "3-4", -1], orientation="landscape")
    assert zone.page_numbers(10) == {1, 3, 4, 10}
    assert zone.applies_to(3, 10, A3_W, A3_H)
    assert not zone.applies_to(3, 10, A3_H, A3_W)  # vertical
    assert not zone.applies_to(2, 10, A3_W, A3_H)


@pytest.mark.parametrize(
    ("zone", "fragment"),
    [
        ("rect: [0.5, 0, 0.2, 1]", "x0 < x1"),
        ("rect: [0, 0, 1.2, 1]", "de 0 a 1"),
        ("rect: [-1, 0, 10, 10]\n    unit: mm", "negativos"),
        ("rect: [0, 0, 1, 1]\n    pages: [0]", "empiezan en 1"),
        ("rect: [0, 0, 1, 1]\n    pages: ['7-3']", "rango de páginas"),
        ("rect: [0, 0, 1, 1]\n    anchor: middle", "anchor"),
    ],
)
def test_invalid_zones(tmp_path, zone, fragment):
    msgs = errors_of(tmp_path, f"version: 1\nname: X\nzones:\n  - id: z\n    {zone}\n")
    assert any(fragment in m for m in msgs), msgs


# ---------------------------------------------------------------------------
# Herencia
# ---------------------------------------------------------------------------


def test_extends_merges_by_id_appends_and_disables(tmp_path):
    write(
        tmp_path,
        """
        version: 1
        name: Parent
        whitelist:
          - {id: a, pattern: 'A-\\d+', examples: [A-1]}
          - {id: b, pattern: 'B-\\d+', examples: [B-1]}
        zones:
          - {id: tb, rect: [0, 0, 0.2, 0.2]}
        detection:
          stroke_text_min_glyphs: 80
          stroke_text: {min_run: 4}
        """,
        "parent.yaml",
    )
    config = load_config(
        write(
            tmp_path,
            """
            version: 1
            name: Child
            extends: parent.yaml
            disable: [b]
            whitelist:
              - {id: a, examples: [A-1, A-22]}
              - {id: c, pattern: 'C-\\d+', examples: [C-1]}
            detection:
              stroke_text: {min_glyph_height: 2.0}
            """,
        )
    )
    assert config.name == "Child"
    assert [r.id for r in config.whitelist] == ["a", "c"]
    assert config.whitelist[0].pattern == r"A-\d+" and config.whitelist[0].examples == ["A-1", "A-22"]
    assert [z.id for z in config.zones] == ["tb"]
    assert config.detection.stroke_text_min_glyphs == 80
    assert config.detection.stroke_text.min_run == 4  # del padre
    assert config.detection.stroke_text.min_glyph_height == 2.0  # del hijo


def test_circular_extends(tmp_path):
    write(tmp_path, "version: 1\nname: A\nextends: b.yaml\n", "a.yaml")
    write(tmp_path, "version: 1\nname: B\nextends: a.yaml\n", "b.yaml")
    with pytest.raises(ConfigError, match="herencia circular"):
        load_config(tmp_path / "a.yaml")


def test_missing_parent_and_unknown_disable(tmp_path):
    with pytest.raises(ConfigError, match="no existe"):
        load_config(write(tmp_path, "version: 1\nname: X\nextends: nope.yaml\n"))
    with pytest.raises(ConfigError, match="no existen"):
        load_config(write(tmp_path, f"version: 1\nname: X\nextends: {BASE}\ndisable: [ghost]\n"))


def test_malformed_yaml(tmp_path):
    with pytest.raises(ConfigError, match="YAML mal formado"):
        load_config(write(tmp_path, "name: [unclosed\n"))


# ---------------------------------------------------------------------------
# Detección configurable
# ---------------------------------------------------------------------------


def test_detection_defaults_and_validation(tmp_path):
    assert load_config(BASE).detection == DetectionParams()
    msgs = errors_of(tmp_path, "version: 1\nname: X\ndetection:\n  stroke_text: {min_glyph_hight: 2}\n")
    assert any("min_glyph_hight" in m for m in msgs)


def test_inventory_uses_config_thresholds(tmp_path, synthetic_pdf, capsys):
    from sanitizer.inventory import main as inventory_main

    cfg = write(tmp_path, "version: 1\nname: X\ndetection:\n  image_coverage_threshold: 0.9\n")
    assert inventory_main([str(synthetic_pdf), "--config", str(cfg)]) == 0
    bad = write(tmp_path, "version: 1\nname: X\ndetection: {nope: 1}\n", "bad.yaml")
    with pytest.raises(SystemExit):
        inventory_main([str(synthetic_pdf), "--config", str(bad)])
    assert "config inválida" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_exit_codes(tmp_path, capsys):
    assert main([str(CONFIGS / "example.yaml"), "--check", "Globex P-101A"]) == 0
    out = capsys.readouterr().out
    assert "→ 'OPR'" in out and "protegido  'P-101A'" in out

    conflict = write(
        tmp_path,
        f"""
        version: 1
        name: X
        extends: {BASE}
        substitutions:
          - id: project_code
            category: project
            replacement: PRJ
            patterns: ['PRJ-\\d{{4}}']
            examples: [PRJ-4711]
        """,
    )
    assert main([str(conflict)]) == 1
    assert main([str(tmp_path / "missing.yaml")]) == 1
