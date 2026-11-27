# GhostPID

Herramienta Python que corre en local para anonimizar P&ID y PFD en PDF antes de
pasarlos a cualquier LLM. Elimina la información sensible (empresa, cajetín,
ubicación, autoría, metadatos, fabricantes, referencias documentales, logos) y
conserva los datos técnicos y de proceso (equipos, tags, líneas, tamaños,
condiciones de diseño, instrumentación, alarmas, interlocks y notas operativas).

La anonimización es **determinista** (reglas, regex y coordenadas). No se usa
ningún LLM para redactar. Toda redacción elimina el texto real con
`apply_redactions`; no basta con taparlo.

> **Manual de uso** (instalación, app de revisión, reglas y problemas frecuentes): [docs/MANUAL.md](docs/MANUAL.md) · English: [docs/MANUAL.en.md](docs/MANUAL.en.md)

## Idioma

La UI de revisión, la app de escritorio, la salida y los `--help` de los
comandos, los mensajes de error de la config y los informes Markdown salen en
**español si el sistema operativo está en español** (cualquier `es_*`) y en
**inglés en cualquier otro caso**. Se lee el idioma de la interfaz del sistema
(`AppleLanguages` en macOS, `GetUserDefaultUILanguage` en Windows) y, si no
hay, las variables `LANGUAGE`, `LC_ALL`, `LC_MESSAGES` y `LANG`. Para forzarlo:

```bash
GHOSTPID_LANG=en uv run validate-config configs/example.yaml
```

No se traducen las claves YAML, los ids de reglas, las categorías
(`company`, `manufacturer`…), las columnas del CSV de la lista negra, las
denominaciones genéricas (`EPC-n`, `VENDOR-n`…), las claves del JSON de los
informes ni los nombres de fichero (`auditoria.md`). El mecanismo está en
[src/sanitizer/i18n.py](src/sanitizer/i18n.py): cada texto se escribe en los
dos idiomas allí donde se usa, `tr("texto", "text")`, sin catálogos que
compilar ni empaquetar. Los tests fuerzan español por
defecto (`tests/conftest.py`).

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
  (`EPC`, `OPR`, `VENDOR-{n}`, `DOC-REF-{n}`…), por categoría. Sus
  `exceptions` son textos literales que encajan con la regla pero se
  conservan (falsos positivos descartados al revisar): se comparan enteros,
  con espacios flexibles, y tampoco cuentan como restos en la auditoría.
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

### Lista negra de la industria

Además de las reglas del cliente, el software ya sabe qué buscar:
`configs/blacklist/industria.csv` es una base de datos pública con más de mil
empresas que aparecen en P&ID y PFD de petróleo y gas, química, gases
industriales, farmacéutica y energía:

| Categoría (`category`) | Qué incluye | Sustitución |
|---|---|---|
| `epc` | ingenierías y contratistas EPC/EPCM | `EPC-{n}` |
| `licensor` | licenciantes de tecnología | `LICENSOR-{n}` |
| `manufacturer` | fabricantes de equipos, válvulas, instrumentos y marcas | `VENDOR-{n}` |
| `operator` | operadores, propietarios y gases industriales | `OPR-{n}` |

`base.yaml` la carga con el bloque `blacklist` y la convierte en las
sustituciones `kb-epc`, `kb-licensor`, `kb-manufacturer` y `kb-operator`.
Columnas del CSV: `name,aliases,category,segment,industries,country,active,notes`
(separador `,` o `;`, UTF-8; se edita bien en Excel). Los alias van separados
por `|` y comparten denominación con el nombre principal: "Endress+Hauser" y
"E+H" son el mismo `VENDOR-n`. Las marcas de un grupo (Fisher, Rosemount) van
en filas propias para que `revert` devuelva el nombre correcto.

Salvaguardas, porque un nombre de empresa puede ser vocabulario de P&ID:

- solo palabras completas, también en OCR y en la auditoría ("Linde" no
  encaja en "CYLINDER");
- siglas en mayúsculas de hasta 5 caracteres distinguen mayúsculas;
- se rechazan términos de menos de 3 caracteres, los formados solo por
  vocabulario de P&ID ("Shell", "Crane", "Total", "Weir", "Gate Valve") y los
  duplicados entre entidades; el error indica fichero y línea.

