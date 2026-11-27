"""Fase 4 · Redacción de texto.

Aplica las reglas de una config (lista blanca y sustituciones) al texto del PDF:

1. Se extraen los caracteres con sus coordenadas y se unen por bloque, de modo
   que un término de varias palabras o partido en dos líneas también encaja.
2. Se marcan los tramos protegidos: coincidencias de la lista blanca y tags
   "apilados" (en un globo de instrumento las letras y el número de lazo son
   palabras distintas, `TI` sobre `01234`; si `TI-01234` encaja con la lista
   blanca, ambas palabras quedan protegidas).
3. Cada coincidencia de una sustitución que no toque nada protegido se
   convierte en rectángulos (uno por línea) y se redacta con
   `apply_redactions`, que elimina el texto real. Si toca algo protegido se
   bloquea y se informa: la lista blanca tiene prioridad absoluta.
4. Se escribe la denominación genérica (EPC, VENDOR-1…) en el sitio del
   original, con su misma orientación.

Qué se elimina además del texto depende del tipo de página (configurable):

- texto normal: solo el texto (las líneas del plano que cruzan una etiqueta se
  conservan);
- texto invisible sobre trazos (AutoCAD/SHX): también los trazos que quedan
  completamente cubiertos, que son lo que se ve;
- escaneado con capa OCR: también los píxeles de la imagen bajo el texto.

El texto de las capas ocultas también se procesa: se activan todas las capas
para extraerlo y se restaura su estado antes de guardar. Las mismas reglas se
aplican al contenido de las anotaciones y a los valores de los formularios.

Todas las coincidencias se localizan antes de redactar nada: si un cajetín es
un Form XObject compartido por varias páginas, redactarlo en una lo cambia en
todas, y así ninguna página se queda sin su texto de sustitución.

La numeración {n} es coherente dentro del documento: el mismo valor original
recibe siempre la misma denominación (base del mapa de la fase 6).
"""

from __future__ import annotations

import argparse
import collections
import io
import json
import math
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import pikepdf
import pymupdf
from pydantic import ConfigDict

from sanitizer.inventory import DetectionParams, image_coverage, text_visibility
from sanitizer.lineart import remove_paths

# Texto con coordenadas: se conservan espacios y ligaduras, y no se recorta al
# MediaBox (el texto fuera de la página no se ve, pero se extrae igual)
TEXT_FLAGS = pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_PRESERVE_LIGATURES

GRAPHICS = {
    "none": pymupdf.PDF_REDACT_LINE_ART_NONE,
    "covered": pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
    "touched": pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
}
IMAGES = {
    "none": pymupdf.PDF_REDACT_IMAGE_NONE,
    "pixels": pymupdf.PDF_REDACT_IMAGE_PIXELS,
    "remove": pymupdf.PDF_REDACT_IMAGE_REMOVE,
}
FILLS = {"white": (1, 1, 1), "black": (0, 0, 0), "none": None}


# Partes de un tag apilado en un globo de instrumento
_STACK_LETTERS = re.compile(r"^[A-Z]{1,5}$")
_STACK_NUMBER = re.compile(r"^\d{2,6}[A-Z]{0,2}$")


@dataclass(frozen=True)
class RedactionMode:
    """Qué se elimina, además del texto, bajo cada redacción."""

    __pydantic_config__ = ConfigDict(extra="forbid")

    graphics: Literal["none", "covered", "touched"]
    images: Literal["none", "pixels", "remove"]


