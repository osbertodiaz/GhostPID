"""Tests de la fase 4: redacción de texto con PDFs sintéticos.

Se usa la config de ejemplo (nombres ficticios: ACME, Globex, Initech…) más
alguna regla añadida en cada test.
"""

from pathlib import Path
from textwrap import dedent

import pikepdf
import pymupdf
import pytest

from sanitizer.redact import (
    DEFAULT_REDACTION,
    Namer,
    RedactionPolicy,
    find_matches,
    main,
    page_mode,
    redact_pdf,
    verify_redaction,
)
from sanitizer.rules import ConfigError, check_overlaps, load_config

CONFIGS = Path(__file__).parent.parent / "configs"
EXAMPLE = CONFIGS / "example.yaml"
A3 = (1190.55, 841.89)


def _config(tmp_path: Path, extra: str = ""):
    """Config de ejemplo con reglas añadidas para el test."""
    path = tmp_path / "cfg.yaml"
    path.write_text(f"version: 1\nname: T\nextends: {EXAMPLE}\n" + dedent(extra), encoding="utf-8")
    return load_config(path)


def _text(path: Path, all_layers: bool = True) -> str:
    doc = pymupdf.open(path)
    if all_layers and doc.get_ocgs():
        doc.set_layer(-1, on=list(doc.get_ocgs()), off=[])
        doc = pymupdf.open("pdf", doc.tobytes())
    with doc:
        return "\n".join(p.get_text() for p in doc)


def _streams(path: Path) -> bytes:
    """Todos los streams descomprimidos y objetos del PDF."""
    out = []
    with pikepdf.open(path) as pdf:
        for obj in pdf.objects:
            if isinstance(obj, pikepdf.Stream):
                try:
                    out.append(obj.read_bytes())
                except pikepdf.PdfError:
                    out.append(obj.read_raw_bytes())
            elif isinstance(obj, (pikepdf.Dictionary, pikepdf.Array)):
                out.append(obj.unparse(resolved=True))
    return b"\n".join(out)


def _title_block() -> pymupdf.Document:
    """Cajetín en un documento aparte: se coloca como Form XObject compartido."""
    tb = pymupdf.open()
    page = tb.new_page(width=300, height=100)
    page.insert_text((10, 30), "Prepared by ACME Engineering", fontsize=10)
    page.insert_text((10, 60), "Client Globex Petroleum", fontsize=10)
    return tb


@pytest.fixture
def pid(tmp_path: Path) -> Path:
    doc = pymupdf.open()
    tb = _title_block()

    # Página 1: P&ID con texto vectorial
    p = doc.new_page(width=A3[0], height=A3[1])
    p.insert_text((100, 100), "PUMP P-101A by Initech Pumps, 15 barg, FIC-1203", fontsize=10)
    p.insert_text((100, 130), 'LINE 6"-PL-1001-A1A to Globex tank', fontsize=10)
    p.insert_text((100, 160), "Drawing ACM-2041-PID-001 project PRJ-4711", fontsize=10)
    p.insert_text((100, 190), "Supplied by Umbrella Valves and ACME", fontsize=10)
    p.insert_text((100, 202), "Engineering for the plant", fontsize=10)  # término partido en dos líneas
    p.draw_line((90, 127), (500, 127))  # tubería que cruza la etiqueta de Globex
    p.insert_text((300, 300), "TI", fontsize=8)  # globo de instrumento: letras…
    p.insert_text((297, 312), "01234", fontsize=8)  # …y número de lazo debajo
    p.insert_text((100, 340), "PROJECT NUMBER 55555", fontsize=10)
    p.insert_text((30, 700), "drawn by John Doe at Springfield Refinery", fontsize=8, rotate=90)
    p.show_pdf_page(pymupdf.Rect(850, 700, 1150, 800), tb, 0)
    ocg = doc.add_ocg("revision notes", on=False)
    p.insert_text((100, 400), "Hidden note for Globex", fontsize=10, oc=ocg)
    annot = p.add_freetext_annot(pymupdf.Rect(600, 100, 800, 130), "Check with ACME", fontsize=10)
    annot.update()
    widget = pymupdf.Widget()
    widget.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    widget.field_name = "client"
    widget.field_value = "Globex"
    widget.rect = pymupdf.Rect(600, 150, 800, 170)
    p.add_widget(widget)

    # Página 2: rotada, con el mismo cajetín compartido
    p2 = doc.new_page(width=A3[0], height=A3[1])
    p2.insert_text((100, 100), "Initech Pumps P-102", fontsize=10)
    p2.show_pdf_page(pymupdf.Rect(850, 700, 1150, 800), tb, 0)
    p2.set_rotation(90)

    # Página 3: AutoCAD/SHX: trazos visibles + el mismo texto invisible encima
    p3 = doc.new_page(width=A3[0], height=A3[1])
    p3.insert_text((100, 100), "Initech Pumps supplies FIC-1203 and P-101A", fontsize=10, render_mode=3)
    for i in range(12):  # "letras" en trazos bajo la palabra Initech
        p3.draw_line((101 + i * 3, 92), (103 + i * 3, 99))
    p3.draw_line((50, 96), (700, 96))  # tubería larga que cruza el texto

    # Página 4: escaneado gris con capa OCR invisible
    p4 = doc.new_page(width=A3[0], height=A3[1])
    pix = pymupdf.Pixmap(pymupdf.csGRAY, pymupdf.IRect(0, 0, 400, 280), False)
    pix.set_rect(pix.irect, (128,))
    p4.insert_image(p4.rect, pixmap=pix)
    p4.insert_text((100, 100), "Client Globex Petroleum, pump P-101A", fontsize=10, render_mode=3)

    path = tmp_path / "pid.pdf"
    doc.save(path)
    return path


