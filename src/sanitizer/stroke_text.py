"""Detección heurística de texto convertido a trazos (p. ej. fuentes SHX de AutoCAD).

En muchos P&ID exportados desde CAD los tags no están en la capa de texto:
cada carácter es un path vectorial independiente. Este módulo localiza esos
caracteres sin OCR, solo con geometría:

1. Un "glifo candidato" es un path de trazo o relleno con tamaño de letra
   (altura entre MIN y MAX puntos y anchura entre 0,2 y 1,5 veces la altura).
   Los caracteres de un solo trazo ("1", "I") quedan fuera, pero no rompen la
   racha porque la separación sigue dentro de la tolerancia.
2. Los candidatos con la misma altura y la misma línea base, separados menos
   de GAP_FACTOR × altura, forman una "racha".
3. Una racha de al menos MIN_RUN glifos se considera texto en trazos.

Se analiza en horizontal y en vertical (texto girado o páginas rotadas).
Las líneas discontinuas no cuentan: sus segmentos tienen altura ~0 en
horizontal o se apilan, sin compartir línea base, en vertical.

Es una estimación para el diagnóstico y para localizar regiones donde aplicar
OCR (fase 5); no sustituye a la verificación visual.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import pymupdf

MIN_GLYPH_HEIGHT = 3.0  # pt; por debajo no es texto legible en un plano
MAX_GLYPH_HEIGHT = 24.0  # pt; por encima suelen ser símbolos (globos, rombos)
MAX_ASPECT = 1.5  # anchura máxima / altura
MIN_ASPECT = 0.2  # anchura mínima / altura (descarta segmentos sueltos)
GAP_FACTOR = 1.2  # separación máxima entre glifos, en alturas
MIN_RUN = 3  # glifos mínimos para considerar una racha como texto
QUANTUM = 0.5  # pt; redondeo de altura y línea base para agrupar


@dataclass(frozen=True)
class StrokeTextRun:
    """Secuencia de glifos alineados: probablemente una palabra o etiqueta."""

    rect: tuple[float, float, float, float]
    glyphs: int
    vertical: bool


def find_stroke_text(drawings: Iterable[dict[str, Any]]) -> list[StrokeTextRun]:
    """Devuelve las rachas de texto en trazos a partir de `page.get_cdrawings()`."""
    rects = [
        pymupdf.Rect(d["rect"]) for d in drawings if d.get("type") in ("s", "f", "fs")
    ]
    runs = _runs(rects, vertical=False)
    used = {i for run, members in runs for i in members}
    # En vertical solo se consideran glifos no usados ya en horizontal
    vruns = [(r, m) for r, m in _runs(rects, vertical=True) if not used.intersection(m)]
    return [run for run, _ in runs + vruns]


def _runs(
    rects: list[pymupdf.Rect], vertical: bool
) -> list[tuple[StrokeTextRun, list[int]]]:
    # Se trabaja en coordenadas "de lectura": a lo largo (x) y altura de letra (h)
    cands = []
    for i, r in enumerate(rects):
        along0, along1 = (r.y0, r.y1) if vertical else (r.x0, r.x1)
        height = r.width if vertical else r.height
        base = r.x1 if vertical else r.y1
        width = along1 - along0
        if (
            MIN_GLYPH_HEIGHT <= height <= MAX_GLYPH_HEIGHT
            and MIN_ASPECT * height <= width <= MAX_ASPECT * height
        ):
            key = (round(height / QUANTUM), round(base / QUANTUM))
            cands.append((key, along0, along1, height, i))
    cands.sort()

    out: list[tuple[StrokeTextRun, list[int]]] = []
    current: list[tuple] = []

    def flush() -> None:
        if len(current) >= MIN_RUN:
            members = [c[4] for c in current]
            box = pymupdf.Rect(rects[members[0]])
            for m in members[1:]:
                box |= rects[m]
            out.append((StrokeTextRun(tuple(box), len(members), vertical), members))

    for cand in cands:
        if current and (
            cand[0] != current[-1][0]
            or cand[1] - max(c[2] for c in current) > GAP_FACTOR * cand[3]
        ):
            flush()
            current = []
        current.append(cand)
    flush()
    return out
