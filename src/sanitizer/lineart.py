"""Eliminación precisa de trazos bajo un área, editando el stream de contenido.

`apply_redactions` de MuPDF decide si un trazo está "cubierto" o "tocado" por
su caja ampliada con el límite de inglete (10 × grosor), no por su geometría:
o no borra las letras SHX diagonales, o se lleva también globos, tuberías y
letras de las líneas vecinas. Aquí se recorren los operadores de trazado con
pikepdf, siguiendo la matriz de transformación (CTM), y se eliminan solo los
**subtrazos** (de un `m` al siguiente, o un `re`) cuyos puntos están todos
dentro de alguna de las áreas. El resto del trazo se conserva.

Limitaciones conocidas (se informan, no se ignoran):

- Solo se edita el contenido de la página; los trazos dentro de Form XObjects
  no se tocan (podrían estar compartidos con otras páginas).
- Los trazos que definen recortes (`W`/`W*`) nunca se eliminan.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pikepdf

Rect = tuple[float, float, float, float]  # x0, y0, x1, y1 en espacio PDF

_PAINT = {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "n"}
_CONSTRUCT = {"m", "l", "c", "v", "y", "h", "re"}
_TOLERANCE = 0.5  # pt


@dataclass
class LineArtResult:
    removed_subpaths: int = 0
    xobject_paths: bool = False  # hay trazos en Form XObjects que no se han revisado
    notes: list[str] = field(default_factory=list)


def _mul(m: tuple, n: tuple) -> tuple:
    """Producto de matrices PDF (a b c d e f): primero m, luego n."""
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (
        a * A + b * C,
        a * B + b * D,
        c * A + d * C,
        c * B + d * D,
        e * A + f * C + E,
        e * B + f * D + F,
    )


def _apply(m: tuple, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def _segment_hits(p: tuple[float, float], q: tuple[float, float], area: Rect) -> bool:
    """¿El segmento p–q cruza el rectángulo? (Liang–Barsky)"""
    x0, y0, x1, y1 = area
    dx, dy = q[0] - p[0], q[1] - p[1]
    t0, t1 = 0.0, 1.0
    for edge_p, edge_q in ((-dx, p[0] - x0), (dx, x1 - p[0]), (-dy, p[1] - y0), (dy, y1 - p[1])):
        if edge_p == 0:
            if edge_q < 0:
                return False
            continue
        t = edge_q / edge_p
        if edge_p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return False
    return True


def _inside(points: list[tuple[float, float]], areas: list[Rect], touched: bool) -> bool:
    """covered: todos los puntos dentro. touched: algún tramo cruza el área.

    En las curvas se usa el polígono de control, que las contiene.
    """
    if not points:
        return False
    t = _TOLERANCE
    for x0, y0, x1, y1 in areas:
        grown = (x0 - t, y0 - t, x1 + t, y1 + t)
        if not touched:
            if all(grown[0] <= x <= grown[2] and grown[1] <= y <= grown[3] for x, y in points):
                return True
        elif len(points) == 1:
            x, y = points[0]
            if grown[0] <= x <= grown[2] and grown[1] <= y <= grown[3]:
                return True
        elif any(_segment_hits(a, b, grown) for a, b in zip(points, points[1:])):
            return True
    return False


def remove_paths(
    pdf: pikepdf.Pdf, page_index: int, areas: list[Rect], touched: bool = False
) -> LineArtResult:
    """Elimina de la página los subtrazos dentro de `areas` (o que las tocan)."""
    result = LineArtResult()
    if not areas:
        return result
    page = pdf.pages[page_index]
    page.contents_coalesce()
    instructions = pikepdf.parse_content_stream(page)

    out: list = []
    ctm = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    stack: list[tuple] = []
    in_text = False
    # Trazo en construcción: lista de subtrazos (instrucciones, puntos por los
    # que pasa el trazo, todos los puntos incluidos los de control)
    subpaths: list[tuple[list, list[tuple[float, float]], list[tuple[float, float]]]] = []
    clip_ops: list = []  # W / W*: van entre la construcción del trazo y su pintado

    def current() -> tuple[list, list, list]:
        if not subpaths:
            subpaths.append(([], [], []))
        return subpaths[-1]

    for inst in instructions:
        operands, operator = list(inst.operands), str(inst.operator)
        if in_text:
            out.append(inst)
            if operator == "ET":
                in_text = False
            continue

        if operator == "BT":
            in_text = True
            out.append(inst)
        elif operator == "q":
            stack.append(ctm)
            out.append(inst)
        elif operator == "Q":
            ctm = stack.pop() if stack else ctm
            out.append(inst)
        elif operator == "cm":
            ctm = _mul(tuple(float(v) for v in operands), ctm)
            out.append(inst)
        elif operator in _CONSTRUCT:
            nums = [float(v) for v in operands]
            if operator in ("m", "re"):
                subpaths.append(([], [], []))
            ops, on_curve, all_pts = current()
            ops.append(inst)
            if operator == "re":
                x, y, w, h = nums
                corners = [_apply(ctm, px, py) for px, py in ((x, y), (x + w, y), (x, y + h), (x + w, y + h))]
                on_curve += corners
                all_pts += corners
            elif operator != "h":
                pts = [_apply(ctm, nums[i], nums[i + 1]) for i in range(0, len(nums), 2)]
                # En las curvas (c, v, y) solo el último punto está sobre el
                # trazo; los de control pueden sobresalir del área de la letra
                on_curve.append(pts[-1])
                all_pts += pts
        elif operator in ("W", "W*"):
            clip_ops.append(inst)
        elif operator in _PAINT:
            kept = []
            for ops, on_curve, all_pts in subpaths:
                # Los trazos de recorte nunca se eliminan
                pts = all_pts if touched else on_curve
                if not clip_ops and operator != "n" and _inside(pts, areas, touched):
                    result.removed_subpaths += 1
                else:
                    kept.extend(ops)
            if kept or clip_ops:
                out.extend(kept)
                out.extend(clip_ops)
                out.append(inst)
            subpaths = []
            clip_ops = []
        else:
            if operator == "Do":
                xobj = page.Resources.get("/XObject", {}).get(operands[0]) if "/Resources" in page else None
                if xobj is not None and xobj.get("/Subtype") == "/Form":
                    result.xobject_paths = True
            out.append(inst)

    if subpaths or clip_ops:
        result.notes.append("trazo sin pintar al final del contenido: se conserva tal cual")
        for ops, _, _ in subpaths:
            out.extend(ops)
        out.extend(clip_ops)

    page.Contents = pdf.make_stream(pikepdf.unparse_content_stream(out))
    return result
