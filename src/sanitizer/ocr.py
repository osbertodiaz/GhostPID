"""Fase 5 · OCR de texto en trazos y de páginas escaneadas.

En muchos P&ID el texto visible no está en la capa de texto: son trazos (SHX,
contornos) o una imagen escaneada. Aquí se renderiza la página tal como se ve
(capas en su estado guardado, sin anotaciones), se pasa Tesseract en varias
orientaciones y se aplican las mismas reglas que en la fase 4 al texto
reconocido, con la lista blanca primero.

Cada coincidencia se elimina según el tipo de página:

- escaneada: se blanquean los píxeles bajo las palabras (y el texto OCR
  invisible que hubiera ahí);
- texto en trazos: se quitan los trazos que quedan dentro del área de las
  palabras (ampliada un poco, porque las cajas del OCR son muy ajustadas),
  con el editor preciso de `lineart`.

Después se escribe la denominación genérica con la orientación del original.

El OCR es determinista para una misma versión de Tesseract y los mismos
parámetros. Los términos se comparan tolerando confusiones típicas
(`0/O`, `1/I/l`, `5/S`, `8/B`); los patrones, tal cual.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import pymupdf
import pytesseract
from PIL import Image
from pydantic import ConfigDict

from sanitizer.redact import (
    FILLS,
    Blocked,
    Replacement,
    Session,
    find_matches,
    insert_replacement,
    near_misses,
    page_mode,
)
from sanitizer.stroke_text import find_stroke_text, page_scale

# Rotaciones que admite la transposición exacta de PIL
_TRANSPOSE = {90: Image.Transpose.ROTATE_90, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_270}


@dataclass(frozen=True)
class OcrPolicy:
    __pydantic_config__ = ConfigDict(extra="forbid")

    enabled: bool = True
    languages: str = "eng+spa"
    # Orientaciones del texto que se prueban (grados, sentido antihorario)
    rotations: list[int] = field(default_factory=lambda: [0, 90, 270])
    # Resolución para un A1; se escala con el tamaño de página, con un tope de píxeles
    base_dpi: int = 600
    max_pixels: int = 60_000_000
    psm: int = 11  # texto disperso: lo habitual en un plano
    min_confidence: float = 50.0
    # También en escaneados que ya traen una capa OCR (suelen ser malas)
    include_scanned_with_text: bool = True
    fuzzy_terms: bool = True
    # Ampliación del área de una palabra al quitar trazos, en alturas de letra
    margin: float = 0.25
    # Pasadas de OCR. Tras quitar lo encontrado, la segmentación de Tesseract
    # cambia y a veces aparece texto que antes leyó mal (una palabra partida en
    # dos líneas, una línea pequeña confundida): las páginas con coincidencias
    # se vuelven a leer
    passes: int = 2

    def __post_init__(self) -> None:
        bad = [r for r in self.rotations if r not in (0, 90, 180, 270)]
        if bad:
            raise ValueError(f"rotations solo admite 0, 90, 180 y 270: {bad}")
        if self.passes < 1:
            raise ValueError("passes debe ser al menos 1")


@dataclass
class OcrChar:
    c: str
    rect: pymupdf.Rect  # coordenadas de página sin rotar


@dataclass
class OcrLine:
    text: str
    chars: list[OcrChar]
    origin: tuple[float, float]
    direction: tuple[float, float]
    height: float  # pt
    rotation: int


# --------------------------------------------------------------------------
# Selección de páginas
# --------------------------------------------------------------------------


def needs_ocr(page: pymupdf.Page, session: Session) -> str | None:
    """Motivo para pasar OCR a la página, o None."""
    mode = page_mode(page, session.detection)
    if mode == "scanned":
        has_text = bool(page.get_text("text").strip())
        if has_text and not session.config.ocr.include_scanned_with_text:
            return None
        return "escaneada con capa OCR" if has_text else "escaneada"
    if mode == "overlay":
        return None  # lo visible ya está cubierto por su texto invisible (fase 4)
    runs = find_stroke_text(page.get_cdrawings(), page.rect, session.detection.stroke_text)
    if sum(r.glyphs for r in runs) >= session.detection.stroke_text_min_glyphs:
        return "texto en trazos"
    return None


# --------------------------------------------------------------------------
# Reconocimiento
# --------------------------------------------------------------------------


def _dpi(page: pymupdf.Page, policy: OcrPolicy) -> float:
    dpi = policy.base_dpi / max(page_scale(page.rect), 1e-6)
    inches = (page.rect.width / 72) * (page.rect.height / 72)
    return max(72.0, min(dpi, math.sqrt(policy.max_pixels / inches)))


def _unrotate(x: float, y: float, rotation: int, width: int, height: int) -> tuple[float, float]:
    """Punto de la imagen girada → punto de la imagen original (píxeles)."""
    if rotation == 90:  # ROTATE_90: (x, y) → (y, W - x)
        return width - y, x
    if rotation == 270:  # ROTATE_270: (x, y) → (H - y, x)
        return y, height - x
    if rotation == 180:
        return width - x, height - y
    return x, y


def ocr_page(page: pymupdf.Page, policy: OcrPolicy) -> list[OcrLine]:
    """Líneas reconocidas en la página, con cajas en coordenadas sin rotar."""
    dpi = _dpi(page, policy)
    zoom = dpi / 72
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY, annots=False)
    image = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    width, height = image.size
    to_page = page.derotation_matrix

    def to_point(x: float, y: float, rotation: int) -> pymupdf.Point:
        ox, oy = _unrotate(x, y, rotation, width, height)
        return pymupdf.Point(ox / zoom, oy / zoom) * to_page

    lines: list[OcrLine] = []
    for rotation in policy.rotations:
        img = image.transpose(_TRANSPOSE[rotation]) if rotation else image
        data = pytesseract.image_to_data(
            img,
            lang=policy.languages,
            config=f"--psm {policy.psm}",
            output_type=pytesseract.Output.DICT,
        )
        grouped: dict[tuple, list[int]] = {}
        for i, word in enumerate(data["text"]):
            if word.strip() and float(data["conf"][i]) >= policy.min_confidence:
                key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
                grouped.setdefault(key, []).append(i)

        for indices in grouped.values():
            indices.sort(key=lambda i: data["left"][i])
            chars: list[OcrChar] = []
            for n, i in enumerate(indices):
                word = data["text"][i]
                left, top, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
                step = w / len(word)
                for k, c in enumerate(word):
                    a = to_point(left + k * step, top, rotation)
                    b = to_point(left + (k + 1) * step, top + h, rotation)
                    chars.append(OcrChar(c, pymupdf.Rect(a, b).normalize()))
                if n < len(indices) - 1:
                    gap = pymupdf.Rect(chars[-1].rect)
                    chars.append(OcrChar(" ", gap))
            first, last = indices[0], indices[-1]
            base_y = max(data["top"][i] + data["height"][i] for i in indices)
            p0 = to_point(data["left"][first], base_y, rotation)
            p1 = to_point(data["left"][last] + data["width"][last], base_y, rotation)
            length = math.hypot(p1.x - p0.x, p1.y - p0.y) or 1.0
            line_height = max(data["height"][i] for i in indices) / zoom
            lines.append(
                OcrLine(
                    "".join(c.c for c in chars),
                    chars,
                    (p0.x, p0.y),
                    ((p1.x - p0.x) / length, (p1.y - p0.y) / length),
                    line_height,
                    rotation,
                )
            )
    return lines


# --------------------------------------------------------------------------
# Redacción
# --------------------------------------------------------------------------


@dataclass
class _OcrHit:
    rule: Any
    original: str
    rect: pymupdf.Rect
    line: OcrLine
    start_origin: tuple[float, float]


def _hits(lines: list[OcrLine], session: Session, page_number: int) -> list[_OcrHit]:
    policy = session.config.ocr
    hits: list[_OcrHit] = []
    for line in lines:
        matches, blocked = find_matches(session.config, line.text, relaxed=True, fuzzy=policy.fuzzy_terms)
        for start, end, rule, wl_id in blocked:
            session.report.blocked.append(Blocked(page_number, "ocr", rule.id, line.text[start:end], wl_id))
        seen = {(n.page, n.text) for n in session.report.near_misses}
        for context, rule in near_misses(session.config, line.text, matches):
            if (page_number, context) not in seen:
                seen.add((page_number, context))
                session.report.near_misses.append(Blocked(page_number, "ocr", rule.id, context, "rozada (OCR)"))
        for m in matches:
            boxes = [c.rect for c in line.chars[m.start : m.end] if not c.c.isspace()]
            if not boxes:
                continue
            rect = pymupdf.Rect(boxes[0])
            for b in boxes[1:]:
                rect |= b
            # Origen de la sustitución: base de la línea a la altura del primer carácter
            first = boxes[0]
            dx, dy = line.direction
            centre = pymupdf.Point((first.x0 + first.x1) / 2, (first.y0 + first.y1) / 2)
            half = line.height / 2
            # Retrocede media letra en la dirección del texto y baja media altura
            ox = centre.x - dx * (first.width if abs(dx) > abs(dy) else first.height) / 2 - dy * half
            oy = centre.y - dy * (first.width if abs(dx) > abs(dy) else first.height) / 2 + dx * half
            hit = _OcrHit(m.rule, line.text[m.start : m.end], rect, line, (ox, oy))
            # La misma palabra leída en dos orientaciones solo cuenta una vez
            if not any((h.rect & rect).get_area() > 0.5 * min(h.rect.get_area(), rect.get_area()) for h in hits):
                hits.append(hit)
    return hits


def ocr_redact(session: Session, only: set[int] | None = None) -> set[int]:
    """Pasa OCR a las páginas que lo necesitan y redacta lo que encuentre.

    `only`: índices de página a releer (pasadas siguientes). Devuelve los
    índices de las páginas en las que ha habido coincidencias.
    """
    policy = session.config.ocr
    if not policy.enabled:
        return set()
    matched: set[int] = set()
    with session.view() as view:
        reasons = {
            page.number: needs_ocr(page, session)
            for page in view
            if only is None or page.number in only
        }
        recognized = {n: ocr_page(view[n], policy) for n, reason in reasons.items() if reason}

    for number, lines in recognized.items():
        page = session.doc[number]
        mode = page_mode(page, session.detection)
        hits = _hits(lines, session, number + 1)
        stats = session.report.ocr.setdefault(
            number + 1, {"motivo": reasons[number], "lineas": len(lines), "coincidencias": 0, "pasadas": 0}
        )
        stats["pasadas"] += 1
        stats["coincidencias"] += len(hits)
        if not hits:
            continue
        matched.add(number)

        if mode == "scanned":
            fill = FILLS[session.policy.fill]
            for hit in hits:
                page.add_redact_annot(hit.rect, fill=fill if fill is not None else False, cross_out=False)
            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                text=pymupdf.PDF_REDACT_TEXT_REMOVE,
            )
        else:
            to_pdf = ~page.transformation_matrix
            areas = []
            for hit in hits:
                grow = policy.margin * hit.line.height
                r = ((hit.rect + (-grow, -grow, grow, grow)) * to_pdf).normalize()
                areas.append((r.x0, r.y0, r.x1, r.y1))
            session.add_line_art(number, areas)

        for hit in hits:
            value = session.namer.name(hit.rule.replacement, hit.original, hit.rule, exact=False)
            insert_replacement(
                page,
                value,
                hit.start_origin,
                hit.line.direction,
                hit.line.height * 0.9,
                hit.rect,
                session.policy,
            )
            session.report.replacements.append(
                Replacement(
                    number + 1,
                    "ocr",
                    hit.rule.id,
                    hit.rule.category.value,
                    hit.original,
                    value,
                    [tuple(hit.rect)],
                )
            )
    return matched


def ocr_passes(session: Session) -> None:
    """Primera pasada en todas las páginas que lo necesitan; las siguientes,
    solo en las que tuvieron coincidencias, tras aplicar lo encontrado."""
    pages = ocr_redact(session)
    for _ in range(session.config.ocr.passes - 1):
        if not pages:
            break
        session.flush_line_art()
        pages = ocr_redact(session, only=pages)
