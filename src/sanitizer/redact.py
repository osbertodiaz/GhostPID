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
    # Al blanquear píxeles, MuPDF descomprime la imagen y la guarda sin
    # pérdidas: un escaneado JPEG ocupa 10 veces más. Las imágenes que eran
    # JPEG en el original se vuelven a codificar en JPEG con esta calidad
    jpeg_quality: int = 90
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
    # Fase 5: zonas eliminadas, grupos de imágenes (logos) y OCR por página
    zones: list[dict[str, Any]] = field(default_factory=list)
    logos: list[dict[str, Any]] = field(default_factory=list)
    ocr: dict[int, dict[str, Any]] = field(default_factory=dict)
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
    """Asigna denominaciones genéricas coherentes.

    El contador va por plantilla, no por regla: dos reglas con VENDOR-{n} no
    pueden dar VENDOR-1 a dos fabricantes distintos. Puede partir de un mapa
    existente (`from_entries`) para que la numeración siga siendo la misma en
    todos los documentos de un proyecto.

    `entries` es la materia prima del mapa de sustitución (fase 6): por cada
    denominación, su plantilla, número, regla, categoría y los originales con
    cuántas veces aparecieron y cuántas se leyeron del texto real (no de OCR).
    """

    def __init__(self) -> None:
        self._values: dict[tuple[str, str], str] = {}
        self._counters: dict[str, int] = {}
        self.mapping: dict[str, list[str]] = {}
        self.entries: dict[str, dict[str, Any]] = {}

    @classmethod
    def from_entries(cls, entries: dict[str, dict[str, Any]]) -> "Namer":
        namer = cls()
        for value, entry in entries.items():
            template = entry["template"]
            for original in entry["originals"]:
                namer._values[(template, normalize(original))] = value
            if entry.get("entity"):
                namer._values[(template, ENTITY_KEY + normalize(entry["entity"]))] = value
            if entry.get("n"):
                namer._counters[template] = max(namer._counters.get(template, 0), entry["n"])
            namer.entries[value] = {
                **entry,
                "originals": {o: dict(stats) for o, stats in entry["originals"].items()},
            }
        return namer

    def alias(self, template: str, original: str, value: str) -> None:
        """Hace que `original` reciba la denominación ya existente `value`."""
        self._values[(template, normalize(original))] = value

    def name(self, template: str, original: str, rule: Any = None, exact: bool = True) -> str:
        """Denominación para `original`. `exact`: leído del texto real (no OCR)."""
        keys = [(template, normalize(original))]
        # Lista negra: todos los alias de una entidad comparten denominación
        entity = rule.entity_of(original) if rule is not None and getattr(rule, "entities", None) else None
        if entity:
            keys.append((template, ENTITY_KEY + normalize(entity)))
        value = next((self._values[k] for k in keys if k in self._values), None)
        n = None
        if value is None:
            if "{n}" in template:
                n = self._counters.get(template, 0) + 1
                self._counters[template] = n
                value = template.format(n=n)
            else:
                value = template
        for k in keys:
            self._values.setdefault(k, value)
        originals = self.mapping.setdefault(value, [])
        if original not in originals:
            originals.append(original)

        entry = self.entries.setdefault(
            value, {"template": template, "n": n, "rule": None, "category": None, "originals": {}}
        )
        if rule is not None and entry["rule"] is None:
            entry["rule"] = rule.id
            entry["category"] = rule.category.value
        if entity:
            entry.setdefault("entity", entity)
        # Los saltos de línea y espacios del plano no distinguen originales
        stats = entry["originals"].setdefault(" ".join(original.split()), {"count": 0, "exact": 0})
        stats["count"] += 1
        stats["exact"] += int(exact)
        return value


# Prefijo de las claves de entidad del Namer (no colisiona con un original)
ENTITY_KEY = "\x00entity:"


def normalize(text: str) -> str:
    return " ".join(text.split()).casefold()


# Confusiones típicas del OCR: se normalizan igual en el texto y en el término
_OCR_CONFUSIONS = str.maketrans({"0": "O", "1": "I", "L": "I", "|": "I", "!": "I", "5": "S", "8": "B"})


