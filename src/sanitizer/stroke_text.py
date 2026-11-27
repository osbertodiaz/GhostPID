"""Detección heurística de texto convertido a trazos o contornos.

En muchos P&ID exportados desde CAD los tags no están en la capa de texto:
cada carácter es un path vectorial independiente. Aparece de dos formas:

- Fuentes SHX de AutoCAD: cada letra es una polilínea de trazo.
- Fuentes TrueType convertidas a curvas (impresoras PostScript, "explode"):
  cada letra es un contorno relleno, a veces partido en varios paths.

Este módulo localiza esos caracteres sin OCR, solo con geometría:

1. Un "glifo candidato" es un path de trazo o relleno con tamaño de letra:
   altura entre MIN y MAX y anchura entre MIN_ASPECT y MAX_ASPECT veces la
   altura. Los caracteres de un solo trazo ("1", "I") quedan fuera, pero no
   rompen la racha porque la separación sigue dentro de la tolerancia.
2. Los candidatos con altura y eje central parecidos (tolerancia relativa a la
   altura), separados menos de GAP_FACTOR × altura, forman una "racha".
   Los paths que empiezan casi en el mismo punto que el glifo anterior (copias
   superpuestas o piezas de una misma letra) no cuentan como glifo nuevo.
3. Una racha es texto si tiene al menos MIN_RUN glifos, al menos
   MIN_DISTINCT_SHAPES formas distintas y la mayoría de sus glifos no se
   solapan con el anterior. Así se descartan filas de símbolos idénticos,
   sombreados y teselas contiguas (parcelas de un plano catastral).

Las medidas se escalan con el tamaño de página tomando A1 como referencia:
un plano A1 impreso en A4 tiene letras ~2,8 veces más pequeñas.

Se analiza en horizontal y en vertical (texto girado o páginas rotadas).

Es una estimación para el diagnóstico y para localizar regiones donde aplicar
OCR (fase 5); no sustituye a la verificación visual.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

import pymupdf

# Diagonal de un A1 en puntos: escala de referencia de las medidas siguientes
REFERENCE_DIAGONAL = math.hypot(2384, 1684)

MIN_GLYPH_HEIGHT = 3.0  # pt en A1; por debajo no es texto legible en un plano
MAX_GLYPH_HEIGHT = 24.0  # pt en A1; por encima suelen ser símbolos (globos, rombos)
MIN_ASPECT = 0.2  # anchura mínima / altura (descarta segmentos sueltos)
MAX_ASPECT = 1.5  # anchura máxima / altura
GAP_FACTOR = 1.2  # separación máxima entre glifos, en alturas
HEIGHT_TOLERANCE = 0.25  # diferencia de altura admitida dentro de una racha
CENTER_TOLERANCE = 0.25  # desplazamiento del eje central admitido, en alturas
MIN_ADVANCE = 0.3  # avance mínimo (en alturas) para contar un glifo nuevo
MIN_RUN = 3  # glifos mínimos para considerar una racha como texto
MIN_DISTINCT_SHAPES = 2  # formas distintas mínimas en una racha
OVERLAP_TOLERANCE = -0.02  # separación mínima con el glifo anterior (en alturas); tocarse cuenta como solape
MAX_OVERLAP_RATIO = 0.5  # fracción máxima de glifos que se solapan con el anterior


@dataclass(frozen=True)
class StrokeTextRun:
    """Secuencia de glifos alineados: probablemente una palabra o etiqueta."""

    rect: tuple[float, float, float, float]
    glyphs: int
    vertical: bool


@dataclass
class _OpenRun:
    center: float
    height: float
    end: float
    last_start: float
    members: list[int] = field(default_factory=list)
    glyphs: int = 0
    shapes: set = field(default_factory=set)
    glyph_end: float = 0.0  # final del último glifo contado (con sus piezas)
    overlaps: int = 0


def page_scale(page_rect: pymupdf.Rect | None) -> float:
    """Factor de escala de la página respecto a un A1."""
    if page_rect is None:
        return 1.0
    return math.hypot(page_rect.width, page_rect.height) / REFERENCE_DIAGONAL


def find_stroke_text(
    drawings: Iterable[dict[str, Any]], page_rect: pymupdf.Rect | None = None
) -> list[StrokeTextRun]:
    """Devuelve las rachas de texto en trazos a partir de `page.get_cdrawings()`.

    `page_rect` permite escalar los umbrales al tamaño de la página; sin él se
    asume un A1.
    """
    paths = [
        (pymupdf.Rect(d["rect"]), len(d.get("items", ())))
        for d in drawings
        if d.get("type") in ("s", "f", "fs") and not _is_plain_box(d)
    ]
    scale = page_scale(page_rect)
    runs = _runs(paths, scale, vertical=False)
    used = {i for _, members in runs for i in members}
    # En vertical solo se consideran glifos no usados ya en horizontal
    vruns = [(r, m) for r, m in _runs(paths, scale, vertical=True) if not used.intersection(m)]
    return [run for run, _ in runs + vruns]


def _is_plain_box(drawing: dict[str, Any]) -> bool:
    """Un único rectángulo o cuadrilátero no es una letra (casillas, parcelas)."""
    items = drawing.get("items", ())
    return len(items) == 1 and items[0][0] in ("re", "qu")


def _runs(
    paths: list[tuple[pymupdf.Rect, int]], scale: float, vertical: bool
) -> list[tuple[StrokeTextRun, list[int]]]:
    min_h, max_h = MIN_GLYPH_HEIGHT * scale, MAX_GLYPH_HEIGHT * scale

    # Coordenadas "de lectura": a lo largo de la línea (along) y altura de letra
    cands = []
    for i, (r, n_items) in enumerate(paths):
        along0, along1 = (r.y0, r.y1) if vertical else (r.x0, r.x1)
        height = r.width if vertical else r.height
        center = (r.x0 + r.x1) / 2 if vertical else (r.y0 + r.y1) / 2
        width = along1 - along0
        if min_h <= height <= max_h and MIN_ASPECT * height <= width <= MAX_ASPECT * height:
            shape = (round(4 * width / height), min(n_items, 20))
            cands.append((along0, along1, height, center, shape, i))
    cands.sort()

    # Las rachas abiertas se indexan por franjas del eje central para no
    # comparar cada glifo con todas las líneas de texto de la página
    bucket = max(min_h, 1e-6)
    open_runs: dict[int, list[_OpenRun]] = defaultdict(list)
    out: list[tuple[StrokeTextRun, list[int]]] = []

    def finalize(run: _OpenRun) -> None:
        if (
            run.glyphs >= MIN_RUN
            and len(run.shapes) >= MIN_DISTINCT_SHAPES
            and run.overlaps <= MAX_OVERLAP_RATIO * (run.glyphs - 1)
        ):
            box = pymupdf.Rect(paths[run.members[0]][0])
            for m in run.members[1:]:
                box |= paths[m][0]
            out.append((StrokeTextRun(tuple(box), run.glyphs, vertical), run.members))

    for along0, along1, height, center, shape, idx in cands:
        reach = CENTER_TOLERANCE * height
        match = None
        for key in range(int((center - reach) // bucket), int((center + reach) // bucket) + 1):
            if key not in open_runs:
                continue
            alive = []
            for run in open_runs[key]:
                if along0 - run.end > GAP_FACTOR * run.height:
                    finalize(run)  # ya no puede crecer: los candidatos van ordenados
                    continue
                alive.append(run)
                if (
                    match is None
                    and abs(center - run.center) <= CENTER_TOLERANCE * run.height
                    and abs(height - run.height) <= HEIGHT_TOLERANCE * run.height
                ):
                    match = run
            open_runs[key] = alive

        if match is None:
            run = _OpenRun(center, height, along1, along0, [idx], 1, {shape}, along1)
            open_runs[int(center // bucket)].append(run)
            continue

        match.members.append(idx)
        match.end = max(match.end, along1)
        if along0 >= match.last_start + MIN_ADVANCE * match.height:
            if along0 < match.glyph_end - OVERLAP_TOLERANCE * match.height:
                match.overlaps += 1
            match.glyphs += 1
            match.last_start = along0
            match.glyph_end = along1
            match.shapes.add(shape)
        else:  # pieza o copia del glifo actual
            match.glyph_end = max(match.glyph_end, along1)

    for runs in open_runs.values():
        for run in runs:
            finalize(run)
    return out