```bash
uv run blacklist stats                       # resumen por categoría e industria
uv run blacklist find valve                  # buscar por nombre, alias o segmento
uv run blacklist check "FISHER 667 / SHELL SIDE"
uv run blacklist add "Nombre SA" --category fabricante --aliases "NSA|Nombre" --industries pharma
```

`add` (y **Añadir término…** en la UI con una regla `kb-…`) escribe en
`configs/blacklist/propia.csv`, que git ignora: ahí van los nombres privados
(clientes, plantas). Los nombres públicos de la industria pueden añadirse a
`industria.csv` y versionarse. Todos los CSV de `configs/blacklist/` se
consolidan al cargar la config. En la config del cliente:

```yaml
blacklist:
  industries: [pharma]   # solo filas de estas industrias (y las generales)
  exclude: [Vega]        # términos que no se buscan en este set
  enabled: false         # desactivarla
substitutions:
  - id: kb-manufacturer  # excepciones de la lista (falsos positivos)
    exceptions: [Samson]
```

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

## Redacción de texto (fase 4)

```bash
uv run redact data/plano.pdf -o data/out/plano.pdf --config configs/<cliente>.yaml \
    --report data/reports/plano.redact.json
```

- Las sustituciones se buscan sobre el texto de cada bloque, así que encajan
  aunque un término esté partido en dos líneas. Todo lo que toque la lista
  blanca se bloquea y se informa.
- Los **tags apilados** de los globos de instrumento (`TI` sobre `01234`) se
  reconocen por su geometría y quedan protegidos.
- `apply_redactions` elimina el texto real. Se usa una franja estrecha en el
  centro de cada carácter para no arrastrar líneas vecinas ni textos que se
  solapan. En su sitio se escribe la denominación genérica (`EPC`, `VENDOR-1`…),
  con la orientación original, también en páginas rotadas y texto vertical.
- Según el tipo de página (bloque `redaction` del YAML): en texto normal solo
  se quita el texto; en páginas AutoCAD/SHX (texto invisible sobre trazos)
  también los trazos que dibujan esas letras, editando el contenido con
  precisión (`lineart.py`); en escaneados con OCR, los píxeles bajo el texto.
- El texto de las **capas ocultas** también se procesa (su sustitución queda
  en una capa oculta), igual que el contenido de **anotaciones** y los valores
  de **formularios**.
- En páginas escaneadas los términos se buscan también dentro de palabras
  pegadas por el OCR (`ocr_relaxed_terms`).
- La numeración `{n}` es coherente en todo el documento.

Al terminar se relee el PDF: ninguna coincidencia debe seguir ahí y ningún dato
protegido debe haber desaparecido. El informe lista además las **coincidencias
rozadas**: lo que una regla encontraría sin límites de palabra o de token
(p. ej. `REF-001/002`), para afinar las reglas del cliente.

El texto convertido a trazos sin capa de texto, los logos y las imágenes son
cosa de la fase 5.

## Zonas, logos y texto en trazos (fase 5)

```bash
uv run anonymize data/plano.pdf -o data/out/plano.pdf --config configs/<cliente>.yaml \
    --report data/reports/plano.anon.json
```

Encadena, en este orden: zonas → logos → redacción de texto (fase 4) → OCR.

- **Zonas** (bloque `zones`): se elimina todo lo que contienen, es decir
  texto, píxeles de imagen y trazos que estén enteros dentro. Una línea que solo
  entra en la zona, como el marco del plano, se conserva. `label` escribe una
  etiqueta opcional en la zona vaciada. Los **logos vectoriales** se tratan
  con zonas.
- **Logos en imagen** (bloque `logos`): las imágenes se agrupan por un hash
  perceptual, así que la misma imagen girada o en gris cuenta como una sola. Se
  eliminan las que se repiten en 3 páginas o más y ocupan menos del 15 % de la
  página, además de las fijadas en `logos.remove`. `logos.keep` excluye
  imágenes. El informe da el hash de cada grupo.