def ocr_normalize(text: str) -> str:
    """Mayúsculas y confusiones de OCR unificadas, conservando la longitud."""
    upper = "".join(c.upper() if len(c.upper()) == 1 else c for c in text)
    return upper.translate(_OCR_CONFUSIONS)


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
    fuzzy: bool = False,
) -> tuple[list[Match], list[tuple[int, int, Any, str]]]:
    """Coincidencias de sustitución aceptadas y bloqueadas en `text`.

    `relaxed`: los términos encajan también dentro de otras palabras.
    `fuzzy` (texto de OCR): los términos se comparan tras `ocr_normalize`.

    Entre coincidencias que se solapan gana la que empieza antes y, a igualdad,
    la más larga. Una coincidencia que toca un tramo protegido se bloquea.
    """
    protected = [(m.start(), m.end(), wl.id) for wl in config.whitelist for m in wl.regex.finditer(text)]
    protected += list(extra_protected)

    normalized = ocr_normalize(text) if fuzzy else None
    candidates = []
    for order, rule in enumerate(config.substitutions):
        if fuzzy:
            searches = [(rx, normalized) for rx in rule.ocr_term_matchers(relaxed)]
            searches += [(rx, text) for rx in rule.pattern_regexes]
        else:
            searches = [(rx, text) for rx in (rule.relaxed_matchers if relaxed else rule.matchers)]
        for rx, haystack in searches:
            for m in rx.finditer(haystack):
                if m.end() > m.start() and not rule.excepted(text[m.start() : m.end()]):
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
    """Coincidencias "rozadas" que no se han sustituido.

    Términos y patrones sin límites de palabra o de token, y la primera
    palabra de los términos de varias palabras.
    """
    from sanitizer.rules import term_regex  # rules importa este módulo

    out = []
    for rule in config.substitutions:
        if rule.word_only:
            continue  # lista negra: solo palabras completas, nada de rozadas
        flags = 0 if rule.case_sensitive else re.IGNORECASE
        raw = [term_regex(t, word_boundaries=False) for t in rule.terms]
        # Términos de varias palabras que aparecen solo en parte (p. ej. un logo
        # que apila "ACME" sobre "ENGINEERING"): se avisa por la primera palabra
        raw += [
            term_regex(t.split()[0])
            for t in rule.terms
            if len(t.split()) > 1 and len(t.split()[0]) >= 4
        ]
        if rule.boundary == "token":
            raw += rule.patterns
        for pattern in raw:
            for m in re.finditer(pattern, text, flags):
                if m.end() <= m.start() or rule.excepted(m.group()):
                    continue
                if not any(m.start() < a.end and a.start < m.end() for a in accepted):
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
        value = namer.name(m.rule.replacement, text[m.start : m.end], m.rule)
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
            value = namer.name(m.rule.replacement, original, m.rule)
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

    for item in planned:
        if item.origin is not None:
            # La sustitución de un texto oculto va también a una capa oculta
            insert_replacement(
                page,
                item.replacement.replacement,
                item.origin,
                item.direction,
                item.font_size,
                item.full_rects[0],
                policy,
                oc=hidden_ocg if item.hidden else None,
            )

    if mode.graphics == "none":
        return []
    to_pdf = ~page.transformation_matrix
    areas = []
    for item in planned:
        for rect in item.full_rects:
            r = (rect * to_pdf).normalize()
            areas.append((r.x0, r.y0, r.x1, r.y1))
    return areas


