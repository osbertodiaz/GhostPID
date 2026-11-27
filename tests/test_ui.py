"""Tests de la UI de revisión (fase 8b) con pytest-qt, sin pantalla."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path  # noqa: E402
from textwrap import dedent  # noqa: E402

import pymupdf  # noqa: E402
import pytest  # noqa: E402

from sanitizer.rules import load_config  # noqa: E402
from sanitizer.ui import ReviewWindow, main, parse_pages  # noqa: E402

EXAMPLE = Path(__file__).parent.parent / "configs" / "example.yaml"


@pytest.fixture
def window(qtbot, tmp_path: Path):
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
    for n in range(2):
        page = doc.new_page(width=842, height=595)
        page.insert_text((50, 80), f"PUMP P-10{n}A by Initech Pumps, 15 barg", fontsize=11)
        page.insert_text((50, 110), "Ref PRJ-4711 and PRJ-0000", fontsize=11)
        page.insert_text((50, 140), "Hooli Industries", fontsize=11)
    doc.save(src)
    data = tmp_path / "data"
    win = ReviewWindow(src, cfg, data / "out", data / "reports", data / "maps", synchronous=True)
    qtbot.addWidget(win)
    win.start()
    return win


def _row_of(win: ReviewWindow, kind: str, text: str) -> int:
    for row, index in enumerate(win._rows):
        d = win.state.detections[index]
        if d.kind == kind and d.text == text:
            return row
    raise AssertionError(f"{kind} {text!r} no está en la lista")


def test_opens_with_quick_preview_and_marks(window):
    assert window.state.origin == "vista previa"
    assert window.table.rowCount() > 0
    row = _row_of(window, "sustitución", "Initech Pumps")
    index = window._rows[row]
    assert window.view.marks(index)  # rectángulo dibujado sobre la página
    window.table.selectRow(row)
    assert window.discard_btn.isEnabled()


def test_page_filter_and_navigation(window):
    on_page_1 = window.table.rowCount()
    window.page_only.setChecked(False)
    assert window.table.rowCount() > on_page_1
    window.page_only.setChecked(True)
    window.go_to(2)
    assert window.page_box.value() == 2
    window.kind_boxes["sustitución"].setChecked(False)
    assert all(window.state.detections[i].kind != "sustitución" for i in window._rows)


def test_discard_then_recalculate(window):
    window.table.selectRow(_row_of(window, "sustitución", "PRJ-0000"))
    action = window.discard_selected()
    assert "PRJ-0000" in action and window.stale
    assert "PRJ-0000" in window.config_path.read_text(encoding="utf-8")
    window.run_preview()
    assert not window.stale
    texts = {d.text for d in window.state.detections if d.kind == "sustitución"}
    assert "PRJ-0000" not in texts and "PRJ-4711" in texts


def test_add_term_from_review_line(window):
    window.add_term("vendors", "Hooli Industries")
    window.run_preview()
    texts = {d.text for d in window.state.detections if d.kind == "sustitución"}
    assert "Hooli Industries" in texts
    window.add_rule("plant_name", "location", "SITE", "Springfield")
    assert any(r.id == "plant_name" for r in load_config(window.config_path).substitutions)


def test_draw_zone_and_generate(window):
    # Recuadro de la mitad inferior derecha de la página 1, solo en esa página
    zone_id = window.create_zone((421, 297.5, 842, 595), [1])
    zone = next(z for z in load_config(window.config_path).zones if z.id == zone_id)
    assert zone.rect == pytest.approx((0.5, 0.5, 1.0, 1.0)) and zone.pages == [1]
    window.run_generate()
    assert window.state.origin == "generado"
    out = window.out_dir / "plano.sanitized.pdf"
    assert out.exists() and window.anonymized is not None and window.show_anonymized
    with pymupdf.open(out) as doc:
        assert "Initech" not in doc[0].get_text()
    assert window.report_btn.isEnabled()


def test_parse_pages():
    assert parse_pages("1, 3-7; -1") == [1, "3-7", -1]
    with pytest.raises(ValueError):
        parse_pages(" , ")


def test_cli_refuses_shared_configs_and_outside_data(tmp_path):
    pdf = tmp_path / "a.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(pdf)
    with pytest.raises(SystemExit):
        main([str(pdf), "--config", str(EXAMPLE)])
    with pytest.raises(SystemExit):
        main([str(pdf), "--config", str(tmp_path / "c.yaml"), "--reports", str(tmp_path / "fuera")])
