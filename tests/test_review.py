"""Tests del modelo de la UI de revisión (fase 8b), sin Qt."""

import os
import time
from pathlib import Path
from textwrap import dedent

import pymupdf
import pytest

from sanitizer.config_edit import ConfigEditor
from sanitizer.review import (
    collect_detections,
    discard,
    discard_action,
    generate,
    load_existing,
    locate,
    preview,
    zone_fraction,
)
from sanitizer.rules import load_config

EXAMPLE = Path(__file__).parent.parent / "configs" / "example.yaml"


@pytest.fixture
def setup(tmp_path: Path):
    cfg = tmp_path / "cliente.yaml"
    cfg.write_text(
        dedent(
            f"""\
            version: 1
            name: T
            extends: {EXAMPLE}
            disable: [side_strip, cover_logo]
            ocr:
              rotations: [0]
            """
        ),
        encoding="utf-8",
    )
    src = tmp_path / "in" / "plano.pdf"
    src.parent.mkdir()
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)
    page.insert_text((50, 80), "PUMP P-101A by Initech Pumps, 15 barg", fontsize=11)
    page.insert_text((50, 110), "Ref PRJ-4711 and PRJ-0000", fontsize=11)
    page.insert_text((50, 140), "Hooli Industries", fontsize=11)
    doc.save(src)
    data = tmp_path / "data"
    return src, cfg, data


def test_preview_lists_substitutions_zones_and_review_lines(setup):
    src, cfg, data = setup
    state = preview(src, load_config(cfg), data / "reports", data / "maps")
    assert state.origin == "vista previa" and state.anonymized.exists()
    assert "revision" in state.anonymized.parts
    kinds = {d.kind for d in state.detections}
    assert {"sustitución", "zona"} <= kinds
    subs = {d.text: d for d in state.detections if d.kind == "sustitución"}
    assert "Initech Pumps" in subs and subs["Initech Pumps"].rects
    assert subs["Initech Pumps"].detail.startswith("VENDOR-")
    # El mapa real no se toca
    assert not (data / "maps").exists()


def test_discard_substitution_adds_exception(setup):
    src, cfg, data = setup
    state = preview(src, load_config(cfg), data / "reports", data / "maps")
    target = next(d for d in state.detections if d.kind == "sustitución" and d.text == "PRJ-0000")
    assert "excepción" in discard_action(target)
    editor = ConfigEditor(cfg)
    discard(editor, target)
    again = preview(src, editor.config, data / "reports", data / "maps")
    assert "PRJ-0000" not in {d.text for d in again.detections if d.kind == "sustitución"}
    assert "PRJ-4711" in {d.text for d in again.detections if d.kind == "sustitución"}


def test_discard_zone_disables_it(setup):
    src, cfg, data = setup
    state = preview(src, load_config(cfg), data / "reports", data / "maps")
    zone = next(d for d in state.detections if d.kind == "zona")
    editor = ConfigEditor(cfg)
    discard(editor, zone)
    assert zone.rule not in {z.id for z in editor.config.zones}


def test_not_everything_is_discardable():
    det = collect_detections(
        {"near_misses": [{"page": 1, "source": "page", "rule": "r", "text": "xACMEx", "protected_by": "límite"}]},
        {"findings": [], "review_lines": [{"texto": "Hooli Industries", "páginas": [1, 2], "origen": ["texto"]}]},
    )
    assert [d.kind for d in det] == ["rozada", "revisar"]
    assert not any(d.discardable for d in det)
    assert det[1].on_page(2) and det[1].page == 0


def test_existing_reports_are_used_while_fresh(setup):
    src, cfg, data = setup
    config = load_config(cfg)
    result, state = generate(src, config, cfg, data / "out", data / "reports", data / "maps")
    assert result.ok and state is not None and state.origin == "generado"
    assert state.anonymized == data / "out" / "plano.sanitized.pdf"
    again = load_existing(src, cfg, data / "out", data / "reports")
    assert again is not None and again.origin == "informes"
    # Si la config cambia después, los informes ya no valen
    later = time.time() + 5
    os.utime(cfg, (later, later))
    assert load_existing(src, cfg, data / "out", data / "reports") is None


def test_locate_and_zone_fraction(setup):
    src, _, _ = setup
    with pymupdf.open(src) as doc:
        page = doc[0]
        rects = locate(page, "Hooli   Industries")
        assert len(rects) == 1 and rects[0][1] < 140 < rects[0][3] + 5
        assert zone_fraction(page, (842, 595, 421, 297.5)) == pytest.approx((0.5, 0.5, 1.0, 1.0))
