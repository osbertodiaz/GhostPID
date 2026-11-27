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
- CLI completa: `uv run sanitize <pdf|carpeta>... --config configs/<cliente>.yaml [--map data/maps/<proyecto>.map.json] [--repair N]`
- UI de revisión (PyQt6): `uv run review <pdf> --config configs/<cliente>.yaml`; edita la config con ruamel (copia .bak). Tests de UI con pytest-qt y `QT_QPA_PLATFORM=offscreen`
- Diagnóstico: `uv run inventory data/ --out data/reports [--config configs/<cliente>.yaml]`
- Reglas: `uv run validate-config configs/<cliente>.yaml --check "texto"`
- Lista negra de la industria: `configs/blacklist/industria.csv` (pública, versionada; propia.csv ignorado). `uv run blacklist stats|find|check|add`. Términos solo palabra completa; vocabulario de P&ID rechazado (PID_VOCABULARY en blacklist.py)
- Metadatos: `uv run clean-metadata <pdf> -o <salida> [--config …]`
- Redacción: `uv run redact <pdf> -o <salida> --config … [--report data/reports/x.json]`
- Fases 4+5 (zonas, logos, texto, OCR): `uv run anonymize <pdf> -o <salida> --config … [--report …]`
- OCR necesita `brew install tesseract tesseract-lang`
- Mapa: `anonymize … --map data/maps/<proyecto>.map.json`; reversión: `uv run revert --map … [fichero]`
- Los mapas contienen valores originales: solo en data/ (save_map lo impone)
- Auditoría: `uv run audit <final.pdf> --config … --out data/reports/<doc> [--original … --anon-report … --map …]`
- pikepdf: para leer el contenido de un objeto indirecto usar `unparse(resolved=True)`; `unparse()` solo da la referencia

## Flexibilidad
- Los sets de `data/` son solo ejemplos: no sobreajustar a ellos. Todo umbral va en un dataclass de parámetros configurable desde el bloque `detection` del YAML.
- Las configs de cliente (`configs/*.yaml` salvo base y example) contienen nombres reales y están en .gitignore.
- Nunca copiar identificadores reales de `data/` a ficheros versionados (ejemplos, tests, docs).

## Idioma
Documentación y comentarios en español. Código en inglés.
