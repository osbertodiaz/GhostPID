# Manual de uso de PID Anonymouse

PID Anonymouse quita de los P&ID y PFD en PDF lo que identifica al cliente
antes de pasárselos a un LLM. Elimina empresas, operador, fabricantes,
ubicación, códigos de proyecto, referencias a otros documentos, autores,
logos, cajetines y metadatos. Conserva todo lo técnico: equipos, tags de
instrumentos, líneas, tamaños, condiciones de diseño, alarmas, interlocks y
notas.

- **Todo ocurre en tu equipo.** No se envía nada a internet ni a ningún LLM.
- **Es determinista:** decide con reglas, patrones y zonas que tú controlas,
  no con inteligencia artificial. Con el mismo PDF y las mismas reglas, el
  resultado es siempre el mismo.
- **El texto se borra de verdad:** no se tapa con un recuadro. Un LLM no
  puede recuperarlo ni leyendo la capa de texto ni "mirando" la imagen.

---

## 1. Instalación

### Windows (descarga)

1. Descarga `PID-Anonymouse-windows-x64.zip` desde la página de
   [Releases](https://github.com/osbertodiaz/PID_Anonymouse/releases).
2. Descomprímelo en una carpeta donde puedas escribir, por ejemplo
   `Documentos\PID Anonymouse`. No lo ejecutes desde dentro del zip.
3. Abre `PID Anonymouse.exe`. La app no está firmada, así que Windows
   SmartScreen puede avisar: pulsa **Más información** y después
   **Ejecutar de todas formas**.

No hace falta instalar nada más: el OCR (Tesseract, con inglés y español) va
incluido.

### macOS (desde el código)

Necesitas [uv](https://docs.astral.sh/uv/) y Tesseract:

```bash
brew install uv tesseract tesseract-lang
git clone https://github.com/osbertodiaz/PID_Anonymouse.git
cd PID_Anonymouse
uv sync
./scripts/build_app.sh
```

La app queda en `dist/PID Anonymouse.app`. Ábrela desde ahí: trabaja con las
carpetas `data/` y `configs/` del proyecto, así que no la muevas a
Aplicaciones.

### Carpetas de trabajo

La primera vez que se abre, la app crea estas carpetas. En Windows van junto
al `.exe`; en macOS, en la carpeta del proyecto.

| Carpeta | Contenido |
|---|---|
| `data/` | Tus PDF originales y todo lo que se genera |
| `data/out/` | **Los PDF anonimizados**: lo único que se comparte |
| `data/reports/<documento>/` | Informes de anonimizado y auditoría |
| `data/maps/` | Mapas de sustitución (los nombres reales y su sustituto) |
| `configs/` | Reglas: `base.yaml` y `example.yaml` (no se editan) y una config por cliente |

> ⚠️ `data/` y `configs/<cliente>.yaml` contienen los nombres reales que
> quieres ocultar. No los compartas, no los subas a ningún sitio y no se los
> pases a un LLM. Comparte solo los PDF de `data/out/`.

---

## 2. Primeros pasos

1. Copia los PDF que quieras anonimizar a `data/`. Es opcional, pero así los
   tienes a mano.
2. Abre la app. En el diálogo inicial:
   - **PDF original**: elige el plano.
   - **Config del cliente**: elige la de tu cliente o crea una con
     **Nueva…**, que parte del ejemplo. Una config por cliente sirve para
     todos sus planos.
   - **Pasadas de autocorrección**: deja 1. Con planos escaneados de letra
     muy pequeña, 2 ayuda.
3. Pulsa **Abrir**.

El diálogo también indica si encontró Tesseract. Sin él, el OCR no funciona
y los planos escaneados o con texto convertido en trazos no se pueden
limpiar.

> La config que crea **Nueva…** trae nombres **ficticios** (ACME, Globex,
> Initech…). Lo primero es cambiarlos por los de tu cliente, desde la propia
> app (apartado 3.4) o editando el YAML (apartado 5).

---

## 3. La ventana de revisión

A la izquierda está la página del plano. A la derecha, la lista de lo que la
herramienta ha detectado.

### 3.1 Qué se ve al abrir

- Si ya generaste ese documento antes, con la misma config y el mismo PDF,
  se cargan esos informes al instante.
- Si no, se hace una **vista previa rápida sin OCR**, que tarda segundos.
  Esta vista no ve el texto de los planos escaneados ni el texto convertido
  en trazos (habitual en los PDF exportados de AutoCAD). Para verlo, pulsa
  **Con OCR**, que puede tardar varios minutos en documentos grandes.

### 3.2 Barra de herramientas

| Botón | Qué hace |
|---|---|
| ◀ ▶ y el número | Cambia de página |
| **Ver anonimizado** | Alterna entre el original con marcas y la versión anonimizada |
| **Ajustar**, ＋, － | Zoom. También funciona Ctrl + rueda del ratón; para moverte, arrastra la página |
| **Dibujar zona** | Arrastra un rectángulo para borrar esa área entera (apartado 3.5) |
| **Recalcular** | Repite la vista previa rápida con la config actual |
| **Con OCR** | Repite la vista previa con OCR (lenta, pero lo ve todo) |
| **Confirmar y generar** | Proceso completo: crea el PDF final y la auditoría |

### 3.3 Colores y tipos de detección

| Tipo | Color | Significado |
|---|---|---|
| sustitución | verde | Texto sensible sustituido por un genérico (`EPC`, `OPR`, `VENDOR-1`…) |
| zona | azul | Área borrada entera (cajetín, márgenes…) |
| logo | morado | Imagen eliminada (logo repetido en varias páginas o marcado en la config) |
| resto | rojo | **La auditoría encontró algo sensible que sigue en el PDF** |
| aviso | naranja | La auditoría tiene dudas; revísalo (p. ej. una lectura girada del OCR) |
| rozada | amarillo | Algo que casi encaja con una regla, pero no se sustituyó |
| bloqueada | gris | Coincidía con una regla, pero se protegió porque es un dato técnico (un tag) |
| revisar | turquesa | Texto que queda en el cajetín y que ninguna regla conoce |

Encima de la lista puedes marcar o desmarcar cada tipo. **Solo esta página**
limita la lista a la página actual. Al elegir una fila, el plano salta a ella
y la resalta en rosa.

**Revisa sobre todo los tipos *revisar*, *rozada* y *resto*.** Ahí suelen
aparecer los nombres que tus reglas todavía no conocen: el nombre completo
de la planta, una sigla del operador, una dirección o un e-mail.

### 3.4 Corregir las reglas

Cada corrección se guarda al momento en la config del cliente. Antes se hace
una copia `.bak` y, si el cambio dejaría la config inválida, no se aplica.
Después de corregir, la ventana avisa de que las detecciones están
desactualizadas: pulsa **Recalcular** (o **Con OCR**) para ver el efecto.

**Descartar un falso positivo.** Selecciona la fila y pulsa **Descartar**.
Según el tipo:

- *sustitución* (o *resto*/*aviso* de un patrón): ese texto pasa a ser una
  excepción de la regla y no se volverá a sustituir en ningún documento;
- *logo*: esa imagen se conservará siempre;
- *zona*: la zona se desactiva.

**Añadir un término.** Selecciona una fila (por ejemplo, una línea
*revisar* con el nombre de la planta) y pulsa **Añadir término…**:

1. Ajusta el texto: deja solo el nombre, sin el resto de la línea.
2. Elige la regla a la que pertenece. Por ejemplo, `operator → OPR` para el
   propietario, `site → SITE` para la ubicación o `vendors → VENDOR-{n}` para
   un fabricante.
3. Si no encaja en ninguna, elige **Nueva regla…** e indica un id
   (`plant_name`), una categoría y la sustitución (`SITE`, o `PLANT-{n}`
   para numerar).

Los términos se buscan como palabras completas, sin distinguir mayúsculas,
y admiten espacios o saltos de línea entre palabras.

**La lista negra de la industria.** Las reglas `kb-epc`, `kb-licensor`,
`kb-manufacturer` y `kb-operator` (marcadas *lista negra (CSV)* en el
desplegable) vienen de `configs/blacklist/industria.csv`, una base de datos con
más de mil ingenierías, licenciantes, fabricantes y operadores. Si eliges una
de ellas al añadir un término, el nombre se guarda como fila nueva en
`configs/blacklist/propia.csv` y servirá para todos los clientes. Descartar
una detección de la lista negra la convierte en excepción solo para este
cliente. El CSV se puede abrir y ampliar en Excel (ver la sección 5).

### 3.5 Dibujar zonas

Sirven para lo que no es texto, como un logo vectorial o un sello, o para
borrar un bloque entero, como el cajetín o una franja con rutas de ficheros.

1. Pulsa **Dibujar zona** y arrastra un rectángulo sobre el plano.
2. Elige dónde se aplica: **solo esta página**, **todas** o una lista
   (`1, 3-7, -1`, donde `-1` es la última página).
3. Opcional: un texto para escribir en el hueco, como `[CAJETÍN]`.

La zona se guarda en proporción a la página, así que vale para otros planos
del mismo formato aunque cambie el tamaño de hoja.

> En el cajetín suele haber también datos útiles, como el número de revisión
> o la escala. Si te interesa conservarlos, en vez de borrar el cajetín entero
> deja que las reglas sustituyan solo los nombres.

### 3.6 Generar el PDF final

Pulsa **Confirmar y generar**. El proceso completo hace esto:

1. Aplica zonas, logos, reglas de texto y OCR.
2. Limpia los metadatos: autor, software, fechas, capas, marcadores,
   comentarios, formularios y adjuntos.
3. Audita el resultado con independencia del proceso: busca restos en el
   texto, en los objetos internos del PDF, en los metadatos y con OCR de
   todas las páginas.
4. Corrige automáticamente los restos que encuentre el OCR y vuelve a
   auditar.

Tarda de minutos a más de media hora, según el número de páginas y si son
escaneadas. Al terminar, la ventana muestra el anonimizado y el registro
indica el resultado:

- **OK**: no quedan restos. El PDF está en `data/out/<documento>.sanitized.pdf`.
- **con FALLOS**: quedan restos (filas rojas *resto*). Corrige las reglas
  (apartado 3.4) y vuelve a generar.

**Abrir informe de auditoría** muestra `auditoria.md`, con lo que se eliminó,
cuántas veces y en qué páginas, y el texto que conviene revisar. En
`data/reports/<documento>/paginas/` hay un PNG por página con las áreas
tocadas resaltadas.

> **Antes de compartir el PDF, ábrelo y míralo.** La herramienta encuentra lo
> que conocen las reglas y lo que lee el OCR, pero un nombre que no está en
> las reglas y que el OCR lee mal puede quedarse. La lista *revisar* existe
> precisamente para eso.

---

## 4. Flujo recomendado con un cliente nuevo

1. Crea su config con **Nueva…** y cambia los nombres ficticios por los
   reales: empresa de ingeniería, operador, fabricantes, planta, ciudad y
   personas.
2. Abre un plano representativo y pulsa **Con OCR**.
3. Revisa las filas *revisar*, *rozada* y *resto*: añade términos y descarta
   falsos positivos.
4. Dibuja zonas para los logos vectoriales o los bloques que quieras borrar
   enteros.
5. **Recalcular** hasta que la vista te convenza y, después, **Confirmar y
   generar**.
6. Con la config ya afinada, el resto de planos del cliente suele salir
   bien a la primera. Para procesarlos en lote, usa la línea de comandos
   (apartado 6).

---

## 5. La config del cliente (YAML)

Es un fichero de texto en `configs/<cliente>.yaml`. La app lo edita por ti,
pero también puedes abrirlo con cualquier editor de texto. Hereda de
`base.yaml`, que trae la lista blanca de datos técnicos, así que solo
contiene lo propio del cliente.

```yaml
version: 1
name: Mi cliente
extends: base.yaml

substitutions:
  - id: operator                 # identificador de la regla
    category: operator           # company, operator, manufacturer, person,
                                 # location, project, document_ref, other
    replacement: OPR             # genérico que se escribe en su lugar
    terms: [Globex Petroleum, Globex]

  - id: vendors
    category: manufacturer
    replacement: VENDOR-{n}      # {n}: un número distinto por fabricante
    terms: [Initech Pumps, Umbrella Valves]

  - id: document_refs
    category: document_ref
    replacement: DOC-REF-{n}
    patterns: ['ACM-\d{4}-[A-Z]{3}-\d{3}']   # expresión regular
    examples: [ACM-2041-PID-001]              # obligatorio con patterns
    exceptions: [ACM-0000-PID-000]            # coincidencias que NO se sustituyen

  - id: people
    category: person
    replacement: ''              # vacío: se borra sin sustituir
    terms: [John Doe, J. Doe]

zones:
  - id: title_block
    rect: [0.0, 0.0, 0.22, 0.30] # x0, y0, x1, y1 en fracción de página
    anchor: bottom-right         # esquina de referencia
    pages: all                   # o [1, "3-7", -1]
    label: "[CAJETÍN]"           # opcional

logos:
  keep: []                       # imágenes que no se eliminan nunca
  remove: []                     # imágenes que se eliminan siempre
```

Puntos clave:

- **Lista blanca:** los tags de instrumentos y equipos, números de línea,
  tamaños, condiciones de diseño, alarmas e interlocks están protegidos en
  `base.yaml` y tienen prioridad sobre cualquier sustitución.
- **Patrones:** para cosas con forma fija, como números de plano o de
  proyecto, usa `patterns` con una expresión regular y pon siempre ejemplos
  reales del formato en `examples`. La herramienta comprueba que encajan.
- **Reglas heredadas:** para ajustar una regla de `base.yaml` o del ejemplo,
  repite su `id` con las claves que cambian. Para quitarla, usa
  `disable: [id]`.
- **Comprobar una config** desde la línea de comandos:
  `uv run validate-config configs/<cliente>.yaml --check "texto de prueba"`.
- **Lista negra de la industria:** `configs/blacklist/*.csv`, una fila por
  empresa con las columnas `name` (nombre principal), `aliases` (otras formas
  separadas por `|`), `category` (`epc`, `licensor`, `manufacturer`,
  `operator`, o en español `ingenieria`, `licenciante`, `fabricante`,
  `operador`), `segment`, `industries` (`oil_gas`, `chemicals`, `gases`,
  `pharma`, `power`, `general`), `country`, `active` (`no` la desactiva) y
  `notes`. Al guardar, la herramienta rechaza nombres de menos de 3
  caracteres, palabras de P&ID ("Shell", "Total", "Crane") y nombres
  repetidos, indicando el fichero y la línea. Para un cliente concreto:
  `blacklist: {industries: [pharma], exclude: [Vega]}` en su YAML.

---

## 6. Línea de comandos (opcional, desde el código)

Para procesar muchos PDF de una vez o integrarlo en otros procesos:

```bash
# Todos los PDF de una carpeta, con un mapa común para el proyecto
uv run sanitize data/proyecto/ --config configs/<cliente>.yaml --map data/maps/proyecto.map.json

# La UI de revisión desde la terminal
uv run review data/plano.pdf --config configs/<cliente>.yaml

# Devolver los nombres reales a una respuesta del LLM
uv run revert --map data/maps/proyecto.map.json respuesta_llm.txt > respuesta.txt
```

`sanitize` imprime al final una tabla resumen y termina con código 1 si
algún documento no pasa la auditoría. Las opciones de cada comando se ven
con `--help`.

### El mapa de sustitución y `revert`

Cada proceso guarda en `data/maps/` qué original corresponde a cada genérico
(`VENDOR-1` → el fabricante real). Así:

- Con el mismo `--map` en todos los planos de un proyecto, `VENDOR-1` es el
  mismo fabricante en todos ellos.
- Cuando el LLM responda hablando de `VENDOR-1` o `OPR`, `revert` devuelve
  los nombres reales a su respuesta.

Para que la reversión sea exacta, usa `{n}` en las reglas que agrupen varias
entidades distintas. Por ejemplo, `SITE-{n}` en lugar de `SITE` si hay
varias ubicaciones. `revert` solo está disponible desde la línea de comandos.

---

## 7. Problemas frecuentes

| Problema | Solución |
|---|---|
| Windows no deja abrir la app | SmartScreen: **Más información → Ejecutar de todas formas** |
| "tesseract no encontrado" | Windows: usa el zip completo, que lo incluye. macOS: `brew install tesseract tesseract-lang` |
| La vista previa no muestra nada en un plano escaneado | Es la vista rápida sin OCR: pulsa **Con OCR** |
| Un dato técnico se sustituyó por error | Selecciónalo y pulsa **Descartar** |
| Sigue apareciendo un nombre | Añádelo como término (apartado 3.4); si es un logo vectorial, dibuja una zona |
| Restos que vuelven tras generar en escaneados de letra muy pequeña | Sube las **pasadas de autocorrección** a 2 |
| "Cambio rechazado" al añadir algo | El cambio dejaría la config inválida (p. ej. un término que es a la vez excepción). Lee el mensaje; la config no se ha tocado |
| El proceso es lento | Es normal con muchas páginas escaneadas: el OCR se repite para no dejar restos. Mientras ajustas reglas, usa **Recalcular** (sin OCR) |
| Quiero deshacer el último cambio de la config | Copia `configs/<cliente>.yaml.bak` sobre `configs/<cliente>.yaml` |

---

## 8. Buenas prácticas de privacidad

- Comparte solo `data/out/*.sanitized.pdf`. Nunca `data/reports/`,
  `data/maps/` ni tu config de cliente: contienen los nombres reales.
- Revisa visualmente cada PDF antes de enviarlo a un LLM, sobre todo la
  primera vez que trabajas con un cliente.
- Si un plano tiene información sensible fuera de lo habitual, como un
  sello, una firma escaneada o una foto, cúbrela con una zona.
- Lo que la herramienta no conoce, no lo puede quitar: mantén al día la
  config de cada cliente.