@dataclass(frozen=True)
class RedactionPolicy:
    __pydantic_config__ = ConfigDict(extra="forbid")

    # Relleno del hueco: blanco (por defecto), ninguno o negro
    fill: Literal["white", "none", "black"] = "white"
    font: str = "helv"
    min_font_size: float = 2.0
    # Por tipo de página (ver page_mode). Al configurarlos en YAML hay que dar
    # graphics e images a la vez.
    text: RedactionMode = field(default_factory=lambda: RedactionMode("none", "none"))
    overlay: RedactionMode = field(default_factory=lambda: RedactionMode("covered", "none"))
    scanned: RedactionMode = field(default_factory=lambda: RedactionMode("none", "pixels"))
    hidden_layers: Literal["apply_rules", "skip"] = "apply_rules"
    annotations: bool = True
    stacked_tags: bool = True
    # En páginas escaneadas el OCR pega palabras ("ACMEPLANT"): los términos
    # se buscan también dentro de otras palabras. Redactar de más en una capa
    # OCR invisible es preferible a dejar una fuga.
    ocr_relaxed_terms: bool = True
    # Separación vertical máxima entre letras y número de un tag apilado, en
    # alturas de línea, y desalineación horizontal máxima de sus centros
    stacked_max_gap: float = 1.6
    stacked_max_offset: float = 0.6


DEFAULT_REDACTION = RedactionPolicy()


# --------------------------------------------------------------------------
# Resultado
# --------------------------------------------------------------------------


@dataclass
class Replacement:
    page: int
    source: Literal["page", "annotation", "form"]
    rule: str
    category: str
    original: str
    replacement: str
    rects: list[tuple[float, float, float, float]] = field(default_factory=list)


@dataclass
class Blocked:
    """Coincidencia de una sustitución que tocaba texto protegido."""

    page: int
    source: str
    rule: str
    text: str
    protected_by: str


@dataclass
class RedactionReport:
    replacements: list[Replacement] = field(default_factory=list)
    blocked: list[Blocked] = field(default_factory=list)
    page_modes: dict[int, str] = field(default_factory=dict)
    stacked_tags: int = 0
    # denominación genérica → valores originales (base del mapa de la fase 6)
    mapping: dict[str, list[str]] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    # Avisos que no son fugas (p. ej. trazos borrados de más)
    warnings: list[str] = field(default_factory=list)
    # Coincidencias "rozadas": un patrón encaja si se ignoran los límites de
    # token, pero no se ha sustituido (p. ej. "REF-001/002"). Suelen indicar
    # una regla que hay que ampliar.
    near_misses: list[Blocked] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.replacements:
            out[r.rule] = out.get(r.rule, 0) + 1
        return out

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------
# Coincidencias sobre texto plano
# --------------------------------------------------------------------------


class Namer:
    """Asigna denominaciones genéricas coherentes dentro de un documento.

    El contador va por plantilla, no por regla: dos reglas con VENDOR-{n} no
    pueden dar VENDOR-1 a dos fabricantes distintos.
    """

    def __init__(self) -> None:
        self._values: dict[tuple[str, str], str] = {}
        self._counters: dict[str, int] = {}
        self.mapping: dict[str, list[str]] = {}

    def name(self, template: str, original: str) -> str:
        key = (template, normalize(original))
        if key not in self._values:
            if "{n}" in template:
                n = self._counters.get(template, 0) + 1
                self._counters[template] = n
                self._values[key] = template.format(n=n)
            else:
                self._values[key] = template
        value = self._values[key]
        originals = self.mapping.setdefault(value, [])
        if original not in originals:
            originals.append(original)
        return value


def normalize(text: str) -> str:
    return " ".join(text.split()).casefold()


@dataclass(frozen=True)
class Match:
    start: int
    end: int
    rule: Any  # SubstitutionRule


