"""Tests del idioma de los textos: detección del sistema y ambos idiomas forzados."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from sanitizer import i18n  # noqa: E402
from sanitizer.audit import AuditResult, Finding, format_markdown  # noqa: E402
from sanitizer.blacklist import main as blacklist_main  # noqa: E402
from sanitizer.cli import main as sanitize_main  # noqa: E402
from sanitizer.inventory import format_markdown as inventory_markdown  # noqa: E402
from sanitizer.inventory import inspect_pdf  # noqa: E402
from sanitizer.rules import ConfigError, load_config  # noqa: E402
from sanitizer.rules import main as validate_main  # noqa: E402

CONFIGS = Path(__file__).parent.parent / "configs"
EXAMPLE = CONFIGS / "example.yaml"


@pytest.fixture
def english(monkeypatch):
    monkeypatch.setenv(i18n.ENV_VAR, "en")


@pytest.fixture
def spanish(monkeypatch):
    monkeypatch.setenv(i18n.ENV_VAR, "es")


# --------------------------------------------------------------------------
# Detección
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tag, expected",
    [
        ("es_ES.UTF-8", "es"),
        ("es-419", "es"),
        ("es_MX", "es"),
        ("ES", "es"),
        ("en_US.UTF-8", "en"),
        ("fr_FR", "en"),
        ("de", "en"),
        ("C", None),
        ("POSIX", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize(tag, expected):
    assert i18n._normalize(tag) == expected


def test_env_override_wins(monkeypatch):
    monkeypatch.setattr(i18n, "system_language", lambda: "es")
    monkeypatch.setenv(i18n.ENV_VAR, "en")
    assert i18n.current_language() == "en"
    assert i18n.tr("hola", "hello") == "hello"
    monkeypatch.setenv(i18n.ENV_VAR, "ES")
    assert i18n.current_language() == "es"
    assert i18n.tr("{n} páginas", "{n} pages", n=3) == "3 páginas"


def test_legacy_env_var_still_works(monkeypatch):
    monkeypatch.setattr(i18n, "system_language", lambda: "es")
    monkeypatch.delenv(i18n.ENV_VAR, raising=False)
    monkeypatch.setenv(i18n.LEGACY_ENV_VAR, "en")
    assert i18n.current_language() == "en"
    monkeypatch.setenv(i18n.ENV_VAR, "es")
    assert i18n.current_language() == "es"


def test_invalid_override_falls_back_to_system(monkeypatch):
    monkeypatch.setattr(i18n, "system_language", lambda: "en")
    monkeypatch.setenv(i18n.ENV_VAR, "fr")
    assert i18n.current_language() == "en"


def test_posix_variables(monkeypatch):
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(var, raising=False)
    assert i18n._env_language() is None
    monkeypatch.setenv("LANG", "es_ES.UTF-8")
    assert i18n._env_language() == "es"
    monkeypatch.setenv("LC_ALL", "en_GB.UTF-8")  # LC_ALL manda sobre LANG
    assert i18n._env_language() == "en"
    monkeypatch.setenv("LANGUAGE", "es:en")
    assert i18n._env_language() == "es"


def test_system_language_on_linux_uses_environment(monkeypatch):
    monkeypatch.setattr(i18n.sys, "platform", "linux")
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LANG", "es_AR.UTF-8")
    i18n.system_language.cache_clear()
    try:
        assert i18n.system_language() == "es"
        i18n.system_language.cache_clear()
        monkeypatch.setenv("LANG", "pt_BR.UTF-8")
        assert i18n.system_language() == "en"
    finally:
        i18n.system_language.cache_clear()


def test_system_language_prefers_the_os_ui_language(monkeypatch):
    monkeypatch.setattr(i18n.sys, "platform", "darwin")
    monkeypatch.setattr(i18n, "_macos_language", lambda: "es")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    i18n.system_language.cache_clear()
    try:
        assert i18n.system_language() == "es"
        i18n.system_language.cache_clear()
        monkeypatch.setattr(i18n.sys, "platform", "win32")
        monkeypatch.setattr(i18n, "_windows_language", lambda: "en")
        monkeypatch.setenv("LANG", "es_ES.UTF-8")
        assert i18n.system_language() == "en"
    finally:
        i18n.system_language.cache_clear()


def test_macos_preferences(monkeypatch, tmp_path):
    import plistlib

    prefs = tmp_path / "Library" / "Preferences"
    prefs.mkdir(parents=True)
    with (prefs / ".GlobalPreferences.plist").open("wb") as fh:
        plistlib.dump({"AppleLanguages": ["es-ES", "en-US"]}, fh, fmt=plistlib.FMT_BINARY)
    monkeypatch.setattr(i18n.Path, "home", lambda: tmp_path)
    assert i18n._macos_language() == "es"
    monkeypatch.setattr(i18n.Path, "home", lambda: tmp_path / "nada")
    assert i18n._macos_language() is None


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_validate_config_in_english(english, capsys):
    assert validate_main([str(EXAMPLE), "--check", "Initech Pumps"]) == 0
    out = capsys.readouterr().out
    assert "whitelist:" in out and "substitutions:" in out and "zones:" in out
    assert "replace" in out
    assert "lista blanca" not in out


def test_validate_config_in_spanish(spanish, capsys):
    assert validate_main([str(EXAMPLE), "--check", "Initech Pumps"]) == 0
    out = capsys.readouterr().out
    assert "lista blanca:" in out and "sustituciones:" in out and "sustituir" in out


@pytest.mark.parametrize("lang, expected", [("en", "shows which rules match TEXT"), ("es", "muestra qué reglas encajan")])
def test_help_follows_the_language(monkeypatch, capsys, lang, expected):
    monkeypatch.setenv(i18n.ENV_VAR, lang)
    with pytest.raises(SystemExit):
        validate_main(["--help"])
    assert expected in capsys.readouterr().out


def test_parser_errors_in_english(english, capsys, tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    with pytest.raises(SystemExit):
        sanitize_main([str(pdf), "--config", str(EXAMPLE), "--repair", "-1"])
    assert "--repair cannot be negative" in capsys.readouterr().err


@pytest.mark.parametrize("lang, expected", [("en", "entities"), ("es", "entidades")])
def test_blacklist_stats(monkeypatch, capsys, lang, expected):
    monkeypatch.setenv(i18n.ENV_VAR, lang)
    assert blacklist_main(["--config", str(CONFIGS / "base.yaml"), "stats"]) == 0
    out = capsys.readouterr().out
    assert expected in out
    # Las categorías del CSV no se traducen
    assert "epc" in out and "manufacturer" in out


@pytest.mark.parametrize(
    "lang, expected",
    [("en", "at least one term (terms) or pattern (patterns) is required"), ("es", "hace falta al menos un término")],
)
def test_config_errors_follow_the_language(monkeypatch, tmp_path, lang, expected):
    monkeypatch.setenv(i18n.ENV_VAR, lang)
    cfg = tmp_path / "c.yaml"
    cfg.write_text("version: 1\nname: T\nsubstitutions:\n  - id: empty\n    category: company\n    replacement: EPC\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(cfg)
    assert expected in str(exc.value)
    # Las claves YAML y los ids de regla no se traducen
    assert "substitutions[0:empty]" in str(exc.value)


def test_blacklist_term_problem_in_english(english):
    from sanitizer.blacklist import term_problem

    assert "too short" in term_problem("AB")
    assert "P&ID vocabulary" in term_problem("PUMP")


# --------------------------------------------------------------------------
# Informes
# --------------------------------------------------------------------------


def _audit_result() -> AuditResult:
    result = AuditResult("x.pdf")
    result.checks = {"texto": {"páginas": 2}, "lista negra": {"literales": 3}}
    result.findings = [Finding("error", "texto", "Initech", 1, "Initech")]
    result.review_lines = [{"texto": "Hooli", "páginas": [1], "origen": ["texto"]}]
    return result


def test_audit_markdown_in_english(english):
    md = format_markdown(_audit_result())
    assert "# Audit: x.pdf" in md and "FAILED (1)" in md
    assert "- text: pages 2" in md and "- blacklist: literals 3" in md
    assert "## Remaining text to review" in md and "| Hooli | 1 | text |" in md


def test_audit_markdown_in_spanish(spanish):
    md = format_markdown(_audit_result())
    assert "# Auditoría: x.pdf" in md and "FALLOS (1)" in md
    assert "- texto: páginas 2" in md and "## Texto restante para revisar" in md


def test_audit_json_keys_do_not_change(english):
    # El JSON es la interfaz estable con la UI y con repair: siempre en español
    result = _audit_result()
    assert set(result.checks) == {"texto", "lista negra"}
    assert result.findings[0].check == "texto"


@pytest.mark.parametrize("lang, expected", [("en", "## Summary"), ("es", "## Resumen")])
def test_inventory_markdown(monkeypatch, synthetic_pdf, lang, expected):
    monkeypatch.setenv(i18n.ENV_VAR, lang)
    md = inventory_markdown(inspect_pdf(synthetic_pdf))
    assert expected in md
    assert ("vector text" in md) == (lang == "en")


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------


def _client_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "cliente.yaml"
    cfg.write_text(f"version: 1\nname: T\nextends: {EXAMPLE}\ndisable: [side_strip, cover_logo]\n", encoding="utf-8")
    return cfg


def _window(qtbot, tmp_path: Path):
    import pymupdf

    from sanitizer.ui import ReviewWindow

    src = tmp_path / "plano.pdf"
    doc = pymupdf.open()
    doc.new_page(width=842, height=595).insert_text((50, 80), "PUMP P-101A by Initech Pumps", fontsize=11)
    doc.save(src)
    data = tmp_path / "data"
    win = ReviewWindow(src, _client_config(tmp_path), data / "out", data / "reports", data / "maps", synchronous=True)
    qtbot.addWidget(win)
    win.start()
    return win


def _texts(win) -> set[str]:
    texts = {a.text() for a in win.findChildren(type(win.view_toggle))}
    texts |= {win.table.horizontalHeaderItem(i).text() for i in range(win.table.columnCount())}
    texts |= {box.text() for box in win.kind_boxes.values()}
    texts |= {win.discard_btn.text(), win.term_btn.text(), win.report_btn.text(), win.page_only.text()}
    return texts


def test_review_window_in_english(english, qtbot, tmp_path):
    win = _window(qtbot, tmp_path)
    texts = _texts(win)
    assert {"Show anonymized", "Recalculate", "With OCR", "Confirm and generate", "Draw zone"} <= texts
    assert {"Type", "Page", "Text", "Detail", "Rule"} <= texts
    assert {"substitution", "leftover", "near miss", "review"} <= texts
    assert {"Discard", "Add term…", "Open audit report", "This page only"} <= texts
    assert win.windowTitle().startswith("Review · ")
    assert "Source: preview" in win.status.text()
    assert "Quick preview (no OCR)…" in win.log.toPlainText()
    # El código interno de los tipos no cambia
    assert any(d.kind == "sustitución" for d in win.state.detections)
    row = next(r for r, i in enumerate(win._rows) if win.state.detections[i].kind == "sustitución")
    assert win.table.item(row, 0).text() == "substitution"
    win.table.selectRow(row)
    assert win.discard_btn.toolTip().startswith("exception '")


def test_review_window_in_spanish(spanish, qtbot, tmp_path):
    win = _window(qtbot, tmp_path)
    texts = _texts(win)
    assert {"Ver anonimizado", "Recalcular", "Con OCR", "Confirmar y generar", "Dibujar zona"} <= texts
    assert {"Tipo", "Pág.", "Texto", "Detalle", "Regla"} <= texts
    assert {"sustitución", "resto", "rozada", "revisar"} <= texts
    assert win.windowTitle().startswith("Revisión · ")
    assert "Origen: vista previa" in win.status.text()


@pytest.mark.parametrize("lang, title, button", [("en", "open document", "New…"), ("es", "abrir documento", "Nueva…")])
def test_start_dialog(monkeypatch, qtbot, tmp_path, lang, title, button):
    from PyQt6.QtWidgets import QPushButton

    from sanitizer.app import StartDialog

    monkeypatch.setenv(i18n.ENV_VAR, lang)
    (tmp_path / "configs").mkdir()
    dialog = StartDialog(tmp_path)
    qtbot.addWidget(dialog)
    assert title in dialog.windowTitle()
    assert button in {b.text() for b in dialog.findChildren(QPushButton)}


@pytest.mark.parametrize("lang, cancel", [("en", "Cancel"), ("es", "Cancelar")])
def test_qt_standard_buttons_are_translated(monkeypatch, qtbot, lang, cancel):
    from PyQt6.QtCore import QCoreApplication
    from PyQt6.QtWidgets import QApplication

    from sanitizer.ui import install_translations

    monkeypatch.setenv(i18n.ENV_VAR, lang)
    app = QApplication.instance()
    previous = getattr(app, "_pid_translator", None)
    if previous is not None:
        app.removeTranslator(previous)
        app._pid_translator = None
    try:
        install_translations(app)
        assert QCoreApplication.translate("QPlatformTheme", "Cancel") == cancel
    finally:
        translator = getattr(app, "_pid_translator", None)
        if translator is not None:
            app.removeTranslator(translator)
            app._pid_translator = None
