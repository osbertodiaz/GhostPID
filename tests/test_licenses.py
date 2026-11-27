"""Tests de las licencias: ficheros, distribuciones incluidas y diálogo «Acerca de»."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path  # noqa: E402

from sanitizer import __version__, licenses  # noqa: E402

ROOT = Path(__file__).parent.parent


def test_license_files_found_from_source():
    text = licenses.read_file(licenses.LICENSE_FILE)
    assert text is not None and text.startswith("MIT License")
    notices = licenses.read_file(licenses.NOTICES_FILE)
    assert notices is not None and "AGPL-3.0" in notices and "GPL-3.0" in notices


def test_package_metadata_declares_mit():
    from importlib.metadata import distribution

    assert distribution("ghostpid").metadata.get("License-Expression") == "MIT"


def test_runtime_distributions_follow_dependencies():
    names = {licenses.normalize(d.metadata["Name"]) for d in licenses.runtime_distributions()}
    assert {"pymupdf", "pyqt6", "pikepdf", "ruamel-yaml"} <= names
    assert "pytest" not in names and "ghostpid" not in names


def test_bundled_filters_by_module():
    dists = licenses.runtime_distributions()
    picked = {licenses.normalize(d.metadata["Name"]) for d in licenses.bundled(dists, {"pymupdf", "PyQt6"})}
    assert "pymupdf" in picked and "pyqt6" in picked
    assert "pikepdf" not in picked


def test_notices_cover_what_the_app_bundles():
    # Lo que la app incluye de verdad (comprobado con PyInstaller): si cambia,
    # build_app.py falla igual; esto avisa antes, sin construir.
    modules = {
        "pymupdf", "PyQt6", "pikepdf", "numpy", "PIL", "lxml", "pydantic", "pydantic_core",
        "annotated_types", "typing_inspection", "typing_extensions", "yaml", "ruamel",
        "pytesseract", "packaging", "psutil", "charset_normalizer",
    }  # fmt: skip
    dists = licenses.bundled(licenses.runtime_distributions(), modules)
    notices = (ROOT / licenses.NOTICES_FILE).read_text(encoding="utf-8")
    assert licenses.missing_from_notices(dists, notices) == []


def test_missing_from_notices_detects_gaps():
    dists = licenses.bundled(licenses.runtime_distributions(), {"pymupdf"})
    assert licenses.missing_from_notices(dists, "nothing here") == ["pymupdf"]


def test_write_license_texts(tmp_path):
    dists = licenses.bundled(licenses.runtime_distributions(), {"pymupdf", "PyQt6"})
    target = licenses.write_license_texts(dists, tmp_path / "licenses" / licenses.TEXTS_FILE)
    text = target.read_text(encoding="utf-8")
    assert f"GhostPID {__version__}" in text
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in text
    assert "GNU GENERAL PUBLIC LICENSE" in text
    assert "Python " in text


def test_about_dialog(qtbot):
    from sanitizer.about import AboutDialog

    dialog = AboutDialog()
    qtbot.addWidget(dialog)
    assert dialog.tabs.count() == 4
    assert __version__ in dialog.tabs.widget(0).toPlainText()
    assert "MIT License" in dialog.tabs.widget(1).toPlainText()
    assert "PyMuPDF" in dialog.tabs.widget(2).toPlainText()


def test_about_text_both_languages(monkeypatch):
    from sanitizer import i18n

    monkeypatch.setenv(i18n.ENV_VAR, "en")
    assert "MIT License" in licenses.about_text() and "AGPL-3.0" in licenses.about_text()
    monkeypatch.setenv(i18n.ENV_VAR, "es")
    assert "licencia MIT" in licenses.about_text()


def test_start_dialog_has_about_button(qtbot, tmp_path):
    from sanitizer.app import StartDialog

    (tmp_path / "configs").mkdir()
    dialog = StartDialog(tmp_path)
    qtbot.addWidget(dialog)
    assert dialog.about_btn.text().startswith("Acerca de")
