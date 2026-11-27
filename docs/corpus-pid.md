# Corpus de P&ID reales: patrones encontrados

Resumen del diagnóstico (fase 1) sobre 6 sets reales de P&ID y planos de proceso
guardados en `data/` (fuera de git). Aquí solo se describen **patrones
técnicos**; no se citan empresas, personas, proyectos ni ubicaciones.

Estos sets son **ejemplos**, no el universo de casos: aparecerán otros
orígenes (otros CAD, otras impresoras PDF, otros escáneres). Por eso todos los
umbrales calibrados aquí son valores por defecto de un dataclass de parámetros
(`StrokeTextParams`, `DetectionParams`) que cada config puede ajustar en su
bloque `detection`, y la lista blanca base es un punto de partida que cada
cliente amplía o corrige.

| Set | Origen aparente | Páginas | Cómo está el texto | Qué implica |
|---|---|---:|---|---|
| A | AutoCAD → plotter PDF, A1/ANSI D | 25 | Cajetín en TrueType; tags en **trazos SHX**, una letra por path | OCR en las zonas de tags; el texto real es casi solo el cajetín |
| B | Escaneado + impresora PDF, A3 | 68 | **Imagen** a página completa; 15 páginas con **capa OCR invisible** (basura + datos reales) | OCR obligatorio; la capa OCR invisible también hay que eliminarla |
| C | Mezcla Acrobat, A1/A3 | 73 | 39 páginas vectoriales con texto real; 34 escaneadas | Tratar cada página según su tipo |
| D | PostScript → Distiller, A1 reducido a **A4** | 10 | Texto TrueType convertido a **contornos rellenos**, ~2,5 pt | Umbrales relativos al tamaño de página |
| E | AutoCAD → PDFMaker, A4 | 74 | Texto SHX **visible como trazos** + el mismo texto **invisible** (modo 3) | Redactar el texto invisible no borra lo visible: hay que quitar también los trazos debajo |
| F | PostScript → Distiller, 11×17" | 10 | Todo en trazos, sin ninguna fuente | Solo OCR; no hay capa de texto que limpiar |

## Información sensible encontrada

- **Metadatos**: autor, título, asunto y palabras clave con nombre de planta,
  cliente, expediente y ubicación. XMP con UUID de documento.
- **Capas (OCG)**: hasta 624 por documento. Sus nombres contienen número de
  proyecto, nombres de ficheros DWG y bloques de cajetín.
- **Marcadores**: títulos con nombres de carpetas internas y fechas.
- **Anotaciones**: comentarios de revisión con el usuario del revisor.
- **Margen lateral del plano**: ruta de red, nombre de fichero, usuario CAD y
  fecha de ploteo (en texto real o en trazos).
- **Logos y sellos**: como imagen repetida en muchas páginas (a veces una copia
  por página, a veces en variantes girada o en gris), o como **vectores**.
- **Cajetín**: empresa de ingeniería, operador, dirección, nombres y firmas
  manuscritas (estas últimas dentro de la imagen escaneada).

## Consecuencias para el diseño

1. **Fase 3 (metadatos)**: además de Info/XMP/ID, limpiar nombres de capas,
   títulos de marcadores y anotaciones.
2. **Fase 4 (texto)**: la redacción debe cubrir también el texto invisible y,
   en páginas tipo E, eliminar el line art bajo cada redacción.
3. **Fase 5 (zonas y logos)**: es la fase crítica. En los sets A, D, E y F los
   tags técnicos están en trazos o contornos, y en B y C en imagen. Los logos
   se agrupan por hash de contenido, pero hará falta un hash perceptual para
   las variantes giradas o en otro espacio de color.
4. **Fase 7 (verificación)**: buscar la lista negra en la capa de texto no
   basta; hay que rasterizar y pasar OCR.

## Detector de texto en trazos (`stroke_text.py`)

Calibrado con este corpus. Glifos contados por página:

| Set | Páginas sin texto en trazos | Páginas con texto en trazos |
|---|---|---|
| A | 6–27 (logo) | 98–2.800 |
| C | 0–41 | — (los tags son texto real) |
| D | 0 | 2.300–3.300 |
| E | 0–15 | — (letras agrupadas en un solo path; las cubre el texto invisible) |
| F | — | 1.400–3.900 |

Falsos positivos corregidos durante la calibración: logo dibujado varias veces
en el mismo sitio, sombreados, filas de símbolos idénticos, segmentos de líneas
discontinuas y parcelas de un mapa catastral.

**Limitación conocida**: en mapas de situación con parcelas irregulares quedan
algunas detecciones (~100 glifos en una portada), lo que solo provoca que esa
página pida OCR sin necesidad.

## Lista blanca base contra texto real

Probada contra la capa de texto de los sets con texto extraíble. Lecciones:

