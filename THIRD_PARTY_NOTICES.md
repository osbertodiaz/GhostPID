# Third-party notices · Avisos de terceros

## English

GhostPID's own source code is released under the [MIT License](LICENSE).

GhostPID uses third-party components, each under its own license. Using
GhostPID from source (`uv sync`) installs them from PyPI under those
licenses.

**Downloadable app (Windows zip, macOS .app).** The desktop app bundles the
components listed below into one program. Two of them are copyleft:
**PyMuPDF/MuPDF (AGPL-3.0)** and **PyQt6 (GPL-3.0)**. As a combined work,
the bundled app is therefore distributed under the terms of the
**GNU AGPL-3.0**. Your rights under that license include receiving the
complete source code, which is at <https://github.com/osbertodiaz/GhostPID>.
That changes nothing for GhostPID's own code, which stays MIT and can be
reused on its own under MIT.

The full license texts of the bundled components are in the `licenses/`
folder next to the program (`THIRD_PARTY_LICENSES.txt`). They are also
available from the app under **About and licenses…**.

## Español

El código propio de GhostPID se publica con la [licencia MIT](LICENSE).

GhostPID usa componentes de terceros, cada uno con su propia licencia. Al
usar GhostPID desde el código (`uv sync`), se instalan desde PyPI con esas
licencias.

**App descargable (zip de Windows, .app de macOS).** La app de escritorio
reúne en un solo programa los componentes de la tabla. Dos son copyleft:
**PyMuPDF/MuPDF (AGPL-3.0)** y **PyQt6 (GPL-3.0)**. Por eso la app
empaquetada, como obra combinada, se distribuye bajo los términos de la
**GNU AGPL-3.0**. Esa licencia te da derecho, entre otras cosas, a recibir el
código fuente completo, que está en <https://github.com/osbertodiaz/GhostPID>.
El código propio de GhostPID sigue siendo MIT y puede reutilizarse por
separado con esa licencia.

Los textos completos de las licencias de los componentes incluidos están en
la carpeta `licenses/` junto al programa (`THIRD_PARTY_LICENSES.txt`).
También se pueden ver desde la app con **Acerca de y licencias…**.

## Components bundled in the desktop app · Componentes incluidos en la app

| Component | License | Project |
|---|---|---|
| Python | PSF-2.0 | <https://www.python.org/> |
| PyMuPDF (`pymupdf`), with MuPDF | AGPL-3.0 (or Artifex commercial) | <https://github.com/pymupdf/PyMuPDF> |
| PyQt6 (`pyqt6`) | GPL-3.0 (or Riverbank commercial) | <https://www.riverbankcomputing.com/software/pyqt/> |
| Qt 6 (`pyqt6-qt6`) | LGPL-3.0 | <https://www.qt.io/> |
| PyQt6-sip (`pyqt6-sip`) | BSD-2-Clause | <https://github.com/Python-SIP/sip> |
| pikepdf (`pikepdf`), with qpdf | MPL-2.0 (qpdf: Apache-2.0) | <https://github.com/pikepdf/pikepdf> |
| Tesseract OCR and its language data (Windows zip only) | Apache-2.0 | <https://github.com/tesseract-ocr/tesseract> |
| pytesseract (`pytesseract`) | Apache-2.0 | <https://github.com/madmaze/pytesseract> |
| NumPy (`numpy`) | BSD-3-Clause (and bundled permissive licenses) | <https://numpy.org/> |
| Pillow (`pillow`) | MIT-CMU | <https://python-pillow.org/> |
| lxml (`lxml`) | BSD-3-Clause | <https://lxml.de/> |
| pydantic (`pydantic`) | MIT | <https://github.com/pydantic/pydantic> |
| pydantic-core (`pydantic-core`) | MIT | <https://github.com/pydantic/pydantic-core> |
| annotated-types (`annotated-types`) | MIT | <https://github.com/annotated-types/annotated-types> |
| typing-inspection (`typing-inspection`) | MIT | <https://github.com/pydantic/typing-inspection> |
| typing-extensions (`typing-extensions`) | PSF-2.0 | <https://github.com/python/typing_extensions> |
| PyYAML (`pyyaml`) | MIT | <https://pyyaml.org/> |
| ruamel.yaml (`ruamel-yaml`) | MIT | <https://sourceforge.net/projects/ruamel-yaml/> |
| packaging (`packaging`) | Apache-2.0 or BSD-2-Clause | <https://github.com/pypa/packaging> |
| psutil (`psutil`) | BSD-3-Clause | <https://github.com/giampaolo/psutil> |
| charset-normalizer (`charset-normalizer`) | MIT | <https://github.com/jawah/charset_normalizer> |

The app is built with PyInstaller (GPL-2.0-or-later with an exception that
allows distributing the built program under any license).

## Used from source only · Solo al usar el código

These are installed by `uv sync` but are not part of the desktop app:
pdfplumber and pdfminer.six (MIT), pypdfium2 (Apache-2.0 / BSD-3-Clause),
OpenCV (`opencv-python`, Apache-2.0), cryptography (Apache-2.0 or
BSD-3-Clause), and dependencies that PyMuPDF installs but GhostPID does not
use (`pymupdf4llm`, `pymupdf-layout`, `onnxruntime`, `networkx`,
`tabulate`…); see each project for its license. Development tools (pytest,
pytest-qt, reportlab, PyInstaller) are not distributed.