- **OCR** (bloque `ocr`): se aplica a las páginas con texto en trazos y a las
  escaneadas, incluidas las que ya traen una capa OCR. Tesseract lee en 0°, 90°
  y 270° (`eng+spa` por defecto; hace falta `brew install tesseract-lang`).
  Las reglas se aplican con la lista blanca primero y los términos toleran
  confusiones típicas del OCR (`0/O`, `1/I/l`, `5/S`, `8/B`). Cada
  coincidencia se borra (trazos con el editor preciso, píxeles en
  escaneados) y en su lugar se escribe la denominación genérica.

## Mapa de sustitución y reversión (fase 6)

```bash
uv run anonymize data/plano.pdf -o data/out/plano.pdf --config configs/<cliente>.yaml \
    --map data/maps/<proyecto>.map.json
uv run revert --map data/maps/<proyecto>.map.json respuesta_llm.txt > respuesta.txt
```

- El mapa guarda, por cada denominación genérica, los originales que
  sustituye, junto con su regla, su categoría y cuántas veces aparecieron (y
  cuántas se leyeron del texto real y no por OCR). Incluye además el SHA-256 de
  cada PDF original y anonimizado.
- **Solo se guarda dentro de `data/`**, con permisos 600: el código se niega a
  escribirlo en otro sitio, y lo comprueba antes de empezar a trabajar.
- Si se pasa el mismo `--map` al anonimizar otro PDF, la numeración se
  reutiliza: `VENDOR-1` es el mismo fabricante en todo el proyecto.
- `revert` cambia cada denominación por su original: de la más larga a la más
  corta, sin distinguir mayúsculas y solo palabras completas. Si una
  denominación agrupa varias entidades distintas (p. ej. `SITE` para varias
  ubicaciones), usa la más frecuente leída del texto real y avisa.
  `validate-config` advierte de esas reglas: para que la reversión sea exacta,
  usa `{n}` en `replacement`.
- Denominaciones como `EPC` también son siglas comunes: si un LLM las usa con
  ese sentido, `revert` las cambiará igualmente. Para evitarlo, usa algo
  inequívoco en la config (p. ej. `[EPC]`).

## Verificación y auditoría (fase 7)

```bash
uv run audit data/out/plano.final.pdf --config configs/<cliente>.yaml --out data/reports/plano \
    --original data/plano.pdf --anon-report data/reports/plano.anon.json --map data/maps/<proyecto>.map.json
```

Comprueba el PDF final con independencia de cómo se produjo y termina con
código 1 si encuentra restos:

- **Lista negra**: términos y patrones de la config y los valores originales
  del mapa (los que se encontraron de verdad). Se buscan en el texto de todas
  las capas, en anotaciones y formularios, en **todos los objetos** del PDF
  (streams descomprimidos, salvo imágenes) y en el **OCR de todas las páginas**
  (`audit.ocr_pages`), que es lo que ve un LLM con visión.
- **Metadatos**: se releen con pikepdf.
- **Revisión visual**: un PNG por página con las áreas tocadas resaltadas por
  colores (sustitución, OCR, zona, logo y restos encontrados).
- **Texto restante para revisar**: líneas en la franja del cajetín
  (`audit.review_areas`) que no son datos protegidos ni denominaciones
  genéricas, para detectar nombres que las reglas no conocen.
- Informe `auditoria.md` y `auditoria.json` con qué se eliminó, cuántas veces y
  en qué páginas. Como contiene valores originales, **solo se guarda en
  `data/`**.

## Uso: CLI completa (fase 8)

```bash
uv run sanitize data/proyecto/ --config configs/<cliente>.yaml [--map data/maps/<proyecto>.map.json]
```

Por cada PDF (se admiten ficheros y carpetas, que se recorren recursivamente):

1. `anonymize`: zonas, logos, texto y OCR, con mapa de sustitución.
2. `clean-metadata`.
3. `audit`.
4. **Autocorrección** (`--repair N`, 1 por defecto; 0 la desactiva): los
   restos que la auditoría encuentra **por OCR** se corrigen y se reaudita,
   con OCR solo en las páginas corregidas. No se tocan restos en la capa de
   texto ni lo que hay sobre texto real: eso indica una regla incompleta y
   se informa.

Salidas:

- `data/out/<doc>.sanitized.pdf`: el PDF que se puede compartir.
- `data/reports/<doc>/`: `anonimizado.json`, `metadatos.json`,
  `auditoria.md` y `auditoria.json`, y los PNG de revisión.
