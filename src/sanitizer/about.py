"""Diálogo «Acerca de y licencias…» (lanzador y ventana de revisión)."""

from __future__ import annotations

from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QPlainTextEdit, QTabWidget, QTextBrowser, QVBoxLayout, QWidget

from sanitizer import licenses
from sanitizer.i18n import tr


def _plain(text: str) -> QPlainTextEdit:
    box = QPlainTextEdit(text)
    box.setReadOnly(True)
    return box


class AboutDialog(QDialog):
    """Versión, licencia propia (MIT) y licencias de terceros, en pestañas."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Acerca de {app} y licencias", "About {app} and licenses", app=licenses.APP_NAME))
        self.resize(760, 620)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        summary = QTextBrowser()
        summary.setOpenExternalLinks(True)
        summary.setPlainText(licenses.about_text())
        self.tabs.addTab(summary, licenses.APP_NAME)

        own = licenses.read_file(licenses.LICENSE_FILE)
        self.tabs.addTab(_plain(own or tr("No se encontró LICENSE.", "LICENSE not found.")), tr("Licencia (MIT)", "License (MIT)"))

        notices = QTextBrowser()
        notices.setOpenExternalLinks(True)
        text = licenses.read_file(licenses.NOTICES_FILE)
        if text:
            notices.setMarkdown(text)
        else:
            notices.setPlainText(tr("No se encontró {f}.", "{f} not found.", f=licenses.NOTICES_FILE))
        self.tabs.addTab(notices, tr("Terceros", "Third parties"))

        full = licenses.read_file(licenses.TEXTS_FILE)
        self.tabs.addTab(
            _plain(
                full
                or tr(
                    "Los textos completos se generan al construir la app ({f}). "
                    "Desde el código, cada paquete trae su licencia en sus metadatos.",
                    "The full texts are generated when the app is built ({f}). "
                    "From source, each package carries its license in its metadata.",
                    f=licenses.TEXTS_FILE,
                )
            ),
            tr("Textos completos", "Full texts"),
        )

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
