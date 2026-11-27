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
- Diagnóstico: `uv run inventory data/ --out data/reports [--config configs/<cliente>.yaml]`
- Reglas: `uv run validate-config configs/<cliente>.yaml --check "texto"`

## Flexibilidad
- Los sets de `data/` son solo ejemplos: no sobreajustar a ellos. Todo umbral va en un dataclass de parámetros configurable desde el bloque `detection` del YAML.
- Las configs de cliente (`configs/*.yaml` salvo base y example) contienen nombres reales y están en .gitignore.
- Nunca copiar identificadores reales de `data/` a ficheros versionados (ejemplos, tests, docs).

## Idioma
Documentación y comentarios en español. Código en inglés.