def find_matches(
    config: Any,
    text: str,
    extra_protected: list[tuple[int, int, str]] = (),
    relaxed: bool = False,
) -> tuple[list[Match], list[tuple[int, int, Any, str]]]:
    """Coincidencias de sustitución aceptadas y bloqueadas en `text`.

    Entre coincidencias que se solapan gana la que empieza antes y, a igualdad,
    la más larga. Una coincidencia que toca un tramo protegido se bloquea.
    """
    protected = [(m.start(), m.end(), wl.id) for wl in config.whitelist for m in wl.regex.finditer(text)]
    protected += list(extra_protected)

    candidates = []
    for order, rule in enumerate(config.substitutions):
        for rx in rule.relaxed_matchers if relaxed else rule.matchers:
            for m in rx.finditer(text):
                if m.end() > m.start():
                    candidates.append((m.start(), -(m.end() - m.start()), order, m.end(), rule))
    candidates.sort(key=lambda c: c[:3])

    accepted: list[Match] = []
    blocked: list[tuple[int, int, Any, str]] = []
    for start, _, _, end, rule in candidates:
        if any(start < a.end and a.start < end for a in accepted):
            continue
        clash = next((pid for ps, pe, pid in protected if start < pe and ps < end), None)
        if clash:
            if not any(b[0] == start and b[1] == end for b in blocked):
                blocked.append((start, end, rule, clash))
            continue
        accepted.append(Match(start, end, rule))
    return accepted, blocked


def near_misses(config: Any, text: str, accepted: list[Match]) -> list[tuple[str, Any]]:
    """Coincidencias de términos y patrones sin límites que no se han sustituido."""
    from sanitizer.rules import term_regex  # rules importa este módulo

    out = []
    for rule in config.substitutions:
        flags = 0 if rule.case_sensitive else re.IGNORECASE
        raw = [term_regex(t, word_boundaries=False) for t in rule.terms]
        if rule.boundary == "token":
            raw += rule.patterns
        for pattern in raw:
            for m in re.finditer(pattern, text, flags):
                if m.end() > m.start() and not any(m.start() < a.end and a.start < m.end() for a in accepted):
                    a, b = max(0, m.start() - 10), min(len(text), m.end() + 10)
                    out.append((text[a:b].replace("\n", " "), rule))
    return out


def substitute_text(
    config: Any, text: str, namer: Namer
) -> tuple[str, list[tuple[Match, str]], list[tuple[int, int, Any, str]]]:
    """Aplica las sustituciones a un texto sin geometría (anotaciones, formularios)."""
    matches, blocked = find_matches(config, text)
    out = text
    done = []
    for m in sorted(matches, key=lambda m: m.start, reverse=True):
        value = namer.name(m.rule.replacement, text[m.start : m.end])
        out = out[: m.start] + value + out[m.end :]
        done.append((m, value))
    return out, list(reversed(done)), blocked


# --------------------------------------------------------------------------
# Texto de página con coordenadas
# --------------------------------------------------------------------------


@dataclass
class _Char:
    c: str
    bbox: pymupdf.Rect | None  # None en los saltos de línea añadidos
    line: int
    size: float
    origin: tuple[float, float] | None
    direction: tuple[float, float]

    def core(self) -> pymupdf.Rect:
        """Franja central del carácter, para quitar el texto sin tocar líneas vecinas.

        En los planos el interlineado es apretado y las cajas de caracteres de
        líneas contiguas se solapan; MuPDF elimina cualquier carácter cuya caja
        toque la redacción. La franja entre el 35 % y el 50 % de la altura de
        letra sobre la línea base queda dentro de la caja del propio carácter y
        no alcanza a las líneas vecinas, ni a textos que se solapan en el plano.
        """
        dx, dy = self.direction
        ux, uy = dy, -dx  # vector "hacia arriba" de la letra
        ox, oy = self.origin
        a, b = 0.35 * self.size, 0.5 * self.size
        if abs(dx) >= 0.99:
            y0, y1 = sorted((oy + uy * a, oy + uy * b))
            return pymupdf.Rect(self.bbox.x0, y0, self.bbox.x1, y1)
        if abs(dy) >= 0.99:
            x0, x1 = sorted((ox + ux * a, ox + ux * b))
            return pymupdf.Rect(x0, self.bbox.y0, x1, self.bbox.y1)
        return pymupdf.Rect(self.bbox)  # texto inclinado: caja completa


