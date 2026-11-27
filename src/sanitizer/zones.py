"""Fase 5 · Zonas: eliminación de rectángulos enteros según la plantilla del cliente.

Cada zona de la config (cajetín, bloques laterales, logos vectoriales…) se
resuelve sobre la página tal como se ve (ya rotada) y se elimina todo lo que
contiene:

- el texto, con `apply_redactions` (también el de capas ocultas y el que está
  dentro de Form XObjects);
- los píxeles de las imágenes que la tocan;
- los trazos que están enteros dentro, con el editor preciso de `lineart`
  (una línea que solo entra en la zona, como el marco del plano, se conserva).

Las anotaciones que tocan una zona también desaparecen. Opcionalmente se
escribe una etiqueta (`label`) para que quien lea el plano sepa que ahí había
algo.
"""

from __future__ import annotations

import pymupdf

from sanitizer.redact import FILLS, Session, insert_replacement

LABEL_SIZE = 8.0


def apply_zones(session: Session) -> int:
    """Elimina el contenido de todas las zonas; devuelve cuántas se han aplicado."""
    config, policy, doc = session.config, session.policy, session.doc
    applied = 0
    for page in doc:
        width, height = page.rect.width, page.rect.height
        hits = []
        for zone in config.zones:
            if not zone.applies_to(page.number + 1, doc.page_count, width, height):
                continue
            view = pymupdf.Rect(zone.resolve(width, height))
            if view.is_empty:
                continue
            hits.append((zone, view, (view * page.derotation_matrix).normalize()))
        if not hits:
            continue

        fill = FILLS[policy.fill]
        for _, _, rect in hits:
            page.add_redact_annot(rect, fill=fill if fill is not None else False, cross_out=False)
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE,
        )

        to_pdf = ~page.transformation_matrix
        areas = []
        for zone, view, rect in hits:
            r = (rect * to_pdf).normalize()
            areas.append((r.x0, r.y0, r.x1, r.y1))
            session.cleared[page.number + 1].append(rect)
            session.report.zones.append({"page": page.number + 1, "zone": zone.id, "rect": tuple(rect)})
            if zone.label:
                _write_label(page, zone.label, view, policy)
            applied += 1
        session.add_line_art(page.number, areas)
    return applied


def _write_label(page: pymupdf.Page, label: str, view: pymupdf.Rect, policy) -> None:
    """Etiqueta horizontal (tal como se ve) en la esquina superior izquierda de la zona."""
    derot = page.derotation_matrix
    origin = pymupdf.Point(view.x0 + 2, view.y0 + LABEL_SIZE + 2) * derot
    direction = (pymupdf.Point(1, 0) * derot) - (pymupdf.Point(0, 0) * derot)
    area = (pymupdf.Rect(view.x0 + 2, view.y0, view.x1 - 2, view.y1) * derot).normalize()
    insert_replacement(page, label, tuple(origin), (direction.x, direction.y), LABEL_SIZE, area, policy)