@pytest.fixture
def config(tmp_path):
    return _config(
        tmp_path,
        """
        substitutions:
          - id: project_number
            category: project
            replacement: PROJ-{n}
            patterns: ['\\d{5}']
            examples: ['55555']
        """,
    )


@pytest.fixture
def redacted(pid, config, tmp_path):
    out = tmp_path / "out.pdf"
    report = redact_pdf(pid, out, config)
    return out, report


# ---------------------------------------------------------------------------
# Coincidencias
# ---------------------------------------------------------------------------


def test_find_matches_prefers_longest_and_respects_whitelist(config):
    text = "ACME Engineering and Globex; pump P-101A"
    accepted, blocked = find_matches(config, text)
    assert [text[m.start : m.end] for m in accepted] == ["ACME Engineering", "Globex"]
    assert blocked == []


def test_find_matches_blocks_protected(tmp_path):
    cfg = _config(
        tmp_path,
        """
        substitutions:
          - id: pump_series
            category: other
            replacement: X
            terms: [PUMP P-101A]
        """,
    )
    accepted, blocked = find_matches(cfg, "PUMP P-101A by Initech Pumps")
    assert [b[3] for b in blocked] == ["equipment_tag"]
    assert all(m.rule.id != "pump_series" for m in accepted)


def test_namer_is_consistent_per_template():
    namer = Namer()
    assert namer.name("VENDOR-{n}", "Initech Pumps") == "VENDOR-1"
    assert namer.name("VENDOR-{n}", "initech  pumps") == "VENDOR-1"  # misma normalización
    assert namer.name("VENDOR-{n}", "Umbrella Valves") == "VENDOR-2"
    assert namer.name("EPC", "ACME") == "EPC"
    assert namer.mapping["VENDOR-1"] == ["Initech Pumps", "initech  pumps"]


# ---------------------------------------------------------------------------
# Documento completo
# ---------------------------------------------------------------------------

SENSITIVE = [
    "ACME",
    "Globex",
    "Initech",
    "Umbrella",
    "ACM-2041-PID-001",
    "PRJ-4711",
    "John Doe",
    "Springfield",
    "55555",
]
PROTECTED = ["P-101A", "FIC-1203", "15 barg", '6"-PL-1001-A1A', "TI", "01234", "P-102"]


def test_sensitive_text_is_gone_everywhere(redacted):
    out, report = redacted
    text = _text(out)
    for value in SENSITIVE:
        assert value not in text, value
    blob = _streams(out)
    for value in ("ACME", "Globex", "Initech", "Umbrella", "Springfield", "55555"):
        assert value.encode() not in blob, value  # eliminado, no tapado
    assert report.problems == []


def test_technical_data_is_kept(redacted):
    text = _text(redacted[0])
    for value in PROTECTED:
        assert value in text, value
    assert "Engineering for the plant" not in text  # el término partido se sustituyó entero
    assert "for the plant" in text


def test_generic_names_are_written(redacted):
    out, report = redacted
    text = _text(out)
    for generic in ("EPC", "OPR", "VENDOR-1", "VENDOR-2", "DOC-REF-1", "PRJ-1", "SITE", "PROJ-1"):
        assert generic in text, generic
    assert report.mapping["VENDOR-1"] == ["Initech Pumps"]
    assert report.mapping["VENDOR-2"] == ["Umbrella Valves"]


def test_stacked_loop_number_is_protected(redacted):
    out, report = redacted
    assert report.stacked_tags == 1
    assert "01234" in _text(out)
    assert "PROJ-1" in _text(out) and "55555" not in _text(out)


def test_numeric_rule_triggers_warning(config):
    warnings = [i.message for i in check_overlaps(config) if i.severity == "warning"]
    assert any("números sueltos" in w and "project_number" in w for w in warnings)