- Con `\b` como límite, fragmentos con forma de tag dentro de referencias
  documentales quedaban protegidos y la referencia solo se sustituía a medias.
  Por eso la lista blanca usa límites de token (`boundary: token`).
- El formato métrico de número de línea (`150-PG-1001-B1`) se tragaba
  referencias documentales que empiezan por un código de 5 cifras; el tamaño
  se limita a 1–4 dígitos.
- En los globos de instrumento, letras y número de lazo son palabras distintas
  en la capa de texto (`TI` / `01234`). Los números sueltos no se protegen,
  así que una sustitución por regex numérica (p. ej. `\d{6}` para un número
  de proyecto) podría alcanzar números de lazo. **Pendiente para la fase 4.**

## Metadatos: casos encontrados al limpiar (fase 3)

- **`/ID` con `static_id`**: qpdf conserva el primer elemento del `/ID`
  original (el identificador permanente) y solo fija el segundo. Hay que
  borrar el `/ID` antes de guardar para que ambos sean el valor fijo.
- **Info dentro del catálogo**: además del trailer, algún generador deja un
  `/Info` con el productor dentro de `/Root`.
- **Capas sin registrar**: en un set había 167 capas registradas en
  `/OCProperties` y otras 460 referenciadas solo desde el contenido de las
  páginas. Hay que renombrar todos los objetos `/Type /OCG`.
- **Widgets fuera del formulario**: un set tenía 852 widgets (botones de
  navegación) que no colgaban de `/AcroForm`. Borrar `/AcroForm` hacía que
  MuPDF dejara de dibujarlos: el formulario se sanea en lugar de borrarse.
- **Páginas fantasma**: un set combinado tenía 12 marcadores apuntando a
  objetos de página que ya no estaban en el árbol de páginas. Esos objetos se
  escribirían en la salida con su contenido; los destinos se descartan.
- **Árbol de estructura**: al quitarlo, MuPDF extrae un guion blando de forma
  distinta (`PRE\xad` → `PRE-`). El aspecto de la página no cambia.

Verificado sobre los 6 sets: ninguna página cambia de aspecto (comparación de
píxeles), la verificación con pikepdf pasa y ningún valor de Info, nombre de
capa, autor de anotación o título de marcador original queda en ningún objeto
de la salida, streams descomprimidos incluidos.

**Pendiente para las fases 4 y 5**: el contenido de las capas **ocultas** sigue
en el PDF (no se ve, pero se puede extraer), igual que el texto de las
anotaciones y el valor de los campos de formulario.

## Redacción de texto: casos encontrados (fase 4)

Probada sobre los 4 sets con capa de texto, con una config local (ignorada por
git) con los nombres reales: ningún nombre queda en la capa de texto y ninguna
coincidencia de la lista blanca desaparece (unas 14 000 comprobadas).

- **Interlineado apretado y textos solapados**: MuPDF borra cualquier carácter
  cuya caja toque la redacción. Con la caja completa se perdían líneas vecinas
  y, en un set, notas (`NOTE 4`) que se solapaban con un tag vertical. Se
  redacta con una franja del 35–50 % de la altura de letra.
- **Anotaciones en el texto de página**: `get_text` incluye la apariencia de
  las anotaciones y redactar encima las borraba enteras. El texto de página se
  busca en una copia sin anotaciones.
- **Trazos SHX bajo texto invisible**: el modo "covered" de MuPDF mide los
  trazos con su caja ampliada 10 × grosor (límite de inglete) y no borra
  diagonales; "touched" mide por caja y se lleva globos y tuberías. Se editan
  los operadores de trazado con pikepdf, por subtrazo y con la CTM. En curvas
  solo cuentan los puntos sobre el trazo (los de control sobresalen).
- **Capas ocultas**: MuPDF no extrae su texto; hay que activarlas, reabrir el
  documento y restaurar su estado al guardar.
- **Referencias con varias hojas** (`…-PID-064/065/066`) y **palabras pegadas
  por el OCR** (`NOMBREG-A-SCOPORATION`): las reglas no encajaban. De ahí las
  coincidencias rozadas y la búsqueda relajada en páginas escaneadas.
- **Rendimiento**: ~1 s por página en los planos más densos (un set de 74
  páginas tarda ~70 s).

## Zonas, logos y OCR: casos encontrados (fase 5)

Probado sobre los 6 sets con configs locales. Comprobación final como la haría
un LLM con visión: OCR de las páginas de salida buscando los nombres reales.
No queda ninguno de los que describen las reglas, y ninguna de las áreas
borradas por OCR toca un dato protegido leído en la entrada.

- **Tesseract en planos**: a 300 ppp lee mal el texto SHX pequeño; hace falta
  ~600 ppp para un A1 (más si es un A1 reducido a A4, con tope de píxeles). El
  texto vertical exige probar 90° **y** 270° (en una orientación sale en espejo).
  Unos 3 s por orientación y página.
