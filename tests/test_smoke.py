"""Test de humo de la fase 0: el entorno genera y lee PDFs."""

import pymupdf

from sanitizer import __version__
from tests.conftest import SMOKE_LINES


def test_package_imports():
    assert __version__


def test_synthetic_pdf_opens_with_pymupdf(synthetic_pdf):
    with pymupdf.open(synthetic_pdf) as doc:
        assert doc.page_count == 1
        assert doc.metadata["author"] == "Test Author"
        text = doc[0].get_text()
    for line in SMOKE_LINES:
        assert line in text
    assert "ACME ENGINEERING" in text


def test_stack_dependencies_import():
    import cv2  # noqa: F401
    import pdfplumber  # noqa: F401
    import pikepdf  # noqa: F401
    import pytesseract  # noqa: F401
    import yaml  # noqa: F401


def test_tesseract_binary_available():
    import pytesseract

    assert pytesseract.get_tesseract_version().major >= 5