def _blocks(page: pymupdf.Page) -> list[tuple[str, list[_Char]]]:
    """Texto de cada bloque y, por cada carácter, su caja y su línea."""
    out = []
    raw = page.get_text("rawdict", flags=TEXT_FLAGS)
    line_id = 0
    for block in raw["blocks"]:
        if block.get("type") != 0:
            continue
        chars: list[_Char] = []
        for i, line in enumerate(block["lines"]):
            if i:
                chars.append(_Char("\n", None, line_id, 0.0, None, (1, 0)))
            line_id += 1
            direction = tuple(line["dir"])
            for span in line["spans"]:
                for ch in span["chars"]:
                    chars.append(
                        _Char(
                            ch["c"],
                            pymupdf.Rect(ch["bbox"]),
                            line_id,
                            span["size"],
                            tuple(ch["origin"]),
                            direction,
                        )
                    )
        if chars:
            out.append(("".join(c.c for c in chars), chars))
    return out


def _char_key(c: _Char) -> tuple:
    return (c.c, round(c.bbox.x0, 1), round(c.bbox.y0, 1))


def _visible_chars(page: pymupdf.Page) -> set[tuple]:
    return {_char_key(c) for _, chars in _blocks(page) for c in chars if c.bbox is not None}


def _stacked_tag_rects(page: pymupdf.Page, config: Any, policy: RedactionPolicy) -> list[tuple[pymupdf.Rect, str]]:
    """Rectángulos de tags apilados (letras sobre número) que encajan con la lista blanca."""
    words = page.get_text("words", flags=TEXT_FLAGS)
    to_view = page.rotation_matrix  # la geometría "arriba/abajo" es la que se ve
    letters, numbers = [], []
    for w in words:
        rect = pymupdf.Rect(w[:4])
        view = rect * to_view
        if _STACK_LETTERS.match(w[4]):
            letters.append((w[4], rect, view))
        elif _STACK_NUMBER.match(w[4]):
            numbers.append((w[4], rect, view))

    found = []
    for ltext, lrect, lview in letters:
        h = lview.height or 1.0
        for ntext, nrect, nview in numbers:
            gap = nview.y0 - lview.y1
            if not (-0.3 * h <= gap <= policy.stacked_max_gap * h):
                continue
            offset = abs((nview.x0 + nview.x1) / 2 - (lview.x0 + lview.x1) / 2)
            if offset > policy.stacked_max_offset * max(lview.width, nview.width, h):
                continue
            for candidate in (f"{ltext}-{ntext}", f"{ltext}{ntext}"):
                rule = next((wl for wl in config.whitelist if wl.regex.fullmatch(candidate)), None)
                if rule:
                    found.append((lrect, f"apilado:{rule.id}"))
                    found.append((nrect, f"apilado:{rule.id}"))
                    break
    return found


def _protected_chars(chars: list[_Char], stacked: list[tuple[pymupdf.Rect, str]]) -> list[tuple[int, int, str]]:
    extra = []
    for i, ch in enumerate(chars):
        if ch.bbox is None:
            continue
        centre = pymupdf.Point((ch.bbox.x0 + ch.bbox.x1) / 2, (ch.bbox.y0 + ch.bbox.y1) / 2)
        for rect, rid in stacked:
            if centre in rect:
                extra.append((i, i + 1, rid))
                break
    return extra


@dataclass
class _Planned:
    """Redacción decidida para una coincidencia en una página."""

    replacement: Replacement
    core_rects: list[pymupdf.Rect]  # por línea: quitan el texto
    full_rects: list[pymupdf.Rect]  # por línea: quitan trazos o píxeles
    origin: tuple[float, float] | None
    direction: tuple[float, float]
    font_size: float
    hidden: bool  # el original estaba en una capa oculta


