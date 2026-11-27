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
vectorial, solo imagen, imagen con capa de texto, solo trazos).

También detecta:

- **Texto convertido a trazos o contornos** (AutoCAD con SHX, PostScript): agrupa
  paths con tamaño de letra alineados. Las páginas con 50 glifos o más cuentan
  como "requiere OCR". Los umbrales se escalan con el tamaño de página.
- **Texto invisible**: capas OCR de escaneados y el texto que AutoCAD superpone
  a los trazos SHX.
- **Imágenes repetidas** (agrupadas por hash de contenido): candidatas a logo.
- **Títulos de marcadores**, que suelen contener nombres internos.

Los patrones encontrados en P&ID reales están en
[docs/corpus-pid.md](docs/corpus-pid.md).

```bash
uv run inventory data/ --out data/reports --json
```

Sin `--out`, el informe sale por pantalla. El informe reproduce datos reales
(autor, títulos, nombres de capas), así que guárdalo solo dentro de `data/`:
si `--out` apunta fuera, el comando avisa.

## Reglas por cliente (fase 2)

Cada cliente o plantilla tiene una config YAML en `configs/` que hereda de
[configs/base.yaml](configs/base.yaml) (patrones genéricos de P&ID). Parte de
[configs/example.yaml](configs/example.yaml), con nombres ficticios:

```bash
cp configs/example.yaml configs/<cliente>.yaml
uv run validate-config configs/<cliente>.yaml --check "texto de prueba"
```

Bloques:

- `whitelist`: lo que se conserva siempre (tags, líneas, tamaños, condiciones
  de diseño, alarmas, interlocks). Prioridad absoluta sobre las sustituciones.
  Por defecto un patrón solo encaja como token completo: `PID-001` no se
  protege dentro de `ACM-2041-PID-001`. `exceptions` quita protección a formas
  con aspecto de tag que no lo son (p. ej. un código de proyecto).
- `substitutions`: términos literales o regex y su denominación genérica
  (`EPC`, `OPR`, `VENDOR-{n}`, `DOC-REF-{n}`…), por categoría.
- `zones`: rectángulos que se eliminan enteros, en fracción de página o en mm,
  anclados a una esquina y filtrables por página y orientación.
- `detection`: umbrales de los detectores. Los valores por defecto se
  calibraron con un corpus de ejemplo y son solo un punto de partida.

Herencia con `extends`: las reglas se combinan por `id` (repetir un `id`
sobrescribe solo las claves indicadas), `disable: [id]` quita reglas heredadas.

`validate-config` comprueba el esquema y los ejemplos de cada regla, y detecta
solapes entre lista blanca y sustituciones (sale con código 1 si hay errores).
`--check` muestra qué haría cada regla con un texto. El inventario acepta
`--config` para usar los umbrales de detección del cliente.

**Las configs de cliente contienen los nombres que se quieren ocultar**: git
solo versiona `base.yaml` y `example.yaml`.

## Limpieza de metadatos (fase 3)

```bash
uv run clean-metadata data/plano.pdf -o data/out/plano.pdf [--config configs/<cliente>.yaml]
```

Elimina Info y XMP (también el XMP de imágenes y otros objetos), datos privados
de aplicación (`PieceInfo`, `OPI`), adjuntos, JavaScript y acciones, etiquetas
de página, árbol de estructura y miniaturas. Sustituye el `/ID` del documento
por un valor fijo y reescribe el fichero entero, así que desaparecen las
revisiones incrementales antiguas. Los destinos que apuntan a páginas fuera
del árbol de páginas ("fantasma") se descartan.

Por defecto (configurable en el bloque `metadata` del YAML):

- anotaciones: se conservan con su contenido, sin autor, fechas, IDs,
  respuestas, popups ni marcas de estado de revisión;
- enlaces: se eliminan (`keep_internal` conserva los que van a otra página);
- capas: `Capa-001`, `Capa-002`…, con su estado visible u oculto;
- marcadores: misma estructura con títulos `Página N`;
- formularios: se conservan, sin XFA, con nombres `Campo-001`… y sin tooltips.

No toca el contenido de las páginas (eso es la fase 4 y la 5). Al terminar
relee el PDF con pikepdf y falla si queda algo (`verify_metadata`).

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