def insert_replacement(
    page: pymupdf.Page,
    text: str,
    origin: tuple[float, float],
    direction: tuple[float, float],
    font_size: float,
    area: pymupdf.Rect,
    policy: RedactionPolicy,
    oc: int | None = None,
) -> None:
    """Escribe la denominación genérica en el sitio del original.

    Coordenadas de página sin rotar (las de get_text). El texto sigue la
    dirección del original y se encoge si no cabe en el área.
    """
    if not text:
        return
    dx, dy = direction
    angle = math.degrees(math.atan2(-dy, dx)) % 360
    vertical = abs(angle % 180 - 90) < 45
    available = area.height if vertical else area.width
    size = font_size or 8.0
    length = pymupdf.get_text_length(text, fontname=policy.font, fontsize=size)
    if length > available > 0:
        size *= available / length
    size = max(size, policy.min_font_size)
    kwargs = {
        "fontsize": size,
        "fontname": policy.font,
        "color": (1, 1, 1) if policy.fill == "black" else (0, 0, 0),
    }
    if oc:
        kwargs["oc"] = oc
    snapped = round(angle / 90) * 90 % 360
    if min(abs(angle - snapped), 360 - abs(angle - snapped)) < 1:
        page.insert_text(origin, text, rotate=snapped, **kwargs)
    else:
        # Texto inclinado: se gira alrededor del origen
        morph = (pymupdf.Point(origin), pymupdf.Matrix(-angle))
        page.insert_text(origin, text, morph=morph, **kwargs)


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


def to_bytes(doc: pymupdf.Document) -> bytes:
    """Serializa el documento comprimiendo también las imágenes.

    Tras blanquear píxeles, MuPDF guarda las imágenes descomprimidas (un plano
    escaneado puede ocupar cientos de MB en RGB). Una copia sin comprimir de
    un set así supera los 2 GB y MuPDF falla al reabrirla.
    """
    return doc.tobytes(garbage=1, deflate=True, deflate_images=True)


def _open_with_all_layers(path: Path, policy: RedactionPolicy) -> tuple[pymupdf.Document, dict | None]:
    """Abre el PDF con todas las capas visibles; devuelve el estado para restaurarlo."""
    doc = pymupdf.open(path)
    ocgs = list(doc.get_ocgs().keys())
    if not ocgs or policy.hidden_layers == "skip":
        return doc, None
    state = doc.get_layer(-1)
    doc.set_layer(-1, on=ocgs, off=[])
    # MuPDF aplica la nueva visibilidad al reabrir el documento
    data = to_bytes(doc)
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
    copy = pymupdf.open("pdf", to_bytes(doc))
    # Se quita la lista /Annots a bajo nivel: borrar una a una falla con
    # anotaciones que tienen popups o respuestas encadenadas
    for page in copy:
        copy.xref_set_key(page.xref, "Annots", "null")
    return pymupdf.open("pdf", to_bytes(copy))


# --------------------------------------------------------------------------
# Documento completo
# --------------------------------------------------------------------------