def _plan_page(
    page: pymupdf.Page,
    config: Any,
    policy: RedactionPolicy,
    namer: Namer,
    report: RedactionReport,
    visible: set[tuple] | None,
    relaxed: bool = False,
) -> list[_Planned]:
    """Coincidencias de una página. `page` debe ser una copia sin anotaciones."""
    stacked = _stacked_tag_rects(page, config, policy) if policy.stacked_tags else []
    report.stacked_tags += len(stacked) // 2
    planned = []
    for text, chars in _blocks(page):
        matches, blocked = find_matches(config, text, _protected_chars(chars, stacked), relaxed)
        for start, end, rule, wl_id in blocked:
            report.blocked.append(Blocked(page.number + 1, "page", rule.id, text[start:end], wl_id))
        seen = {(n.page, n.text) for n in report.near_misses}
        for context, rule in near_misses(config, text, matches):
            if (page.number + 1, context) not in seen:
                seen.add((page.number + 1, context))
                report.near_misses.append(Blocked(page.number + 1, "page", rule.id, context, "límite de token"))
        for m in matches:
            original = text[m.start : m.end]
            value = namer.name(m.rule.replacement, original)
            span_chars = [c for c in chars[m.start : m.end] if c.bbox is not None and not c.c.isspace()]
            if not span_chars:
                continue
            core: dict[int, pymupdf.Rect] = {}
            full: dict[int, pymupdf.Rect] = {}
            for c in span_chars:
                core[c.line] = core[c.line] | c.core() if c.line in core else c.core()
                full[c.line] = full[c.line] | c.bbox if c.line in full else pymupdf.Rect(c.bbox)
            hidden = visible is not None and not any(_char_key(c) in visible for c in span_chars)
            first = span_chars[0]
            rep = Replacement(
                page.number + 1,
                "page",
                m.rule.id,
                m.rule.category.value,
                original,
                value,
                [tuple(r) for r in full.values()],
            )
            planned.append(
                _Planned(
                    rep, list(core.values()), list(full.values()), first.origin, first.direction, first.size, hidden
                )
            )
    return planned


def page_mode(page: pymupdf.Page, detection: DetectionParams) -> Literal["text", "overlay", "scanned"]:
    """Tipo de página a efectos de redacción (ver inventory.classify_page)."""
    if image_coverage(page) >= detection.image_coverage_threshold:
        return "scanned"
    visible, invisible = text_visibility(page)
    if invisible >= detection.invisible_text_min_chars and invisible > visible:
        return "overlay"
    return "text"


def _apply_page(
    page: pymupdf.Page,
    planned: list[_Planned],
    mode: RedactionMode,
    policy: RedactionPolicy,
    hidden_ocg: int | None,
) -> list[tuple[float, float, float, float]]:
    """Redacta la página; devuelve las áreas (espacio PDF) cuyos trazos hay que quitar."""
    fill = FILLS[policy.fill]
    # 1) Texto: franjas centrales, sin tocar trazos ni imágenes
    for item in planned:
        for rect in item.core_rects:
            page.add_redact_annot(
                rect, fill=fill if fill is not None and not item.hidden else False, cross_out=False
            )
    page.apply_redactions(
        images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE
    )
    # 2) Píxeles bajo la caja completa, sin tocar más texto
    if mode.images != "none":
        for item in planned:
            for rect in item.full_rects:
                page.add_redact_annot(rect, fill=False, cross_out=False)
        page.apply_redactions(
            images=IMAGES[mode.images],
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            text=pymupdf.PDF_REDACT_TEXT_NONE,
        )
    # 3) Los trazos bajo el texto (p. ej. letras SHX) se quitan después, con
    #    precisión, editando el stream de contenido (ver lineart.py)

    color = (1, 1, 1) if policy.fill == "black" else (0, 0, 0)
    for item in planned:
        text = item.replacement.replacement
        if not text or item.origin is None:
            continue
        dx, dy = item.direction
        angle = math.degrees(math.atan2(-dy, dx)) % 360
        first = item.full_rects[0]
        vertical = abs(angle % 180 - 90) < 45
        available = first.height if vertical else first.width
        size = item.font_size or 8.0
        length = pymupdf.get_text_length(text, fontname=policy.font, fontsize=size)
        if length > available > 0:
            size *= available / length
        size = max(size, policy.min_font_size)
        # La sustitución de un texto oculto va también a una capa oculta
        kwargs = {"fontsize": size, "fontname": policy.font, "color": color}
        if item.hidden and hidden_ocg:
            kwargs["oc"] = hidden_ocg
        snapped = round(angle / 90) * 90 % 360
        if min(abs(angle - snapped), 360 - abs(angle - snapped)) < 1:
            page.insert_text(item.origin, text, rotate=snapped, **kwargs)
        else:
            # Texto inclinado: se gira alrededor del origen
            morph = (pymupdf.Point(item.origin), pymupdf.Matrix(-angle))
            page.insert_text(item.origin, text, morph=morph, **kwargs)

    if mode.graphics == "none":
        return []
    to_pdf = ~page.transformation_matrix
    areas = []
    for item in planned:
        for rect in item.full_rects:
            r = (rect * to_pdf).normalize()
            areas.append((r.x0, r.y0, r.x1, r.y1))
    return areas


