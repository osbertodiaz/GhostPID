# PID_Anonymouse

Herramienta Python local para anonimizar P&ID y PFD en PDF antes de usarlos con LLMs.

## Reglas no negociables
- Nunca commitear PDFs reales, mapas de sustitución ni nada de `data/`. Está en .gitignore.
- Los tests usan solo PDFs sintéticos generados con reportlab.
- La anonimización es determinista (reglas, regex, coordenadas). Nunca usar un LLM para redactar.
- Preservar siempre: tags de equipos, instrumentos, líneas, tamaños, condiciones de diseño, alarmas, interlocks y notas operativas.
- Toda redacción debe eliminar el texto real (apply_redactions), no solo taparlo.
- Cada cambio pasa por tests antes de darlo por hecho.

## Stack
Python 3.12, uv, PyMuPDF, pikepdf, pdfplumber, OpenCV, pytesseract, pyyaml, pytest, reportlab.

## Comandos
- Tests: `uv run pytest`
- CLI: `uv run sanitize <pdf> --config configs/<cliente>.yaml`

## Idioma
Documentación y comentarios en español. Código en inglés.
