"""Fase 5 · Logos e imágenes repetidas.

Las imágenes se agrupan por un hash perceptual (dHash de 64 bits) que no
cambia si la imagen está girada 90/180/270° ni si es una variante en gris de
la misma imagen en color: en un set real, el mismo sello aparecía en tres
variantes y el mismo logo en color y en gris.

Se eliminan los grupos que:

- figuran en `logos.remove` (por hash), o
- con `remove_repeated`, aparecen en `min_pages` páginas o más y ocupan como
  máximo `max_area` de la página (así nunca se toca un escaneado a página
  completa), salvo que figuren en `logos.keep`.

Eliminar = sustituir la imagen por una transparente de 1×1 en todo el
documento (`Page.delete_image`). El informe da el hash de cada grupo para
poder fijarlo en la config del cliente.

Los logos vectoriales no tienen una firma fiable: se tratan con zonas.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pymupdf
from PIL import Image
from pydantic import ConfigDict

MIN_IMAGE_SIDE = 8  # px; por debajo no tiene sentido hablar de logo


@dataclass(frozen=True)
class LogoPolicy:
    __pydantic_config__ = ConfigDict(extra="forbid")

    remove_repeated: bool = True
    min_pages: int = 3
    max_area: float = 0.15  # fracción de la página
    hash_distance: int = 6  # bits distintos (de 64) para considerar dos imágenes iguales
    remove: list[str] = field(default_factory=list)  # hashes (hex) que se eliminan siempre
    keep: list[str] = field(default_factory=list)  # hashes (hex) que no se eliminan nunca


def dhash(img: Image.Image) -> int:
    """Hash de diferencias de 64 bits sobre una miniatura en gris de 9×8."""
    small = img.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    px = small.tobytes()  # 1 byte por píxel en modo "L"
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return bits


def canonical_hash(img: Image.Image) -> int:
    """El menor dHash de las cuatro orientaciones: igual para la imagen girada."""
    return min(dhash(img.rotate(angle, expand=True)) for angle in (0, 90, 180, 270))


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _image(doc: pymupdf.Document, xref: int) -> Image.Image | None:
    """La imagen como PIL (gris o RGB), o None si no se puede decodificar."""
    try:
        pix = pymupdf.Pixmap(doc, xref)
        if pix.width < MIN_IMAGE_SIDE or pix.height < MIN_IMAGE_SIDE:
            return None
        if pix.colorspace is None:
            # Máscara o imagen solo-alfa (sin espacio de color): se usa tal cual
            return Image.frombytes("L", (pix.width, pix.height), bytes(pix.samples)[:: pix.n])
        if pix.alpha:
            pix = pymupdf.Pixmap(pix, 0)
        if pix.n not in (1, 3):
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
        mode = "L" if pix.n == 1 else "RGB"
        return Image.frombytes(mode, (pix.width, pix.height), pix.samples)
    except Exception:  # imagen que MuPDF no sabe decodificar o convertir
        return None


def find_image_groups(doc: pymupdf.Document, policy: LogoPolicy) -> list[dict[str, Any]]:
    """Grupos de imágenes perceptualmente iguales, con su decisión."""
    usage: dict[int, dict[str, Any]] = {}
    for page in doc:
        page_area = page.rect.width * page.rect.height or 1.0
        for info in page.get_images(full=True):
            xref = info[0]
            entry = usage.setdefault(xref, {"pages": set(), "area": 0.0, "size": (info[2], info[3])})
            entry["pages"].add(page.number + 1)
            for rect in page.get_image_rects(xref):
                visible = rect & page.rect
                if not visible.is_empty:
                    entry["area"] = max(entry["area"], visible.width * visible.height / page_area)

    hashes = {}
    for xref in usage:
        img = _image(doc, xref)
        if img is not None:
            hashes[xref] = canonical_hash(img)

    # Agrupación por distancia de Hamming (unión de conjuntos)
    parent = {x: x for x in hashes}

    def root(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    xrefs = sorted(hashes)
    for i, a in enumerate(xrefs):
        for b in xrefs[i + 1 :]:
            if hamming(hashes[a], hashes[b]) <= policy.hash_distance:
                parent[root(a)] = root(b)

    groups: dict[int, list[int]] = {}
    for x in xrefs:
        groups.setdefault(root(x), []).append(x)

    keep = {int(h, 16) for h in policy.keep}
    remove = {int(h, 16) for h in policy.remove}
    out = []
    for members in groups.values():
        pages = sorted(set().union(*(usage[x]["pages"] for x in members)))
        area = max(usage[x]["area"] for x in members)
        group_hashes = [hashes[x] for x in members]
        near = lambda targets: any(  # noqa: E731
            hamming(h, t) <= policy.hash_distance for h in group_hashes for t in targets
        )
        if near(keep):
            decision, reason = False, "en logos.keep"
        elif near(remove):
            decision, reason = True, "en logos.remove"
        elif policy.remove_repeated and len(pages) >= policy.min_pages and area <= policy.max_area:
            decision, reason = True, f"repetida en {len(pages)} páginas"
        else:
            decision, reason = False, ""
        out.append(
            {
                "hash": f"{group_hashes[0]:016x}",
                "xrefs": members,
                "pages": pages,
                "size": usage[members[0]]["size"],
                "max_area": round(area, 4),
                "removed": decision,
                "reason": reason,
            }
        )
    return sorted(out, key=lambda g: (-len(g["pages"]), g["hash"]))


def remove_logos(session: Any) -> int:
    """Elimina las imágenes de los grupos marcados; devuelve cuántas imágenes."""
    doc = session.doc
    groups = find_image_groups(doc, session.config.logos)
    removed = 0
    for group in groups:
        if not group["removed"]:
            continue
        for xref in group["xrefs"]:
            page = next(p for p in doc if any(info[0] == xref for info in p.get_images(full=True)))
            page.delete_image(xref)
            removed += 1
    session.report.logos = groups
    return removed