# --------------------------------------------------------------------------
# Anotaciones y formularios
# --------------------------------------------------------------------------


def _process_annotations(page: pymupdf.Page, config: Any, namer: Namer, report: RedactionReport) -> None:
    number = page.number + 1
    for annot in page.annots():
        info = annot.info
        changed = {}
        for key in ("content", "subject"):
            value = info.get(key) or ""
            if not value:
                continue
            new, done, blocked = substitute_text(config, value, namer)
            for start, end, rule, wl_id in blocked:
                report.blocked.append(Blocked(number, "annotation", rule.id, value[start:end], wl_id))
            for m, rep in done:
                report.replacements.append(
                    Replacement(number, "annotation", m.rule.id, m.rule.category.value, value[m.start : m.end], rep)
                )
            if new != value:
                changed[key] = new
        if changed:
            annot.set_info(**changed)
            # El texto enriquecido duplica el contenido con formato: se descarta
            page.parent.xref_set_key(annot.xref, "RC", "null")
            annot.update()

    for widget in page.widgets():
        value = widget.field_value
        if not isinstance(value, str) or not value:
            continue
        new, done, blocked = substitute_text(config, value, namer)
        for start, end, rule, wl_id in blocked:
            report.blocked.append(Blocked(number, "form", rule.id, value[start:end], wl_id))
        for m, rep in done:
            report.replacements.append(
                Replacement(number, "form", m.rule.id, m.rule.category.value, value[m.start : m.end], rep)
            )
        if new != value:
            widget.field_value = new
            widget.update()


# --------------------------------------------------------------------------
# Capas ocultas
# --------------------------------------------------------------------------


def _open_with_all_layers(path: Path, policy: RedactionPolicy) -> tuple[pymupdf.Document, dict | None]:
    """Abre el PDF con todas las capas visibles; devuelve el estado para restaurarlo."""
    doc = pymupdf.open(path)
    ocgs = list(doc.get_ocgs().keys())
    if not ocgs or policy.hidden_layers == "skip":
        return doc, None
    state = doc.get_layer(-1)
    doc.set_layer(-1, on=ocgs, off=[])
    # MuPDF aplica la nueva visibilidad al reabrir el documento
    data = doc.tobytes()
    doc.close()
    return pymupdf.open("pdf", data), state


def _restore_layers(doc: pymupdf.Document, state: dict | None) -> None:
    if state is None:
        return
    kwargs = {k: state[k] for k in ("basestate", "rbgroups", "locked") if state.get(k) is not None}
    doc.set_layer(-1, on=state.get("on") or [], off=state.get("off") or [], **kwargs)


def _without_annotations(doc: pymupdf.Document) -> pymupdf.Document:
    """Copia del documento sin anotaciones ni widgets.

    `get_text` incluye el texto de la apariencia de las anotaciones: si se
    redactara sobre él, `apply_redactions` borraría la anotación entera. El
    texto de página se busca en esta copia y el de las anotaciones, aparte.
    """
    copy = pymupdf.open("pdf", doc.tobytes())
    # Se quita la lista /Annots a bajo nivel: borrar una a una falla con
    # anotaciones que tienen popups o respuestas encadenadas
    for page in copy:
        copy.xref_set_key(page.xref, "Annots", "null")
    return pymupdf.open("pdf", copy.tobytes())


