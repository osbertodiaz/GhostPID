"""Fase 8 · Autocorrección de los restos que encuentra la auditoría por OCR.

El OCR no lee igual dos veces la misma página: a veces la auditoría encuentra
un nombre que el OCR del anonimizado no vio. Esos hallazgos traen página,
rectángulo, dirección y altura de la línea, así que se pueden corregir igual
que en la fase 5:

- página escaneada: se blanquean los píxeles bajo el resto;
- texto en trazos: se quitan los trazos que quedan dentro del área;

y se escribe la denominación genérica (la misma que en el mapa, si el original
ya estaba en él).

Solo se corrigen hallazgos de OCR con coordenadas y gravedad "error". Un resto
en la capa de texto indica una regla incompleta (p. ej. un término dentro de
otra palabra): corregirlo a ciegas dejaría texto roto, así que se informa.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pymupdf

from sanitizer.audit import Finding
from sanitizer.redact import FILLS, Namer, Session, find_matches, insert_replacement, ocr_normalize, page_mode

# Ampliación del área de un resto al quitar trazos, en alturas de letra
MARGIN = 0.25


def repairable(findings: list[Finding]) -> list[Finding]:
    """Restos de OCR con coordenadas que no corresponden a texto real.

    Si en la misma zona hay un resto en la capa de texto, lo visible es texto
    real: el problema es de reglas y no se corrige a ciegas.
    """
    text_rects = [
        (f.page, pymupdf.Rect(f.rect)) for f in findings if f.check == "texto" and f.page and f.rect
    ]
    out = []
    for f in findings:
        if f.severity != "error" or f.check != "ocr" or not f.page or not f.rect:
            continue
        rect = pymupdf.Rect(f.rect)
        if any(page == f.page and rect.intersects(r) for page, r in text_rects):
            continue
        out.append(f)
    return out


def _replacement(config: Any, namer: Namer, finding: Finding) -> str:
    """Denominación genérica para un resto: la del mapa si el original ya estaba,
    la de la regla que encaja si no, y vacía (solo borrar) en último caso."""
    def ocr_key(text: str) -> str:
        return " ".join(ocr_normalize(text).split())

    key = ocr_key(finding.text)
    for value, entry in namer.entries.items():
        if value and any(ocr_key(o) == key for o in entry["originals"]):
            # Variante de OCR de un original conocido: misma denominación
            namer.alias(entry["template"], finding.text, value)
            return namer.name(entry["template"], finding.text, exact=False)
    matches, _ = find_matches(config, finding.text, relaxed=True, fuzzy=True)
    if matches:
        rule = matches[0].rule
        return namer.name(rule.replacement, finding.text, rule, exact=False)
    if finding.where.startswith("patrón '"):
        rule_id = finding.where.split("'")[1]
        rule = next((r for r in config.substitutions if r.id == rule_id), None)
        if rule is not None:
            return namer.name(rule.replacement, finding.text, rule, exact=False)
    return ""


def _origin(rect: pymupdf.Rect, direction: tuple[float, float]) -> tuple[float, float]:
    """Comienzo de la línea base según la dirección del texto (coordenadas sin rotar)."""
    dx, dy = direction
    if abs(dx) >= abs(dy):
        return (rect.x0, rect.y1) if dx >= 0 else (rect.x1, rect.y0)
    return (rect.x1, rect.y1) if dy < 0 else (rect.x0, rect.y0)


def repair(src: str | Path, dst: str | Path, config: Any, findings: list[Finding], namer: Namer) -> set[int]:
    """Corrige los restos de OCR de `src` y guarda en `dst`; devuelve los índices de página tocados."""
    todo = repairable(findings)
    pages: set[int] = set()
    with Session(src, config, namer=namer) as session:
        session.restore_layers()  # se trabaja sobre la página tal como se ve
        by_page: dict[int, list[Finding]] = {}
        for f in todo:
            by_page.setdefault(f.page - 1, []).append(f)
        for number, items in by_page.items():
            page = session.doc[number]
            mode = page_mode(page, session.detection)
            fill = FILLS[session.policy.fill]
            areas = []
            # Bajo el área no puede haber texto real (sería un problema de reglas)
            items = [f for f in items if not page.get_text("text", clip=pymupdf.Rect(f.rect)).strip()]
            if not items:
                continue
            for f in items:
                rect = pymupdf.Rect(f.rect)
                if mode == "scanned":
                    page.add_redact_annot(rect, fill=fill if fill is not None else False, cross_out=False)
                else:
                    grow = MARGIN * (f.height or rect.height)
                    r = ((rect + (-grow, -grow, grow, grow)) * ~page.transformation_matrix).normalize()
                    areas.append((r.x0, r.y0, r.x1, r.y1))
            if mode == "scanned":
                page.apply_redactions(
                    images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                    graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                    text=pymupdf.PDF_REDACT_TEXT_REMOVE,
                )
                session.pixel_pages.add(number)
            if areas:
                session.add_line_art(number, areas)
            for f in items:
                rect = pymupdf.Rect(f.rect)
                direction = f.direction or (1.0, 0.0)
                value = _replacement(config, namer, f)
                insert_replacement(
                    page,
                    value,
                    _origin(rect, direction),
                    direction,
                    (f.height or rect.height) * 0.9,
                    rect,
                    session.policy,
                )
            pages.add(number)
        session.save(dst)
    return pages