class Session:
    """PDF en proceso de anonimización.

    Se abre con todas las capas visibles (para ver el texto oculto) y se va
    modificando por etapas: zonas, logos, texto, OCR. Los trazos que hay que
    quitar se acumulan y se eliminan con pikepdf en `flush_line_art`.

    Orden obligado: `snapshot()` antes de cambiar nada; `redact_text()` antes
    de `restore_layers()`; el OCR, después de `flush_line_art()`, para que no
    vuelva a encontrar lo que ya se ha quitado.
    """

    def __init__(
        self,
        src: str | Path,
        config: Any,
        policy: RedactionPolicy | None = None,
        detection: DetectionParams | None = None,
        namer: Namer | None = None,
    ) -> None:
        self.src = Path(src)
        self.config = config
        self.policy = policy or config.redaction
        self.detection = detection or config.detection
        self.report = RedactionReport()
        self.namer = namer or Namer()
        self.doc, self.layer_state = _open_with_all_layers(self.src, self.policy)
        self.layers_restored = self.layer_state is None
        self.line_art: dict[int, list[tuple[list, bool]]] = collections.defaultdict(list)
        self.protected_before: dict[int, collections.Counter] = {}
        # Rectángulos (coordenadas de página) donde se permite perder datos
        # protegidos: las zonas se eliminan enteras
        self.cleared: dict[int, list[pymupdf.Rect]] = collections.defaultdict(list)
        # Páginas (índices) en las que se han blanqueado píxeles de imágenes
        self.pixel_pages: set[int] = set()

    def close(self) -> None:
        self.doc.close()

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- etapas -------------------------------------------------------------

    def snapshot(self) -> None:
        """Datos protegidos por página antes de tocar nada (salvaguarda final)."""
        with _without_annotations(self.doc) as plain:
            for page in plain:
                self.protected_before[page.number + 1] = _protected_counts(page, self.config)

    def redact_text(self) -> None:
        """Fase 4: texto de páginas, capas ocultas, anotaciones y formularios."""
        config, policy, report = self.config, self.policy, self.report
        plain = _without_annotations(self.doc)
        # Lo que se ve con las capas en su estado original, para saber qué
        # coincidencias están en contenido oculto
        original = _without_annotations(pymupdf.open(self.src)) if self.layer_state is not None else None

        # 1) Planificar todas las páginas antes de tocar ninguna
        plans = []
        for page in plain:
            mode = page_mode(page, self.detection)
            report.page_modes[page.number + 1] = mode
            visible = _visible_chars(original[page.number]) if original is not None else None
            relaxed = policy.ocr_relaxed_terms and mode == "scanned"
            plans.append((page.number, mode, _plan_page(page, config, policy, self.namer, report, visible, relaxed)))
        plain.close()
        if original is not None:
            original.close()

        hidden_ocg = None
        if any(item.hidden for _, _, planned in plans for item in planned):
            hidden_ocg = self.doc.add_ocg("redacted-hidden", on=False)
            if self.layer_state is not None:
                off = [*(self.layer_state.get("off") or []), hidden_ocg]
                self.layer_state = {**self.layer_state, "off": off}

        # 2) Redactar e insertar las denominaciones genéricas
        for number, mode, planned in plans:
            page = self.doc[number]
            if planned:
                page_mode_cfg = getattr(policy, mode)
                areas = _apply_page(page, planned, page_mode_cfg, policy, hidden_ocg)
                if page_mode_cfg.images != "none":
                    self.pixel_pages.add(number)
                if areas:
                    self.add_line_art(number, areas, page_mode_cfg.graphics == "touched")
                report.replacements += [p.replacement for p in planned]
            if policy.annotations:
                _process_annotations(page, config, self.namer, report)

    def add_line_art(self, page_index: int, areas: list, touched: bool = False) -> None:
        """Áreas (espacio PDF) cuyos trazos hay que quitar de la página."""
        self.line_art[page_index].append((areas, touched))

    def restore_layers(self) -> None:
        if not self.layers_restored:
            _restore_layers(self.doc, self.layer_state)
            self.layers_restored = True

    def flush_line_art(self) -> None:
        """Quita los trazos pendientes con pikepdf y reabre el documento.

        pikepdf renumera los objetos, así que antes se restaura el estado de
        las capas (sus referencias dejarían de ser válidas).
        """
        if not self.line_art:
            return
        self.restore_layers()
        self._restore_lossy_images()
        with pikepdf.open(io.BytesIO(to_bytes(self.doc))) as pdf:
            for number, jobs in sorted(self.line_art.items()):
                for areas, touched in jobs:
                    result = remove_paths(pdf, number, areas, touched)
                    if result.xobject_paths:
                        self.report.warnings.append(
                            f"página {number + 1}: hay trazos dentro de Form XObjects que no se "
                            f"han revisado (revisa visualmente el resultado)"
                        )
            buf = io.BytesIO()
            pdf.save(buf)
        self.doc.close()
        self.doc = pymupdf.open("pdf", buf.getvalue())
        self.line_art.clear()

    def view(self) -> pymupdf.Document:
        """Copia tal como se ve: capas en su estado guardado y sin anotaciones."""
        self.restore_layers()
        self._restore_lossy_images()
        return _without_annotations(self.doc)

    def save(self, dst: str | Path) -> None:
        dst = Path(dst)
        if self.src.resolve() == dst.resolve():
            raise ValueError("la salida no puede sobrescribir el PDF original")
        self.restore_layers()
        self.flush_line_art()
        self._restore_lossy_images()
        # deflate_images: lo que siga sin comprimir tras blanquear píxeles
        self.doc.save(dst, garbage=3, deflate=True, deflate_images=True)
        self.report.mapping = self.namer.mapping

    def _restore_lossy_images(self) -> None:
        """Vuelve a codificar en JPEG las imágenes que lo eran en el original.

        Solo en páginas con píxeles blanqueados y solo imágenes de 8 bits que
        ocupan el mismo sitio que una imagen JPEG/JPEG2000 del original.
        """
        if not self.pixel_pages:
            return
        with pymupdf.open(self.src) as original:
            for number in sorted(self.pixel_pages):
                lossy = set()
                for info in original[number].get_images(full=True):
                    filters = original.xref_get_key(info[0], "Filter")[1]
                    if "DCTDecode" in filters or "JPXDecode" in filters:
                        lossy |= {tuple(round(v) for v in r) for r in original[number].get_image_rects(info[0])}
                if not lossy:
                    continue
                page = self.doc[number]
                for info in page.get_images(full=True):
                    xref, bpc = info[0], info[4]
                    if bpc != 8 or "DCTDecode" in self.doc.xref_get_key(xref, "Filter")[1]:
                        continue
                    if not any(tuple(round(v) for v in r) in lossy for r in page.get_image_rects(xref)):
                        continue
                    pix = pymupdf.Pixmap(self.doc, xref)
                    if pix.alpha:
                        pix = pymupdf.Pixmap(pix, 0)
                    if pix.n not in (1, 3):
                        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                    self.doc.update_stream(xref, pix.tobytes("jpeg", jpg_quality=self.policy.jpeg_quality), compress=0)
                    self.doc.xref_set_key(xref, "Filter", "/DCTDecode")
                    self.doc.xref_set_key(xref, "DecodeParms", "null")
                    if pix.n == 1:
                        self.doc.xref_set_key(xref, "ColorSpace", "/DeviceGray")
                    elif pix.n == 3 and pix.colorspace and pix.colorspace.n == 3:
                        self.doc.xref_set_key(xref, "ColorSpace", "/DeviceRGB")

    def verify(self, dst: str | Path) -> None:
        """Relee la salida: coincidencias que sigan ahí y datos protegidos perdidos."""
        self.report.problems = verify_redaction(dst, self.config, self.policy, self.detection)
        after_doc, _ = _open_with_all_layers(Path(dst), self.policy)
        with after_doc, _without_annotations(after_doc) as plain_after:
            for page in plain_after:
                number = page.number + 1
                excluded = self.cleared.get(number, [])
                before = self.protected_before.get(number, collections.Counter())
                lost = before - _protected_counts(page, self.config)
                if lost and excluded:
                    # Lo que estaba dentro de una zona se elimina a propósito
                    lost -= collections.Counter(self._protected_in(number, excluded))
                if lost:
                    items = ", ".join(f"'{k}'" + (f" ×{v}" if v > 1 else "") for k, v in lost.items())
                    self.report.warnings.append(f"página {number}: se han perdido datos protegidos: {items}")

    def _protected_in(self, number: int, rects: list[pymupdf.Rect]) -> list[str]:
        """Datos protegidos del original que caían dentro de `rects`."""
        found = []
        with pymupdf.open(self.src) as original, _without_annotations(original) as plain:
            page = plain[number - 1]
            for text, chars in _blocks(page):
                for wl in self.config.whitelist:
                    for m in wl.regex.finditer(text):
                        boxes = [c.bbox for c in chars[m.start() : m.end()] if c.bbox is not None]
                        if boxes and any(r.intersects(b) for r in rects for b in boxes):
                            found.append(m.group(0))
        return found


def redact_pdf(
    src: str | Path,
    dst: str | Path,
    config: Any,
    policy: RedactionPolicy | None = None,
    detection: DetectionParams | None = None,
) -> RedactionReport:
    """Fase 4 sola: redacta el texto de `src` y guarda el resultado en `dst`."""
    if Path(src).resolve() == Path(dst).resolve():
        raise ValueError("la salida no puede sobrescribir el PDF original")
    with Session(src, config, policy, detection) as session:
        session.snapshot()
        session.redact_text()
        session.save(dst)
    session.verify(dst)
    return session.report


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