# --------------------------------------------------------------------------
# Documento completo
# --------------------------------------------------------------------------


def redact_pdf(
    src: str | Path,
    dst: str | Path,
    config: Any,
    policy: RedactionPolicy | None = None,
    detection: DetectionParams | None = None,
) -> RedactionReport:
    """Redacta `src` según `config` y guarda el resultado en `dst`."""
    src, dst = Path(src), Path(dst)
    if src.resolve() == dst.resolve():
        raise ValueError("la salida no puede sobrescribir el PDF original")
    policy = policy or config.redaction
    detection = detection or config.detection

    report = RedactionReport()
    namer = Namer()
    doc, layer_state = _open_with_all_layers(src, policy)
    try:
        plain = _without_annotations(doc)
        # Lo que se ve con las capas en su estado original, para saber qué
        # coincidencias están en contenido oculto
        original = _without_annotations(pymupdf.open(src)) if layer_state is not None else None

        # 1) Planificar todas las páginas antes de tocar ninguna
        plans = []
        protected_before: dict[int, collections.Counter] = {}
        for page in plain:
            mode = page_mode(page, detection)
            report.page_modes[page.number + 1] = mode
            visible = _visible_chars(original[page.number]) if original is not None else None
            relaxed = policy.ocr_relaxed_terms and mode == "scanned"
            plans.append(
                (page.number, mode, _plan_page(page, config, policy, namer, report, visible, relaxed))
            )
            protected_before[page.number + 1] = _protected_counts(page, config)
        plain.close()
        if original is not None:
            original.close()

        hidden_ocg = None
        if any(item.hidden for _, _, planned in plans for item in planned):
            hidden_ocg = doc.add_ocg("redacted-hidden", on=False)
            if layer_state is not None:
                layer_state = {**layer_state, "off": [*(layer_state.get("off") or []), hidden_ocg]}

        # 2) Redactar e insertar las denominaciones genéricas
        line_art: dict[int, tuple[list, bool]] = {}
        for number, mode, planned in plans:
            page = doc[number]
            if planned:
                page_mode_cfg = getattr(policy, mode)
                areas = _apply_page(page, planned, page_mode_cfg, policy, hidden_ocg)
                if areas:
                    line_art[number] = (areas, page_mode_cfg.graphics == "touched")
                report.replacements += [p.replacement for p in planned]
            if policy.annotations:
                _process_annotations(page, config, namer, report)

        _restore_layers(doc, layer_state)
        if not line_art:
            doc.save(dst, garbage=3, deflate=True)
        else:
            # 3) Trazos bajo el texto redactado, editando el contenido con pikepdf
            with pikepdf.open(io.BytesIO(doc.tobytes(garbage=3, deflate=True))) as pdf:
                for number, (areas, touched) in line_art.items():
                    result = remove_paths(pdf, number, areas, touched)
                    if result.xobject_paths:
                        report.warnings.append(
                            f"página {number + 1}: hay trazos dentro de Form XObjects que no se "
                            f"han revisado (revisa visualmente el resultado)"
                        )
                pdf.save(dst)
    finally:
        doc.close()

    report.mapping = namer.mapping
    report.problems = verify_redaction(dst, config, policy, detection)

    # Salvaguarda: ningún dato protegido por la lista blanca debe desaparecer
    after_doc, _ = _open_with_all_layers(dst, policy)
    with after_doc:
        with _without_annotations(after_doc) as plain_after:
            for page in plain_after:
                lost = protected_before.get(page.number + 1, collections.Counter()) - _protected_counts(page, config)
                if lost:
                    items = ", ".join(f"'{k}'" + (f" ×{v}" if v > 1 else "") for k, v in lost.items())
                    report.warnings.append(f"página {page.number + 1}: se han perdido datos protegidos: {items}")
    return report


