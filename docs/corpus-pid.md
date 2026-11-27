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