def test_pipe_crossing_label_is_kept(pid, redacted):
    out, _ = redacted
    with pymupdf.open(pid) as before, pymupdf.open(out) as after:
        lines = lambda page: [d for d in page.get_drawings() if d["rect"].width > 300]  # noqa: E731
        assert len(lines(after[0])) == len(lines(before[0])) == 1


def test_shared_title_block_replaced_on_every_page(redacted):
    out, _ = redacted
    with pymupdf.open(out) as doc:
        for number in (0, 1):
            text = doc[number].get_text()
            assert "Prepared by" in text and "EPC" in text and "OPR" in text, number
            assert "ACME" not in text and "Globex" not in text


def test_rotated_page_and_vertical_text(redacted):
    out, _ = redacted
    with pymupdf.open(out) as doc:
        assert "VENDOR-1" in doc[1].get_text()
        vertical = [
            line
            for block in doc[0].get_text("dict")["blocks"]
            for line in block.get("lines", [])
            if abs(line["dir"][0]) < 0.1
        ]
        joined = " ".join(s["text"] for line in vertical for s in line["spans"])
        assert "SITE" in joined and "drawn by" in joined


def test_hidden_layer_text_redacted_and_state_restored(redacted):
    out, _ = redacted
    everything = _text(out, all_layers=True)
    assert "Hidden note for" in everything and "Globex" not in everything
    with pymupdf.open(out) as doc:
        names = {x: o["name"] for x, o in doc.get_ocgs().items()}
        off = {names[x] for x in doc.get_layer(-1)["off"]}
        assert off == {"revision notes", "redacted-hidden"}  # la capa sigue oculta
        visible = doc[0].get_text()
        assert "Hidden note" not in visible
    # La sustitución del texto oculto también está oculta: con todas las capas
    # se ve un "OPR" más que con las capas en su estado guardado
    assert _text(out, all_layers=True).count("OPR") == _text(out, all_layers=False).count("OPR") + 1


def test_annotation_and_form_values(redacted):
    out, report = redacted
    with pymupdf.open(out) as doc:
        page = doc[0]
        assert [a.info["content"] for a in page.annots()] == ["Check with EPC"]
        assert [w.field_value for w in page.widgets()] == ["OPR"]
    sources = {r.source for r in report.replacements}
    assert sources == {"page", "annotation", "form"}


def test_page_modes(pid):
    with pymupdf.open(pid) as doc:
        modes = [page_mode(p, load_config(EXAMPLE).detection) for p in doc]
    assert modes == ["text", "text", "overlay", "scanned"]


def test_overlay_page_removes_covered_strokes_only(pid, redacted):
    out, _ = redacted
    with pymupdf.open(pid) as before, pymupdf.open(out) as after:
        small = lambda page: [d for d in page.get_drawings() if d["rect"].width < 10]  # noqa: E731
        long = lambda page: [d for d in page.get_drawings() if d["rect"].width > 300]  # noqa: E731
        assert len(small(before[2])) == 12
        assert len(small(after[2])) < 12  # las letras en trazos bajo "Initech" se van
        assert len(long(after[2])) == 1  # la tubería que cruza se queda
        assert "VENDOR-1" in after[2].get_text()


def test_no_collateral_line_art_removed(redacted):
    assert redacted[1].warnings == []


def test_scanned_page_blanks_pixels_under_match(pid, redacted):
    out, _ = redacted
    with pymupdf.open(pid) as before, pymupdf.open(out) as after:
        word = next(w for w in before[3].get_text("words") if w[4] == "Globex")
        # Extremo derecho de la palabra: "OPR" es más corto y no llega ahí
        centre = (word[2] - 2, (word[1] + word[3]) / 2)
        clip = pymupdf.Rect(centre, centre) + (-1, -1, 1, 1)
        a = before[3].get_pixmap(clip=clip, dpi=72).pixel(0, 0)
        b = after[3].get_pixmap(clip=clip, dpi=72).pixel(0, 0)
        assert a[0] == 128 and b[0] > 200  # gris → blanco
        assert "P-101A" in after[3].get_text()


def test_output_never_overwrites_source(pid, config):
    with pytest.raises(ValueError, match="sobrescribir"):
        redact_pdf(pid, pid, config)


def test_verification_detects_leftovers(pid, config, tmp_path):
    out = tmp_path / "noannots.pdf"
    report = redact_pdf(pid, out, config, policy=RedactionPolicy(annotations=False))
    assert any("anotación" in p and "ACME" in p for p in report.problems)
    assert any("formulario" in p for p in report.problems)
    assert verify_redaction(out, config) == report.problems


def test_black_fill_policy(pid, tmp_path):
    cfg = _config(tmp_path, "redaction:\n  fill: black\n")
    out = tmp_path / "black.pdf"
    report = redact_pdf(pid, out, cfg)
    assert report.problems == []
    assert cfg.redaction.fill == "black" and cfg.redaction.overlay.graphics == "covered"