- **OCR inestable**: al quitar texto cambia la segmentación de Tesseract y en
  una segunda lectura aparecen palabras que antes leyó mal ("PREVIO" en vez de
  la palabra real, o una palabra partida en dos líneas). Las páginas con
  coincidencias se leen una segunda vez (`ocr.passes`).
- **Logos apilados**: un logo con el nombre en dos líneas no encaja con un
  término de dos palabras. Las coincidencias rozadas avisan ahora de la
  primera palabra de los términos de varias palabras.
- **Logos en imagen**: el mismo logo en color en unas páginas y en gris en
  otras, y el mismo sello en tres variantes (una girada), se agrupan con el hash
  perceptual. Un set tenía una máscara de 1 bit sin espacio de color que hacía
  fallar la conversión: ahora se salta.
- **Logos vectoriales** (símbolos, letras estilizadas) no son texto ni imagen:
  hace falta una zona por plantilla. Lo mismo para nombres que las reglas no
  describen: el motor solo quita lo que la config dice.
- **Rendimiento**: ~8–16 s por página con OCR (un set escaneado de 68 páginas,
  ~10 min); las páginas que no lo necesitan no lo pagan.

## Auditoría: resultados sobre el corpus (fase 7)

Ciclo completo sobre los 6 sets: `anonymize` → `clean-metadata` → `audit`
(OCR de todas las páginas, unos 40 minutos en total).

- **Metadatos y objetos**: limpios en los 6. Al escribir la auditoría salió que
  `unparse()` de pikepdf sobre un objeto indirecto solo devuelve la referencia
  (`1 0 R`). Las comprobaciones de las fases 3 y 4 que buscaban restos en los
  diccionarios eran más débiles de lo que parecía. Se corrigieron
  (`unparse(resolved=True)`) y se repitió la verificación: la limpieza ya era
  correcta.
- **Restos que el anonimizado no vio**: en un set escaneado, 9 referencias
  documentales y un teléfono que el OCR del anonimizado leyó mal y el de la
  auditoría leyó bien (la variabilidad del OCR de la fase 5). En otro set, un
  término dentro de otra palabra ("NOMBRE" en "NOMBREAN BELT ROAD"): la
  sustitución exige palabra completa, pero la auditoría busca también dentro
  de palabras.
- **Texto restante para revisar**: en todos los sets aparecen arriba
  justamente los nombres que las reglas no conocían: operador, sigla, dirección
  completa, e-mail, web, nombre de la refinería y logos en escaneados. Para
  reducir el ruido se descartan las etiquetas típicas de cajetín y las líneas
  de OCR con confianza media menor de 80.

**Pendiente para la fase 8**: usar los hallazgos de la auditoría para
corregir automáticamente (redactar las áreas de los restos que tienen
coordenadas y volver a auditar) en lugar de solo informar.

## CLI completa y autocorrección: resultados (fase 8)

`sanitize` sobre los 6 sets (anonimizar → metadatos → auditoría →
autocorrección de una pasada): unos 72 minutos, 5 de 6 sin restos a la
primera.

- **Tamaño y caída con escaneados**: la primera pasada completa terminó en
  segfault con el set escaneado de 68 páginas. MuPDF guarda sin comprimir las
  imágenes JPEG redactadas por píxeles. El anonimizado salía unas 10 veces
  mayor que el original (más de 200 MB) y las copias en memoria para el OCR
  ocupaban varios GB. Ahora las imágenes que eran JPEG en el original se
  recodifican como JPEG (calidad 90, `redaction.jpeg_quality`), y toda copia
  en memoria se guarda comprimida. Ese set pasó de 223 MB a 49 MB y ya no cae.
- **Autocorrección**: en el set escaneado más grande corrigió restos de OCR
  en 10 páginas. Uno de los restos volvió a aparecer en la reauditoría: el
  OCR de letra muy pequeña (unos 4 pt) no lee igual dos veces. `--repair 2`
  da una segunda pasada.
- **Lecturas giradas**: en páginas rotadas, el OCR leyó del revés una
  denominación genérica ya insertada ("DOC-REF-108" como un número de teléfono) y la
  marcó como teléfono. La autocorrección no la tocó porque debajo hay texto
  real. La auditoría marca ahora como aviso, no como error, los hallazgos de
  OCR cuya área ocupa texto real en otra dirección, y solo esos. Si el texto
  real va en la misma dirección que la línea del OCR, sigue siendo error:
  podría ser una fuga bajo la sustitución.
- **Sufijos en referencias**: una referencia documental con sufijo de letra
  (`XXX-D-014-A`) no encajaba en un patrón que solo admitía sufijo numérico.
  Era un hueco de la config local, no de la herramienta. La auditoría lo
  encontró en la capa de texto y no lo corrigió a ciegas, como se esperaba.
