# PID_Anonymouse

Herramienta Python que corre en local para anonimizar P&ID y PFD en PDF antes de
pasarlos a cualquier LLM. Elimina la información sensible (empresa, cajetín,
ubicación, autoría, metadatos, fabricantes, referencias documentales, logos) y
conserva los datos técnicos y de proceso (equipos, tags, líneas, tamaños,
condiciones de diseño, instrumentación, alarmas, interlocks y notas operativas).

La anonimización es **determinista** (reglas, regex y coordenadas). No se usa
ningún LLM para redactar. Toda redacción elimina el texto real con
`apply_redactions`; no basta con taparlo.

## Requisitos

- macOS (probado en Apple Silicon), Python 3.12
- [uv](https://docs.astral.sh/uv/) y Tesseract:

```bash
brew install uv tesseract
```

## Instalación y tests

```bash
uv sync
uv run pytest
```

## Diagnóstico de PDFs (fase 1)

Inventario de solo lectura: metadatos Info y XMP, IDs del documento, adjuntos,
anotaciones, capas (OCG), JavaScript, fuentes y tipo de cada página (texto
vectorial, solo imagen, imagen con capa de texto o solo trazos).

```bash
uv run inventory data/ --out data/reports --json
```

Sin `--out`, el informe sale por pantalla. El informe reproduce datos reales
(autor, títulos, nombres de capas), así que guárdalo solo dentro de `data/`:
si `--out` apunta fuera, el comando avisa.

## Uso (previsto, fase 8)

```bash
uv run sanitize data/plano.pdf --config configs/<cliente>.yaml
```

## Estructura

```
src/sanitizer/   motores: metadatos, texto, zonas, logos, verificación
configs/         reglas YAML por cliente o plantilla
tests/           tests con PDFs sintéticos generados con reportlab
data/            PDFs reales y mapas de sustitución (fuera de git)
```

## Datos

`data/` está en `.gitignore`. Nunca se commitean PDFs reales ni mapas de
sustitución. Los tests solo usan PDFs sintéticos.

## Roadmap

0. Entorno · 1. Diagnóstico · 2. Reglas · 3. Metadatos · 4. Redacción de texto ·
5. Zonas, logos y texto vectorial · 6. Mapa de sustitución · 7. Verificación y
auditoría · 8. CLI y UI · 9. LLM local (opcional)