def test_redaction_block_validation(tmp_path):
    cfg = _config(tmp_path, "redaction:\n  scanned: {graphics: none, images: remove}\n")
    assert cfg.redaction.scanned.images == "remove"
    assert cfg.redaction.text == DEFAULT_REDACTION.text
    with pytest.raises(ConfigError, match="images"):
        _config(tmp_path, "redaction:\n  text: {graphics: none}\n")
    with pytest.raises(ConfigError, match="fill"):
        _config(tmp_path, "redaction:\n  fill: pink\n")


def test_replacement_loop_is_an_error(tmp_path):
    cfg = _config(
        tmp_path,
        """
        substitutions:
          - id: loop
            category: other
            replacement: ACME-{n}
            terms: [Foo Corp]
        """,
    )
    errors = [i.message for i in check_overlaps(cfg) if i.severity == "error"]
    assert any("vuelve a encajar" in e for e in errors)


def test_cli(pid, tmp_path, capsys):
    out = tmp_path / "cli.pdf"
    report = tmp_path / "data" / "r.json"
    assert main([str(pid), "-o", str(out), "--config", str(EXAMPLE), "--report", str(report)]) == 0
    printed = capsys.readouterr().out
    assert "verificación (relectura del texto): OK" in printed
    assert '"original": "Initech Pumps"' in report.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Casos encontrados con planos reales
# ---------------------------------------------------------------------------


def _single_page(tmp_path, draw, name="case.pdf") -> Path:
    doc = pymupdf.open()
    page = doc.new_page(width=A3[0], height=A3[1])
    draw(page)
    path = tmp_path / name
    doc.save(path)
    return path


def test_glued_ocr_words_on_scanned_pages(tmp_path, config):
    def draw(page):
        pix = pymupdf.Pixmap(pymupdf.csGRAY, pymupdf.IRect(0, 0, 200, 140), False)
        pix.set_rect(pix.irect, (200,))
        page.insert_image(page.rect, pixmap=pix)
        page.insert_text((100, 100), "GLOBEXPETROLEUMCORP pump P-101A", fontsize=10, render_mode=3)

    out = tmp_path / "ocr.pdf"
    report = redact_pdf(_single_page(tmp_path, draw), out, config)
    assert report.page_modes == {1: "scanned"}
    assert "GLOBEX" not in _text(out).upper()
    assert "P-101A" in _text(out)
    assert report.problems == []


def test_glued_words_untouched_on_text_pages(tmp_path, config):
    out = tmp_path / "plain.pdf"
    path = _single_page(tmp_path, lambda p: p.insert_text((100, 100), "ACMEX valve and ACME", fontsize=10))
    report = redact_pdf(path, out, config)
    text = _text(out)
    assert "ACMEX" in text and "ACME valve" not in text
    # …pero se informa como coincidencia rozada para que se revise la regla
    assert any("ACMEX" in n.text for n in report.near_misses)


def test_overlapping_texts_keep_protected_neighbour(tmp_path, config):
    def draw(page):
        # Tag vertical cuya caja se solapa con la de una nota, como en un plano
        # real (las cajas se pisan unos 6 pt, los cuerpos de letra no)
        page.insert_text((818, 827), "ACM-2041-PID-064", fontsize=10, rotate=90)
        page.insert_text((826, 821), "NOTE 4", fontsize=10, rotate=90)
        # Interlineado muy apretado
        page.insert_text((100, 100), "Globex Petroleum", fontsize=10)
        page.insert_text((100, 108), "P-101A FIC-1203", fontsize=10)

    out = tmp_path / "overlap.pdf"
    report = redact_pdf(_single_page(tmp_path, draw), out, config)
    text = _text(out)
    assert "NOTE 4" in text and "P-101A" in text and "FIC-1203" in text
    assert "ACM-2041" not in text and "Globex" not in text
    assert report.warnings == []


def test_pattern_near_miss_reported(tmp_path, config):
    path = _single_page(tmp_path, lambda p: p.insert_text((100, 100), "see ACM-2041-PID-064/065", fontsize=10))
    report = redact_pdf(path, tmp_path / "nm.pdf", config)
    assert [n.rule for n in report.near_misses] == ["document_refs"]


def test_substitution_exceptions_are_not_replaced(tmp_path):
    from sanitizer.redact import find_matches, near_misses

    cfg = _config(
        tmp_path,
        """
        substitutions:
          - id: project_code
            exceptions: [PRJ-0000]
        """,
    )
    text = "Ref PRJ-4711 and PRJ-0000"
    accepted, _ = find_matches(cfg, text)
    assert [text[m.start : m.end] for m in accepted] == ["PRJ-4711"]
    assert not near_misses(cfg, "xPRJ-0000x", [])
