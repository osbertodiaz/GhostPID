"""Tests de la edición de la config desde la UI (fase 8b)."""

from pathlib import Path
from textwrap import dedent

import pytest

from sanitizer.config_edit import ConfigEditor, EditError
from sanitizer.redact import find_matches
from sanitizer.rules import load_config

EXAMPLE = Path(__file__).parent.parent / "configs" / "example.yaml"


@pytest.fixture
def client(tmp_path: Path) -> Path:
    path = tmp_path / "cliente.yaml"
    path.write_text(
        dedent(
            f"""\
            # Config de prueba: este comentario debe sobrevivir
            version: 1
            name: Cliente
            extends: {EXAMPLE}

            substitutions:
              - id: owner  # comentario en línea
                category: operator
                replacement: OPR
                terms: [Globex Petroleum]
            """
        ),
        encoding="utf-8",
    )
    return path


def test_add_term_to_own_rule_keeps_comments(client):
    cfg = ConfigEditor(client).add_term("owner", "Globex   Gas")
    text = client.read_text(encoding="utf-8")
    assert "# Config de prueba" in text and "# comentario en línea" in text
    rule = next(r for r in cfg.substitutions if r.id == "owner")
    assert rule.terms == ["Globex Petroleum", "Globex Gas"]
    assert (client.parent / "cliente.yaml.bak").exists()


def test_add_term_to_inherited_rule_keeps_inherited_terms(client):
    before = next(r for r in load_config(client).substitutions if r.id == "vendors").terms
    cfg = ConfigEditor(client).add_term("vendors", "Hooli Motors")
    after = next(r for r in cfg.substitutions if r.id == "vendors").terms
    assert after == [*before, "Hooli Motors"]


def test_duplicate_term_is_a_noop(client):
    editor = ConfigEditor(client)
    before = client.read_text(encoding="utf-8")
    editor.add_term("owner", "globex petroleum")
    assert client.read_text(encoding="utf-8") == before


def test_add_exception_stops_substitution(client):
    cfg = ConfigEditor(client).add_exception("project_code", "PRJ-0000")
    accepted, _ = find_matches(cfg, "PRJ-0000 PRJ-4711")
    assert [(m.start, m.end) for m in accepted] == [(9, 17)]


def test_invalid_change_is_rolled_back(client):
    editor = ConfigEditor(client)
    before = client.read_text(encoding="utf-8")
    # Un término que es un ejemplo de la regla contradice la excepción
    with pytest.raises(EditError):
        editor.add_exception("project_code", "PRJ-4711")
    assert client.read_text(encoding="utf-8") == before
    with pytest.raises(EditError):
        editor.add_term("no_existe", "x")


def test_add_rule_and_zone(client):
    editor = ConfigEditor(client)
    editor.add_rule("plant_name", "location", "SITE", ["Springfield Plant"])
    zone_id, cfg = editor.add_zone((0.8, 0.85, 1.2, 1.0), pages=[1, "3-4"], label="[CAJETÍN]")
    zone = next(z for z in cfg.zones if z.id == zone_id)
    assert zone.rect == (0.8, 0.85, 1.0, 1.0) and zone.pages == [1, "3-4"]
    assert any(r.id == "plant_name" for r in cfg.substitutions)
    second, _ = editor.add_zone((0, 0, 0.1, 0.1))
    assert second != zone_id


def test_logo_decisions_move_between_lists(client):
    editor = ConfigEditor(client)
    cfg = editor.set_logo("0123456789abcdef", remove=False)
    assert "0123456789abcdef" in cfg.logos.keep
    cfg = editor.set_logo("0123456789abcdef", remove=True)
    assert "0123456789abcdef" in cfg.logos.remove and "0123456789abcdef" not in cfg.logos.keep
    with pytest.raises(EditError):
        editor.set_logo("nope", remove=True)


def test_disable_inherited_zone(client):
    cfg = ConfigEditor(client).disable("title_block")
    assert "title_block" not in {z.id for z in cfg.zones}
    with pytest.raises(EditError):
        ConfigEditor(client).disable("nada")
