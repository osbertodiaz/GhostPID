"""Fixtures compartidas: PDFs sintéticos generados con reportlab."""

from pathlib import Path

import pytest
from reportlab.lib.pagesizes import A3, landscape
from reportlab.pdfgen import canvas

# Texto mínimo con aspecto de P&ID: tags que deben conservarse
SMOKE_LINES = [
    "P-101A CENTRIFUGAL PUMP",
    "FIC-1203 HH ALARM",
    '6"-PL-1001-A1A',
    "DESIGN 15 barg @ 120 C",
]


@pytest.fixture
def synthetic_pdf(tmp_path: Path) -> Path:
    """Genera un PDF sintético de una página A3 apaisada con texto de proceso."""
    path = tmp_path / "synthetic_pid.pdf"
    c = canvas.Canvas(str(path), pagesize=landscape(A3))
    c.setTitle("Synthetic P&ID")
    c.setAuthor("Test Author")
    y = 700
    for line in SMOKE_LINES:
        c.drawString(100, y, line)
        y -= 30
    c.rect(900, 50, 250, 120)  # cajetín ficticio
    c.drawString(910, 150, "ACME ENGINEERING")
    c.showPage()
    c.save()
    return path