- `data/maps/<doc>.map.json`, o el mapa común de `--map`.

Al final se imprime una tabla resumen. El código de salida es 1 si algún
documento no pasa la auditoría. Una config con errores (`validate-config`) se
rechaza antes de empezar.

**Ciclo recomendado con un cliente nuevo**: `inventory` → preparar la config
→ `sanitize` → leer el "texto restante para revisar" de cada `auditoria.md` →
añadir a las reglas lo que sea un nombre → repetir.

## UI de revisión (fase 8)

```bash
uv run review data/plano.pdf --config configs/<cliente>.yaml [--map data/maps/<proyecto>.map.json] [--repair N]
```

Ventana PyQt6 para preparar la config de un cliente mirando el plano:

- **Ver detecciones**: la página original con marcas de color por tipo
  (sustitución, zona, logo, resto y aviso de la auditoría, coincidencia
  rozada, bloqueada por la lista blanca, texto para revisar). La lista de la
  derecha se filtra por tipo y página; al elegir una fila se resalta en el
  plano (o se busca su texto). «Ver anonimizado» alterna con la versión
  anonimizada.
- **Descartar** un falso positivo: excepción en la regla (`exceptions`),
  imagen a conservar (`logos.keep`) o zona desactivada (`disable`).
- **Añadir término** a una regla existente o crear una regla nueva, con el
  texto de la fila elegida (p. ej. una línea del "texto para revisar").
- **Dibujar zona**: se arrastra un rectángulo, que se guarda en fracción de
  página, para esta página, todas o una lista (`1, 3-7, -1`).
- **Confirmar y generar**: proceso completo de `sanitize` (con OCR,
  metadatos, auditoría y autocorrección) y abre el informe.

Al abrir se cargan los informes de un `sanitize` previo si son más recientes
que el PDF y la config. Si no, se hace una **vista previa rápida sin OCR**:
segundos en vez de minutos, pero no ve el texto en trazos ni el escaneado.
«Con OCR» la repite completa. La vista previa trabaja en
`data/reports/<doc>/revision/` con una copia del mapa, así que no toca el
mapa real.

Cada cambio se escribe en la config del cliente con ruamel.yaml, que conserva
comentarios y orden, y deja antes una copia `.bak`. Si el cambio dejaría la
config inválida o con solapes, se deshace. Tras un cambio, las detecciones
quedan marcadas como desactualizadas hasta «Recalcular». La UI no edita
`base.yaml` ni `example.yaml`.

## App de escritorio

**Windows**: descarga `GhostPID-windows-x64.zip` de la página de
[Releases](https://github.com/osbertodiaz/GhostPID/releases), descomprímelo
y abre `GhostPID.exe`. No hace falta instalar nada: el OCR (tesseract
con inglés y español) va incluido. `data/` y `configs/` se crean junto al
.exe. La construye el workflow `.github/workflows/windows.yml` al subir un
tag `v*`; también se puede lanzar a mano desde Actions.

**macOS** (desde el código, con tesseract de Homebrew):

```bash
./scripts/build_app.sh   # → dist/GhostPID.app
```

La app de macOS usa `data/` y `configs/` del proyecto, así que se abre desde
`dist/` (no la muevas a Aplicaciones). Al arrancar pide el PDF y la config
del cliente, o crea una a partir del ejemplo. `GHOSTPID_SELFTEST=1`
comprueba el paquete sin abrir ventanas (módulos, tesseract y OCR).

## Estructura

```
src/sanitizer/   motores: metadatos, texto, zonas, logos, verificación
configs/         reglas YAML por cliente o plantilla
configs/blacklist/  lista negra de la industria (CSV)
tests/           tests con PDFs sintéticos generados con reportlab
data/            PDFs reales y mapas de sustitución (fuera de git)
```

## Datos

`data/` está en `.gitignore`. Nunca se commitean PDFs reales ni mapas de
sustitución. Los tests solo usan PDFs sintéticos.

## Roadmap

0. Entorno · 1. Diagnóstico · 2. Reglas · 3. Metadatos · 4. Redacción de texto ·
5. Zonas, logos y texto vectorial · 6. Mapa de sustitución · 7. Verificación y
auditoría · 8. CLI y UI de revisión · 9. LLM local (opcional)
