"""Fase 7 · Verificación y auditoría de un PDF anonimizado.

Comprueba el resultado con independencia de cómo se produjo:

1. **Lista negra**: los términos y patrones de la config y, sobre todo, los
   valores originales del mapa de sustitución (los que se encontraron de
   verdad). Se buscan en:
   - el texto de todas las páginas, con todas las capas visibles;
   - anotaciones y valores de formularios;
   - todos los objetos del PDF con sus streams descomprimidos (nombres de
     fuente, XMP de imágenes, marcadores, nombres de capa…), salvo imágenes;
   - el OCR de las páginas renderizadas, que es lo que ve un LLM con visión.
2. **Metadatos**: `verify_metadata` relee el PDF con pikepdf.
3. **Revisión visual**: un PNG por página con las áreas tocadas resaltadas.
4. **Texto restante**: líneas que quedan en la franja del cajetín y que no son
   datos protegidos ni denominaciones genéricas, para detectar nombres que las
   reglas no conocen.

El informe recoge los valores eliminados: se guarda **solo dentro de data/**.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import pikepdf
import pymupdf
from pydantic import ConfigDict

from sanitizer.mapping import MapError, inside_data, load_map
from sanitizer.metadata import verify_metadata
from sanitizer.ocr import ocr_page
from sanitizer.redact import _blocks, _open_with_all_layers, _without_annotations, ocr_normalize

# Términos más cortos no se buscan en los objetos del PDF ni en OCR: darían
# coincidencias por azar
MIN_LITERAL = 4

COLORS = {
    "page": (0.1, 0.4, 0.9),  # sustitución en el texto
    "annotation": (0.1, 0.6, 0.2),
    "form": (0.1, 0.6, 0.2),
    "ocr": (0.95, 0.55, 0.0),  # sustitución encontrada por OCR
    "zone": (0.85, 0.1, 0.1),
    "logo": (0.55, 0.2, 0.7),
    "finding": (1.0, 0.0, 0.6),  # resto encontrado por la auditoría
}


@dataclass(frozen=True)
class AuditPolicy:
    __pydantic_config__ = ConfigDict(extra="forbid")

    # OCR de la auditoría: todas las páginas, solo las que lo necesitan o ninguna
    ocr_pages: Literal["all", "needed", "none"] = "all"
    # Franjas (fracción de la página tal como se ve: x0, y0, x1, y1) donde se
    # lista el texto restante para revisar
    review_areas: list[tuple[float, float, float, float]] = field(
        default_factory=lambda: [(0.75, 0.0, 1.0, 1.0), (0.0, 0.8, 1.0, 1.0)]
    )
    png_dpi: int = 60
    max_review_lines: int = 200
    # En la revisión se descartan las líneas de OCR con confianza media menor
    # (texto leído en espejo en una orientación equivocada, ruido)
    review_min_confidence: float = 80.0
    # Palabras que se añaden a las típicas de cajetín: una línea formada solo
    # por estas palabras no se lista para revisar
    review_ignore_words: list[str] = field(default_factory=list)


# Palabras habituales de un cajetín (inglés y español): no identifican a nadie
TITLE_BLOCK_WORDS = set(
    """
    drawing drawings dwg number no nº num sheet sheets of rev revision revisions date scale size
    drawn checked approved designed description descriptions project projects client contractor
    consortium title document documents doc filename file the and for by to at in on a an is
    plant unit units issued issue for construction review approval comments general notes note
    legend symbols abbreviations reference references ref sht p&id p&ids pid design build
    prepared submitted owner operator engineer engineering vendor package
    plano planos hoja hojas revisión revisiones fecha escala tamaño dibujado revisado aprobado
    preparado elaborado propietario operador ingeniería
    diseñado descripción proyecto cliente contratista título documento archivo de del la el los
    las y para por en con sin notas nota leyenda símbolos referencia
    """.split()
)


@dataclass
class Finding:
    severity: Literal["error", "warning"]
    check: str  # texto, anotación, formulario, objeto, ocr, metadatos
    text: str
    page: int | None = None
    where: str = ""
    rect: tuple[float, float, float, float] | None = None


@dataclass
class AuditResult:
    file: str
    findings: list[Finding] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)
    removed: dict[str, Any] = field(default_factory=dict)
    review_lines: list[dict[str, Any]] = field(default_factory=list)
    images: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(f.severity == "error" for f in self.findings)


# --------------------------------------------------------------------------
# Lista negra
# --------------------------------------------------------------------------


@dataclass
class Blacklist:
    literals: list[str]
    patterns: list[tuple[str, re.Pattern[str]]]  # (regla, regex)
    generics: set[str]

    def text_hits(self, text: str) -> list[tuple[int, int, str]]:
        """Restos en texto extraído: literales (sin distinguir mayúsculas,
        también dentro de otras palabras) y patrones."""
        hits = []
        for lit in self.literals:
            words = lit.split()
            rx = re.compile(r"\s*".join(re.escape(w) for w in words) if len(words) > 1 else re.escape(lit), re.IGNORECASE)
            hits += [(m.start(), m.end(), lit) for m in rx.finditer(text)]
        for rule_id, rx in self.patterns:
            hits += [(m.start(), m.end(), f"patrón '{rule_id}'") for m in rx.finditer(text)]
        return hits

    def ocr_hits(self, text: str) -> list[tuple[int, int, str]]:
        """Como text_hits, tolerando confusiones de OCR en los literales."""
        norm = ocr_normalize(text)
        hits = []
        for lit in self.literals:
            if len(lit) < MIN_LITERAL:
                continue
            words = ocr_normalize(lit).split()
            rx = re.compile(r"\s*".join(re.escape(w) for w in words))
            hits += [(m.start(), m.end(), lit) for m in rx.finditer(norm)]
        for rule_id, rx in self.patterns:
            hits += [(m.start(), m.end(), f"patrón '{rule_id}'") for m in rx.finditer(text)]
        return hits


def build_blacklist(config: Any, map_data: dict[str, Any] | None) -> Blacklist:
    literals: set[str] = set()
    generics: set[str] = set()
    for rule in config.substitutions:
        literals.update(" ".join(t.split()) for t in rule.terms)
        if rule.replacement:
            generics.add(rule.replacement.split("{")[0].strip("-_ ").casefold())
    if map_data:
        for value, entry in map_data["entries"].items():
            literals.update(" ".join(o.split()) for o in entry["originals"])
            if value:
                generics.add(value.casefold())
    # Un original que coincide con una denominación genérica no es un resto
    literals = {lit for lit in literals if lit and lit.casefold() not in generics}
    patterns = [(rule.id, rx) for rule in config.substitutions for rx in rule.pattern_regexes]
    return Blacklist(sorted(literals, key=lambda s: (-len(s), s)), patterns, generics)


def _protected_spans(config: Any, text: str) -> list[tuple[int, int]]:
    return [m.span() for wl in config.whitelist for m in wl.regex.finditer(text)]


def _severity(config: Any, text: str, start: int, end: int) -> Literal["error", "warning"]:
    """Un resto que cae dentro de un dato protegido es un aviso, no una fuga."""
    inside = any(a <= start and end <= b for a, b in _protected_spans(config, text))
    return "warning" if inside else "error"


# --------------------------------------------------------------------------
# Comprobaciones
# --------------------------------------------------------------------------


def _check_text(path: Path, config: Any, blacklist: Blacklist, result: AuditResult) -> None:
    doc, _ = _open_with_all_layers(path, config.redaction)
    with doc, _without_annotations(doc) as plain:
        for page in plain:
            for text, chars in _blocks(page):
                for start, end, label in blacklist.text_hits(text):
                    boxes = [c.bbox for c in chars[start:end] if c.bbox is not None]
                    rect = None
                    if boxes:
                        r = pymupdf.Rect(boxes[0])
                        for b in boxes[1:]:
                            r |= b
                        rect = tuple(r)
                    result.findings.append(
                        Finding(_severity(config, text, start, end), "texto", text[start:end], page.number + 1, label, rect)
                    )
        for page in doc:
            values = [("anotación", a.info.get("content") or "") for a in page.annots()]
            values += [("formulario", w.field_value) for w in page.widgets() if isinstance(w.field_value, str)]
            for check, value in values:
                for start, end, label in blacklist.text_hits(value):
                    result.findings.append(
                        Finding(_severity(config, value, start, end), check, value[start:end], page.number + 1, label)
                    )
        result.checks["texto"] = {"páginas": doc.page_count}


def _check_objects(path: Path, blacklist: Blacklist, result: AuditResult) -> None:
    """Literales en cualquier objeto del PDF (streams descomprimidos, sin imágenes)."""
    literals = [lit for lit in blacklist.literals if len(lit) >= MIN_LITERAL]
    rxs = []
    for lit in literals:
        for encoding in ("utf-8", "utf-16-be"):
            try:
                raw = lit.encode(encoding)
            except UnicodeEncodeError:
                continue
            rxs.append((lit, re.compile(re.escape(raw), re.IGNORECASE)))
    scanned = 0
    with pikepdf.open(path) as pdf:
        for obj in pdf.objects:
            if isinstance(obj, pikepdf.Stream):
                if obj.get("/Subtype") == "/Image":
                    continue
                try:
                    data = obj.read_bytes()
                except pikepdf.PdfError:
                    data = obj.read_raw_bytes()
                data = obj.stream_dict.unparse(resolved=True) + b"\n" + data
            elif isinstance(obj, (pikepdf.Dictionary, pikepdf.Array, pikepdf.String)):
                try:
                    data = obj.unparse(resolved=True)
                except Exception:
                    continue
            else:
                continue
            scanned += 1
            for lit, rx in rxs:
                if rx.search(data):
                    result.findings.append(Finding("error", "objeto", lit, None, f"objeto {obj.objgen}"))
        try:
            trailer = pdf.trailer.unparse(resolved=True)
            for lit, rx in rxs:
                if rx.search(trailer):
                    result.findings.append(Finding("error", "objeto", lit, None, "trailer"))
        except Exception:
            pass
    result.checks["objetos"] = {"revisados": scanned}


def _check_metadata(path: Path, config: Any, result: AuditResult) -> None:
    problems = verify_metadata(path, config.metadata)
    for p in problems:
        result.findings.append(Finding("error", "metadatos", p))
    result.checks["metadatos"] = {"problemas": len(problems)}


def _ocr_pages(view: pymupdf.Document, config: Any, anon_report: dict[str, Any] | None) -> list[int]:
    policy = config.audit
    if policy.ocr_pages == "none":
        return []
    if policy.ocr_pages == "all":
        return list(range(view.page_count))
    done = {int(k) - 1 for k in (anon_report or {}).get("ocr", {})}
    return sorted(done)


def _check_ocr(
    path: Path,
    config: Any,
    blacklist: Blacklist,
    anon_report: dict[str, Any] | None,
    result: AuditResult,
) -> dict[int, list[Any]]:
    """OCR de las páginas tal como se ven; devuelve las líneas por página."""
    lines_by_page: dict[int, list[Any]] = {}
    with pymupdf.open(path) as doc, _without_annotations(doc) as view:
        pages = _ocr_pages(view, config, anon_report)
        for number in pages:
            lines = ocr_page(view[number], config.ocr)
            lines_by_page[number] = lines
            for line in lines:
                for start, end, label in blacklist.ocr_hits(line.text):
                    boxes = [c.rect for c in line.chars[start:end] if not c.c.isspace()]
                    rect = None
                    if boxes:
                        r = pymupdf.Rect(boxes[0])
                        for b in boxes[1:]:
                            r |= b
                        rect = tuple(r)
                    result.findings.append(
                        Finding(_severity(config, line.text, start, end), "ocr", line.text[start:end], number + 1, label, rect)
                    )
    result.checks["ocr"] = {"páginas": len(pages)}
    return lines_by_page


# --------------------------------------------------------------------------
# Texto restante para revisar
# --------------------------------------------------------------------------

_ONLY_NOISE = re.compile(r"^[\W\d_]*$")


def _in_review_area(page: pymupdf.Page, rect: pymupdf.Rect, areas: list[tuple]) -> bool:
    view = (rect * page.rotation_matrix).normalize()
    cx, cy = (view.x0 + view.x1) / 2 / page.rect.width, (view.y0 + view.y1) / 2 / page.rect.height
    return any(x0 <= cx <= x1 and y0 <= cy <= y1 for x0, y0, x1, y1 in areas)


def _is_reviewable(config: Any, blacklist: Blacklist, line: str) -> bool:
    """Línea con palabras que no son datos protegidos, denominaciones genéricas
    ni vocabulario habitual de cajetín."""
    rest = line
    for wl in config.whitelist:
        rest = wl.regex.sub(" ", rest)
    ignore = TITLE_BLOCK_WORDS | {w.casefold() for w in config.audit.review_ignore_words}
    words = [
        w
        for w in re.findall(r"[^\W\d_]{3,}", rest)
        if w.casefold() not in blacklist.generics and w.casefold() not in ignore
    ]
    return bool(words) and not _ONLY_NOISE.match(rest)


def _review_text(
    path: Path,
    config: Any,
    blacklist: Blacklist,
    ocr_lines: dict[int, list[Any]],
    result: AuditResult,
) -> None:
    areas = [tuple(a) for a in config.audit.review_areas]
    seen: dict[str, dict[str, Any]] = {}

    def add(text: str, page: int, source: str) -> None:
        key = " ".join(text.split()).casefold()
        if not key or not _is_reviewable(config, blacklist, text):
            return
        entry = seen.setdefault(key, {"texto": " ".join(text.split()), "páginas": set(), "origen": set()})
        entry["páginas"].add(page)
        entry["origen"].add(source)

    with pymupdf.open(path) as doc, _without_annotations(doc) as view:
        for page in view:
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    text = "".join(s["text"] for s in line["spans"])
                    if _in_review_area(page, pymupdf.Rect(line["bbox"]), areas):
                        add(text, page.number + 1, "texto")
            for line in ocr_lines.get(page.number, []):
                if line.confidence < config.audit.review_min_confidence:
                    continue
                r = pymupdf.Rect(line.chars[0].rect)
                for c in line.chars[1:]:
                    r |= c.rect
                if _in_review_area(page, r, areas):
                    add(line.text, page.number + 1, "ocr")

    lines = sorted(seen.values(), key=lambda e: (-len(e["páginas"]), e["texto"]))
    result.review_lines = [
        {"texto": e["texto"], "páginas": sorted(e["páginas"]), "origen": sorted(e["origen"])}
        for e in lines[: config.audit.max_review_lines]
    ]
    result.checks["revisión"] = {"líneas": len(lines), "listadas": len(result.review_lines)}


# --------------------------------------------------------------------------
# Qué se ha eliminado
# --------------------------------------------------------------------------


def summarize_removed(anon_report: dict[str, Any] | None) -> dict[str, Any]:
    if not anon_report:
        return {}
    by_rule: dict[str, dict[str, Any]] = {}
    for r in anon_report.get("replacements", []):
        entry = by_rule.setdefault(
            r["rule"], {"categoría": r["category"], "veces": 0, "páginas": set(), "origen": collections.Counter()}
        )
        entry["veces"] += 1
        entry["páginas"].add(r["page"])
        entry["origen"][r["source"]] += 1
    zones: dict[str, set[int]] = {}
    for z in anon_report.get("zones", []):
        zones.setdefault(z["zone"], set()).add(z["page"])
    return {
        "reglas": {
            rule: {**e, "páginas": sorted(e["páginas"]), "origen": dict(e["origen"])} for rule, e in by_rule.items()
        },
        "zonas": {z: sorted(p) for z, p in zones.items()},
        "logos": [g for g in anon_report.get("logos", []) if g.get("removed")],
        "bloqueadas": len(anon_report.get("blocked", [])),
        "rozadas": len(anon_report.get("near_misses", [])),
        "avisos": anon_report.get("warnings", []),
    }


# --------------------------------------------------------------------------
# Revisión visual
# --------------------------------------------------------------------------


def _render_review(
    original: Path | None,
    path: Path,
    anon_report: dict[str, Any] | None,
    result: AuditResult,
    outdir: Path,
    dpi: int,
) -> None:
    marks: dict[int, list[tuple[tuple, str]]] = collections.defaultdict(list)
    for r in (anon_report or {}).get("replacements", []):
        for rect in r.get("rects", []):
            marks[r["page"]].append((tuple(rect), r["source"]))
    for z in (anon_report or {}).get("zones", []):
        marks[z["page"]].append((tuple(z["rect"]), "zone"))
    if original is not None and anon_report:
        with pymupdf.open(original) as src:
            for group in anon_report.get("logos", []):
                if not group.get("removed"):
                    continue
                for number in group["pages"]:
                    for xref in group["xrefs"]:
                        try:
                            rects = src[number - 1].get_image_rects(xref)
                        except Exception:
                            rects = []
                        marks[number] += [(tuple(rect), "logo") for rect in rects]
    for f in result.findings:
        if f.page and f.rect:
            marks[f.page].append((f.rect, "finding"))

    pages_dir = outdir / "paginas"
    pages_dir.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(path) as doc:
        # Se dibuja sobre una copia en memoria: el PDF auditado no se toca
        review = pymupdf.open("pdf", doc.tobytes())
        for page in review:
            for rect, kind in marks.get(page.number + 1, []):
                r = pymupdf.Rect(rect)
                if r.is_empty:
                    continue
                color = COLORS.get(kind, COLORS["finding"])
                page.draw_rect(r, color=color, fill=color, fill_opacity=0.18, width=1.2, overlay=True)
            name = f"pagina-{page.number + 1:03d}.png"
            page.get_pixmap(dpi=dpi).save(pages_dir / name)
            result.images.append(f"paginas/{name}")
        review.close()


# --------------------------------------------------------------------------
# Auditoría completa
# --------------------------------------------------------------------------


def audit(
    path: str | Path,
    config: Any,
    outdir: str | Path,
    original: str | Path | None = None,
    anon_report: dict[str, Any] | None = None,
    map_data: dict[str, Any] | None = None,
) -> AuditResult:
    """Audita `path` (PDF anonimizado) y escribe el informe en `outdir` (dentro de data/)."""
    path, outdir = Path(path), Path(outdir)
    if not inside_data(outdir / "x"):
        raise MapError(f"el informe de auditoría contiene valores originales: solo dentro de data/ (no en {outdir})")
    outdir.mkdir(parents=True, exist_ok=True)

    result = AuditResult(path.name)
    blacklist = build_blacklist(config, map_data)
    result.checks["lista negra"] = {"literales": len(blacklist.literals), "patrones": len(blacklist.patterns)}

    _check_text(path, config, blacklist, result)
    _check_objects(path, blacklist, result)
    _check_metadata(path, config, result)
    ocr_lines = _check_ocr(path, config, blacklist, anon_report, result)
    _review_text(path, config, blacklist, ocr_lines, result)
    result.removed = summarize_removed(anon_report)
    _render_review(Path(original) if original else None, path, anon_report, result, outdir, config.audit.png_dpi)

    (outdir / "auditoria.json").write_text(
        json.dumps(asdict(result) | {"ok": result.ok}, indent=2, ensure_ascii=False, default=list),
        encoding="utf-8",
    )
    (outdir / "auditoria.md").write_text(format_markdown(result), encoding="utf-8")
    return result


# --------------------------------------------------------------------------
# Informe
# --------------------------------------------------------------------------


def _ranges(pages: list[int]) -> str:
    out, start, prev = [], None, None
    for n in sorted(pages):
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(f"{start}-{prev}" if start != prev else str(start))
            start = prev = n
    if start is not None:
        out.append(f"{start}-{prev}" if start != prev else str(start))
    return ", ".join(out)


def format_markdown(result: AuditResult) -> str:
    errors = [f for f in result.findings if f.severity == "error"]
    warnings = [f for f in result.findings if f.severity == "warning"]
    lines = [
        f"# Auditoría: {result.file}",
        "",
        f"**Resultado: {'OK' if result.ok else f'FALLOS ({len(errors)})'}**"
        + (f" · {len(warnings)} avisos" if warnings else ""),
        "",
        "## Comprobaciones",
        "",
    ]
    for name, data in result.checks.items():
        lines.append(f"- {name}: " + ", ".join(f"{k} {v}" for k, v in data.items()))

    if result.findings:
        lines += ["", "## Hallazgos", "", "| Gravedad | Comprobación | Página | Texto | Dónde / regla |", "|---|---|---:|---|---|"]
        for f in result.findings:
            lines.append(f"| {f.severity} | {f.check} | {f.page or '-'} | {f.text} | {f.where} |")

    removed = result.removed
    if removed:
        lines += ["", "## Qué se ha eliminado", "", "| Regla | Categoría | Veces | Páginas | Origen |", "|---|---|---:|---|---|"]
        for rule, e in sorted(removed["reglas"].items(), key=lambda kv: -kv[1]["veces"]):
            origin = ", ".join(f"{k} ×{v}" for k, v in e["origen"].items())
            lines.append(f"| {rule} | {e['categoría']} | {e['veces']} | {_ranges(e['páginas'])} | {origin} |")
        if removed["zonas"]:
            lines += ["", "Zonas eliminadas:"]
            lines += [f"- {z}: páginas {_ranges(p)}" for z, p in removed["zonas"].items()]
        if removed["logos"]:
            lines += ["", "Imágenes eliminadas (logos):"]
            lines += [
                f"- `{g['hash']}` {g['size'][0]}×{g['size'][1]} px, páginas {_ranges(g['pages'])} ({g['reason']})"
                for g in removed["logos"]
            ]
        lines += [
            "",
            f"Coincidencias bloqueadas por la lista blanca: {removed['bloqueadas']} · "
            f"coincidencias rozadas: {removed['rozadas']}",
        ]
        lines += [f"- aviso: {w}" for w in removed["avisos"]]

    lines += ["", "## Texto restante para revisar", ""]
    if result.review_lines:
        lines.append(
            "Líneas en la franja del cajetín que no son datos protegidos ni denominaciones "
            "genéricas. Si alguna es un nombre, añádelo a las reglas."
        )
        lines += ["", "| Texto | Páginas | Origen |", "|---|---|---|"]
        for e in result.review_lines:
            text = e["texto"].replace("|", "\\|")
            lines.append(f"| {text} | {_ranges(e['páginas'])} | {', '.join(e['origen'])} |")
    else:
        lines.append("(nada)")

    if result.images:
        lines += [
            "",
            "## Revisión visual",
            "",
            "Colores: azul = sustitución en el texto · naranja = sustitución por OCR · "
            "rojo = zona · morado = logo · verde = anotación o formulario · rosa = resto encontrado.",
            "",
        ]
        lines += [f"- [{Path(img).name}]({img})" for img in result.images]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="audit",
        description="Audita un PDF anonimizado: lista negra (texto, objetos, OCR), metadatos y revisión visual.",
    )
    parser.add_argument("pdf", type=Path, help="PDF anonimizado")
    parser.add_argument("--config", type=Path, required=True, help="config YAML del cliente")
    parser.add_argument("--out", type=Path, required=True, help="carpeta del informe (dentro de data/)")
    parser.add_argument("--original", type=Path, help="PDF original (para marcar los logos eliminados)")
    parser.add_argument("--anon-report", type=Path, help="informe JSON de anonymize (--report)")
    parser.add_argument("--map", type=Path, help="mapa de sustitución: sus originales entran en la lista negra")
    args = parser.parse_args(argv)

    from sanitizer.rules import ConfigError, load_config  # evita import circular

    try:
        config = load_config(args.config)
        map_data = load_map(args.map) if args.map else None
    except (ConfigError, MapError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    anon_report = json.loads(args.anon_report.read_text(encoding="utf-8")) if args.anon_report else None
    try:
        result = audit(args.pdf, config, args.out, args.original, anon_report, map_data)
    except MapError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    errors = [f for f in result.findings if f.severity == "error"]
    print(f"{args.pdf}: {'OK' if result.ok else f'FALLOS ({len(errors)})'}")
    for name, data in result.checks.items():
        print(f"  {name}: " + ", ".join(f"{k} {v}" for k, v in data.items()))
    for f in errors[:20]:
        print(f"  ERROR ({f.check}, pág {f.page or '-'}): '{f.text}' {f.where}")
    print(f"  texto restante para revisar: {len(result.review_lines)} líneas")
    print(f"  informe: {args.out / 'auditoria.md'}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