def _protected_counts(page: pymupdf.Page, config: Any) -> collections.Counter:
    """Coincidencias de la lista blanca en el texto de la página."""
    return collections.Counter(
        m.group(0) for text, _ in _blocks(page) for wl in config.whitelist for m in wl.regex.finditer(text)
    )


def verify_redaction(
    path: str | Path,
    config: Any,
    policy: RedactionPolicy = DEFAULT_REDACTION,
    detection: DetectionParams | None = None,
) -> list[str]:
    """Busca en el PDF redactado coincidencias de sustitución que sigan ahí.

    Mira el texto de todas las páginas (con todas las capas visibles), el
    contenido de las anotaciones y los valores de los formularios.
    """
    problems = []
    doc, _ = _open_with_all_layers(Path(path), policy)
    with doc:
        plain = _without_annotations(doc)
        with plain:
            for page in plain:
                number = page.number + 1
                stacked = _stacked_tag_rects(page, config, policy) if policy.stacked_tags else []
                relaxed = policy.ocr_relaxed_terms and page_mode(page, detection or config.detection) == "scanned"
                for text, chars in _blocks(page):
                    for m in find_matches(config, text, _protected_chars(chars, stacked), relaxed)[0]:
                        problems.append(f"página {number}: queda '{text[m.start:m.end]}' (regla '{m.rule.id}')")
        for page in doc:
            number = page.number + 1
            texts = [(a.info.get("content") or "", "anotación") for a in page.annots()]
            texts += [(w.field_value, "formulario") for w in page.widgets() if isinstance(w.field_value, str)]
            for value, where in texts:
                for m in find_matches(config, value)[0]:
                    problems.append(f"página {number} ({where}): queda '{value[m.start:m.end]}' (regla '{m.rule.id}')")
    return problems


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="redact",
        description="Redacta el texto de un PDF según las reglas de una config YAML.",
    )
    parser.add_argument("pdf", type=Path, help="PDF de entrada")
    parser.add_argument("-o", "--output", type=Path, required=True, help="PDF de salida")
    parser.add_argument("--config", type=Path, required=True, help="config YAML del cliente")
    parser.add_argument(
        "--report",
        type=Path,
        help="JSON con cada sustitución (contiene los valores originales: guárdalo en data/)",
    )
    args = parser.parse_args(argv)

    from sanitizer.rules import ConfigError, load_config  # evita import circular

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        parser.error(f"config inválida: {exc}")

    try:
        report = redact_pdf(args.pdf, args.output, config)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    modes: dict[str, int] = {}
    for mode in report.page_modes.values():
        modes[mode] = modes.get(mode, 0) + 1
    print(f"{args.pdf} → {args.output}")
    print(f"  páginas por tipo: {modes} · tags apilados protegidos: {report.stacked_tags}")
    counts = report.counts()
    print("  sustituciones: " + (", ".join(f"{k} ×{v}" for k, v in counts.items()) or "ninguna"))
    for w in report.warnings:
        print(f"  aviso: {w}")
    if report.near_misses:
        print(f"  coincidencias rozadas (revisa las reglas): {len(report.near_misses)}")
        for n in report.near_misses[:10]:
            print(f"    pág {n.page}: …{n.text}… ('{n.rule}')")
    for b in report.blocked:
        print(f"  BLOQUEADA (pág {b.page}, {b.source}): '{b.text}' de '{b.rule}' toca '{b.protected_by}'")
    if args.report:
        if "data" not in args.report.resolve().parts:
            print(f"AVISO: {args.report} está fuera de data/ y contiene valores originales.", file=sys.stderr)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report.to_json(), encoding="utf-8")
        print(f"  informe: {args.report}")
    if report.problems:
        print("VERIFICACIÓN FALLIDA:", file=sys.stderr)
        for p in report.problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print("  verificación (relectura del texto): OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
