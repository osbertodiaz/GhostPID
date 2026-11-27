"""Fase 1 · Diagnóstico: inventario de un PDF antes de anonimizarlo.

Recoge todo lo que puede contener información sensible o condiciona la
estrategia de anonimización: metadatos (Info, XMP, IDs), adjuntos,
anotaciones, capas (OCG), JavaScript, fuentes y, por página, si hay capa de
texto, solo imagen o solo trazos vectoriales.

El inventario es de solo lectura: nunca modifica ni guarda el PDF.

AVISO: el informe reproduce valores reales (autor, títulos, nombres de capas,
contenido de anotaciones). Guárdalo solo dentro de data/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pikepdf
import pymupdf
from pydantic import ConfigDict

from sanitizer.stroke_text import StrokeTextParams, find_stroke_text


@dataclass(frozen=True)
class DetectionParams:
    """Umbrales de diagnóstico. Valores por defecto calibrados con un corpus de
    P&ID reales (docs/corpus-pid.md); cada config puede ajustarlos en `detection`.
    """

    __pydantic_config__ = ConfigDict(extra="forbid")

    # Fracción de la página cubierta por imágenes a partir de la cual se
    # considera que la página es "de imagen" (escaneado o plano rasterizado)
    image_coverage_threshold: float = 0.5
    # Glifos en trazos a partir de los cuales la página tiene texto convertido
    # a curvas. En el corpus: logos y cajetines < 45; P&ID con tags en SHX > 90
    stroke_text_min_glyphs: int = 50
    # Caracteres invisibles mínimos para considerar que una página vectorial
    # lleva texto invisible sobre trazos (AutoCAD exporta así el texto SHX)
    invisible_text_min_chars: int = 20
    # Páginas mínimas en las que aparece una imagen para marcarla como repetida
    # (logo, sello). Absoluto y no fracción: en un set mixto el logo puede estar
    # solo en las páginas vectoriales y no en las escaneadas
    repeated_image_min_pages: int = 3
    stroke_text: StrokeTextParams = field(default_factory=StrokeTextParams)


DEFAULT_DETECTION = DetectionParams()

# Marcadores que se listan en el informe (los títulos pueden ser sensibles)
MAX_OUTLINE_LISTED = 40

# Longitud máxima de los textos citados en el informe (contenido de anotaciones)
PREVIEW_CHARS = 80

# Claves PDF que indican JavaScript (/JS o /S /JavaScript), como nombre completo
_JS_TOKEN = re.compile(r"/(JS|JavaScript)(?![A-Za-z0-9])")

PAGE_KIND_LABELS = {
    "text": "texto vectorial",
    "text_and_stroke_text": "texto vectorial + texto en trazos (necesita OCR en esas zonas)",
    "stroke_text_only": "solo texto en trazos, sin capa de texto (necesita OCR)",
    "image_only": "solo imagen (escaneado o raster, necesita OCR)",
    "image_with_text": "imagen con capa de texto (posible OCR previo)",
    "invisible_text_over_vectors": (
        "texto invisible sobre trazos (AutoCAD/SHX): se extrae, pero lo visible son trazos"
    ),
    "vector_only": "solo trazos, sin texto detectado",
    "empty": "vacía",
}


# --------------------------------------------------------------------------
# Recogida de datos
# --------------------------------------------------------------------------


def inspect_pdf(
    path: str | Path, detection: DetectionParams = DEFAULT_DETECTION
) -> dict[str, Any]:
    """Devuelve el inventario de un PDF como diccionario serializable a JSON."""
    path = Path(path)
    report: dict[str, Any] = {
        "file": path.name,
        "size_bytes": path.stat().st_size,
    }

    with pymupdf.open(path) as doc:
        report["encrypted"] = bool(doc.needs_pass or doc.is_encrypted)
        if doc.needs_pass:
            # Sin contraseña no se puede inspeccionar el contenido
            report["error"] = "PDF protegido con contraseña: no se puede inspeccionar."
            return report

        report["page_count"] = doc.page_count
        toc = doc.get_toc()
        report["outline_entries"] = len(toc)
        report["outline"] = [
            {"level": lvl, "title": title, "page": page}
            for lvl, title, page in toc[:MAX_OUTLINE_LISTED]
        ]
        report["embedded_files"] = _embedded_files(doc)
        report["layers"] = _layers(doc)
        report["javascript"] = _javascript(doc)
        report["pages"] = [_page_info(page, detection) for page in doc]
        report["fonts"] = _fonts(doc)
        report["images"] = _images(doc, detection.repeated_image_min_pages)

    report.update(_pikepdf_info(path))
    report["summary"] = _summary(report)
    return report


def _pikepdf_info(path: Path) -> dict[str, Any]:
    """Info completo (incluye claves personalizadas), XMP, /ID y versión."""
    out: dict[str, Any] = {}
    with pikepdf.open(path) as pdf:
        out["pdf_version"] = pdf.pdf_version
        out["info"] = {str(k).lstrip("/"): _pdf_str(v) for k, v in pdf.docinfo.items()}

        ids = pdf.trailer.get("/ID")
        out["document_ids"] = [bytes(x).hex() for x in ids] if ids is not None else []

        out["xmp"] = {}
        if "/Metadata" in pdf.Root:
            # Solo lectura: no se llama a save(), así que nada se escribe
            meta = pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False)
            out["xmp"] = {str(k): _pdf_str(v) for k, v in meta.items()}
            out["xmp_bytes"] = len(pdf.Root.Metadata.read_bytes())
    return out


def _pdf_str(value: Any) -> str:
    try:
        return str(value)
    except Exception:  # objetos PDF raros: mejor algo que fallar
        return repr(value)


def _embedded_files(doc: pymupdf.Document) -> list[dict[str, Any]]:
    files = []
    for name in doc.embfile_names():
        info = doc.embfile_info(name)
        files.append(
            {
                "name": name,
                "filename": info.get("filename", ""),
                "size": info.get("size", 0),
                "description": info.get("description", ""),
            }
        )
    return files


def _layers(doc: pymupdf.Document) -> list[dict[str, Any]]:
    return [
        {"xref": xref, "name": ocg.get("name", ""), "on": ocg.get("on"), "intent": ocg.get("intent")}
        for xref, ocg in sorted(doc.get_ocgs().items())
    ]


def _javascript(doc: pymupdf.Document) -> dict[str, Any]:
    """Busca JavaScript en el catálogo y en cualquier objeto con /JS o /S /JavaScript."""
    catalog = doc.pdf_catalog()
    names_js = doc.xref_get_key(catalog, "Names/JavaScript")[0] != "null"
    open_action = doc.xref_get_key(catalog, "OpenAction")[0] != "null"
    catalog_aa = doc.xref_get_key(catalog, "AA")[0] != "null"

    # Se busca en el código fuente de cada objeto para detectar también
    # diccionarios inline (p. ej. /OpenAction <</S/JavaScript/JS(...)>>)
    js_objects = []
    for xref in range(1, doc.xref_length()):
        try:
            source = doc.xref_object(xref, compressed=True)
        except Exception:
            continue
        if _JS_TOKEN.search(source):
            js_objects.append(xref)

    return {
        "present": bool(names_js or js_objects),
        "names_tree": names_js,
        "open_action": open_action,
        "catalog_additional_actions": catalog_aa,
        "objects": js_objects,
    }


def _page_info(page: pymupdf.Page, detection: DetectionParams) -> dict[str, Any]:
    rect = page.rect

    words = page.get_text("words")
    visible_chars, invisible_chars = text_visibility(page)
    images = page.get_image_info()
    coverage = image_coverage(page, images)

    cdrawings = page.get_cdrawings()
    drawings = len(cdrawings)
    stroke_runs = find_stroke_text(cdrawings, page.rect, detection.stroke_text)
    stroke_glyphs = sum(r.glyphs for r in stroke_runs)

    annots = []
    for annot in page.annots():
        info = annot.info
        annots.append(
            {
                "type": annot.type[1],
                "author": info.get("title", ""),
                "content": info.get("content", "")[:PREVIEW_CHARS],
            }
        )

    return {
        "number": page.number + 1,
        "width_mm": round(rect.width * 25.4 / 72, 1),
        "height_mm": round(rect.height * 25.4 / 72, 1),
        "rotation": page.rotation,
        "words": len(words),
        "visible_chars": visible_chars,
        "invisible_chars": invisible_chars,
        "images": len(images),
        "image_coverage": round(coverage, 3),
        "drawings": drawings,
        "stroke_text_runs": len(stroke_runs),
        "stroke_text_glyphs": stroke_glyphs,
        "links": len(page.get_links()),
        "annotations": annots,
        "kind": classify_page(
            len(words), coverage, drawings, stroke_glyphs, visible_chars, invisible_chars, detection
        ),
    }


def image_coverage(page: pymupdf.Page, images: list[dict[str, Any]] | None = None) -> float:
    """Fracción de la página cubierta por imágenes (0–1)."""
    rect = page.rect
    page_area = rect.width * rect.height or 1.0
    covered = 0.0
    for img in images if images is not None else page.get_image_info():
        bbox = pymupdf.Rect(img["bbox"]) & rect  # recortar a la página
        if not bbox.is_empty:
            covered += bbox.width * bbox.height
    return min(covered / page_area, 1.0)


def text_visibility(page: pymupdf.Page) -> tuple[int, int]:
    """Caracteres visibles e invisibles (modo de render 3 u opacidad 0).

    El texto invisible es típico de la capa OCR de un escaneado: no se ve,
    pero se extrae y se envía a un LLM igual que el visible.
    """
    visible = invisible = 0
    for span in page.get_texttrace():
        n = len(span.get("chars", ()))
        if span.get("type") == 3 or span.get("opacity", 1) == 0:
            invisible += n
        else:
            visible += n
    return visible, invisible


def _images(doc: pymupdf.Document, min_pages: int) -> list[dict[str, Any]]:
    """Imágenes del documento por xref, con las páginas donde aparecen.

    Una imagen que se repite en muchas páginas suele ser un logo, un sello o
    un cajetín rasterizado: candidata a eliminarse por hash en la fase 5.
    """
    # Se agrupa por hash del contenido: muchos PDF guardan una copia del logo
    # por página, con xref distinto pero los mismos bytes
    digests: dict[int, str] = {}
    images: dict[str, dict[str, Any]] = {}
    for page in doc:
        for img in page.get_images(full=True):
            xref, width, height = img[0], img[2], img[3]
            if xref not in digests:
                raw = doc.xref_stream_raw(xref) or b""
                digests[xref] = hashlib.sha256(raw).hexdigest()[:16]
            entry = images.setdefault(
                digests[xref],
                {"hash": digests[xref], "xrefs": [], "width": width, "height": height, "pages": []},
            )
            if xref not in entry["xrefs"]:
                entry["xrefs"].append(xref)
            if page.number + 1 not in entry["pages"]:
                entry["pages"].append(page.number + 1)
    threshold = min(min_pages, max(doc.page_count, 2))
    for entry in images.values():
        entry["repeated"] = len(entry["pages"]) >= threshold
    return sorted(images.values(), key=lambda e: (-len(e["pages"]), e["xrefs"][0]))


def classify_page(
    words: int,
    image_coverage: float,
    drawings: int,
    stroke_glyphs: int = 0,
    visible_chars: int = 0,
    invisible_chars: int = 0,
    detection: DetectionParams = DEFAULT_DETECTION,
) -> str:
    """Clasifica una página según su contenido (ver PAGE_KIND_LABELS)."""
    is_image = image_coverage >= detection.image_coverage_threshold
    has_stroke_text = stroke_glyphs >= detection.stroke_text_min_glyphs
    if is_image:
        return "image_with_text" if words else "image_only"
    if invisible_chars >= detection.invisible_text_min_chars and invisible_chars > visible_chars:
        # El texto se puede extraer, pero redactarlo no borra lo que se ve:
        # hay que eliminar también los trazos que hay debajo
        return "invisible_text_over_vectors"
    if words:
        return "text_and_stroke_text" if has_stroke_text else "text"
    if has_stroke_text:
        return "stroke_text_only"
    if drawings:
        return "vector_only"
    return "empty"


# Tipos de página cuyo contenido técnico no está (entero) en la capa de texto
OCR_KINDS = ("image_only", "stroke_text_only", "text_and_stroke_text")


def _fonts(doc: pymupdf.Document) -> list[dict[str, Any]]:
    """Fuentes del documento, agregadas por xref con las páginas donde aparecen."""
    fonts: dict[int, dict[str, Any]] = {}
    for page in doc:
        for xref, ext, ftype, basefont, _name, encoding in page.get_fonts(full=False):
            entry = fonts.setdefault(
                xref,
                {
                    "name": basefont,
                    "type": ftype,
                    "embedded": ext != "n/a",
                    # Las fuentes subset llevan un prefijo de 6 letras y "+"
                    "subset": len(basefont) > 7 and basefont[6] == "+",
                    "encoding": encoding,
                    "pages": [],
                },
            )
            if page.number + 1 not in entry["pages"]:
                entry["pages"].append(page.number + 1)
    return sorted(fonts.values(), key=lambda f: f["name"])


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    kinds: dict[str, int] = {}
    for page in report["pages"]:
        kinds[page["kind"]] = kinds.get(page["kind"], 0) + 1
    return {
        "page_kinds": kinds,
        "has_info": bool(report["info"]),
        "has_xmp": bool(report["xmp"]) or bool(report.get("xmp_bytes")),
        "embedded_files": len(report["embedded_files"]),
        "annotations": sum(len(p["annotations"]) for p in report["pages"]),
        "links": sum(p["links"] for p in report["pages"]),
        "layers": len(report["layers"]),
        "javascript": report["javascript"]["present"],
        "fonts": len(report["fonts"]),
        "images": len(report["images"]),
        "invisible_text_over_vectors": any(
            p["kind"] == "invisible_text_over_vectors" for p in report["pages"]
        ),
        "repeated_images": sum(1 for i in report["images"] if i["repeated"]),
        "invisible_text_pages": sum(1 for p in report["pages"] if p["invisible_chars"]),
        "needs_ocr": any(p["kind"] in OCR_KINDS for p in report["pages"]),
    }


# --------------------------------------------------------------------------
# Informe legible
# --------------------------------------------------------------------------


def format_markdown(report: dict[str, Any]) -> str:
    """Convierte el inventario en un informe Markdown legible."""
    lines = [f"# Inventario: {report['file']}", ""]
    if "error" in report:
        return "\n".join(lines + [f"**Error:** {report['error']}", ""])

    s = report["summary"]
    yes = lambda flag: "sí" if flag else "no"  # noqa: E731
    lines += [
        "## Resumen",
        "",
        f"- Tamaño: {report['size_bytes'] / 1024:.1f} KB · PDF {report['pdf_version']}"
        f" · cifrado: {yes(report['encrypted'])}",
        f"- Páginas: {report['page_count']} · marcadores: {report['outline_entries']}",
        "- Tipos de página: "
        + ", ".join(f"{PAGE_KIND_LABELS[k]} ×{n}" for k, n in s["page_kinds"].items()),
        f"- Metadatos Info: {yes(s['has_info'])} · XMP: {yes(s['has_xmp'])}"
        f" · IDs de documento: {len(report['document_ids'])}",
        f"- Adjuntos: {s['embedded_files']} · anotaciones: {s['annotations']}"
        f" · enlaces: {s['links']} · capas (OCG): {s['layers']}"
        f" · JavaScript: {yes(s['javascript'])}",
        f"- Fuentes: {s['fonts']} · imágenes: {s['images']}"
        f" (repetidas en muchas páginas: {s['repeated_images']})",
        f"- Páginas con texto invisible (capa OCR): {s['invisible_text_pages']}",
        f"- Requiere OCR en alguna página: {yes(s['needs_ocr'])}",
        "",
        "## Metadatos Info",
        "",
    ]
    lines += _kv_lines(report["info"])

    lines += ["", "## Metadatos XMP", ""]
    lines += _kv_lines(report["xmp"])

    lines += ["", "## IDs del documento (trailer /ID)", ""]
    lines += [f"- `{i}`" for i in report["document_ids"]] or ["- (ninguno)"]

    lines += ["", "## Adjuntos embebidos", ""]
    lines += [
        f"- {f['name']} → {f['filename']} ({f['size']} bytes) {f['description']}".rstrip()
        for f in report["embedded_files"]
    ] or ["- (ninguno)"]

    lines += ["", "## Capas (OCG)", ""]
    lines += [
        f"- [{l['xref']}] {l['name']} · {'visible' if l['on'] else 'oculta'}"
        for l in report["layers"]
    ] or ["- (ninguna)"]

    lines += ["", "## Marcadores", ""]
    lines += [
        f"- {'  ' * (o['level'] - 1)}{o['title']} → pág {o['page']}" for o in report["outline"]
    ] or ["- (ninguno)"]
    if report["outline_entries"] > len(report["outline"]):
        lines.append(f"- … y {report['outline_entries'] - len(report['outline'])} más")

    js = report["javascript"]
    lines += ["", "## JavaScript", ""]
    if js["present"] or js["open_action"] or js["catalog_additional_actions"]:
        lines += [
            f"- Árbol /Names/JavaScript: {yes(js['names_tree'])}",
            f"- /OpenAction en el catálogo: {yes(js['open_action'])}",
            f"- /AA en el catálogo: {yes(js['catalog_additional_actions'])}",
            f"- Objetos con JavaScript (xref): {js['objects'] or '-'}",
        ]
    else:
        lines.append("- (ninguno)")

    lines += [
        "",
        "## Páginas",
        "",
        "| Pág | Tamaño (mm) | Rot | Palabras | Car. invisibles | Imágenes | Cobertura img "
        "| Trazos | Glifos en trazos | Anot. | Tipo |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for p in report["pages"]:
        lines.append(
            f"| {p['number']} | {p['width_mm']}×{p['height_mm']} | {p['rotation']} "
            f"| {p['words']} | {p['invisible_chars']} | {p['images']} | {p['image_coverage']:.0%} "
            f"| {p['drawings']} "
            f"| {p['stroke_text_glyphs']} | {len(p['annotations'])} | {PAGE_KIND_LABELS[p['kind']]} |"
        )

    annotated = [p for p in report["pages"] if p["annotations"]]
    if annotated:
        lines += ["", "## Anotaciones", ""]
        for p in annotated:
            for a in p["annotations"]:
                detail = " · ".join(x for x in (a["author"], a["content"]) if x)
                lines.append(f"- Pág {p['number']}: {a['type']}" + (f" · {detail}" if detail else ""))

    lines += [
        "",
        "## Imágenes",
        "",
        "| Hash | xrefs | Tamaño (px) | Páginas | Repetida |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| `{i['hash']}` | {_page_ranges(i['xrefs'])} | {i['width']}×{i['height']} "
        f"| {_page_ranges(i['pages'])} "
        f"| {'sí (¿logo/sello?)' if i['repeated'] else 'no'} |"
        for i in report["images"]
    ] or ["| (ninguna) | | | | |"]

    lines += [
        "",
        "## Fuentes",
        "",
        "| Fuente | Tipo | Embebida | Subset | Codificación | Páginas (recursos) |",
        "|---|---|---|---|---|---|",
    ]
    for f in report["fonts"]:
        lines.append(
            f"| {f['name']} | {f['type']} | {yes(f['embedded'])} | {yes(f['subset'])} "
            f"| {f['encoding'] or '-'} | {_page_ranges(f['pages'])} |"
        )
    if not report["fonts"]:
        lines.append("| (ninguna) | | | | | |")

    return "\n".join(lines) + "\n"


def _kv_lines(data: dict[str, str]) -> list[str]:
    return [f"- **{k}**: {v}" for k, v in data.items()] or ["- (vacío)"]


def _page_ranges(pages: list[int]) -> str:
    """[1,2,3,5] → '1-3, 5'."""
    ranges: list[str] = []
    start = prev = None
    for n in sorted(pages):
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            ranges.append(f"{start}-{prev}" if start != prev else str(start))
            start = prev = n
    if start is not None:
        ranges.append(f"{start}-{prev}" if start != prev else str(start))
    return ", ".join(ranges)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _collect_pdfs(inputs: list[str]) -> list[Path]:
    pdfs: list[Path] = []
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            pdfs += sorted(x for x in p.rglob("*") if x.suffix.lower() == ".pdf")
        else:
            pdfs.append(p)
    return pdfs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="inventory",
        description="Inventario de PDFs (metadatos, adjuntos, anotaciones, capas, texto, fuentes).",
    )
    parser.add_argument("inputs", nargs="+", help="PDFs o carpetas (se recorren recursivamente)")
    parser.add_argument(
        "--out", type=Path, help="Carpeta donde guardar <nombre>.inventory.md (p. ej. data/reports)"
    )
    parser.add_argument("--json", action="store_true", help="Guardar también <nombre>.inventory.json")
    parser.add_argument(
        "--config",
        type=Path,
        help="Config YAML de cliente: usa sus umbrales de detección (bloque detection)",
    )
    args = parser.parse_args(argv)

    detection = DEFAULT_DETECTION
    if args.config:
        from sanitizer.rules import ConfigError, load_config  # evita import circular

        try:
            detection = load_config(args.config).detection
        except ConfigError as exc:
            parser.error(f"config inválida: {exc}")

    pdfs = _collect_pdfs(args.inputs)
    if not pdfs:
        parser.error("no se encontró ningún PDF")

    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        if "data" not in args.out.resolve().parts:
            print(
                f"AVISO: {args.out} está fuera de data/. El informe contiene datos reales "
                "y no debe acabar en git.",
                file=sys.stderr,
            )

    status = 0
    for pdf in pdfs:
        try:
            report = inspect_pdf(pdf, detection)
        except Exception as exc:  # un PDF corrupto no debe parar el lote
            print(f"ERROR en {pdf}: {exc}", file=sys.stderr)
            status = 1
            continue

        md = format_markdown(report)
        if args.out:
            (args.out / f"{pdf.stem}.inventory.md").write_text(md, encoding="utf-8")
            if args.json:
                (args.out / f"{pdf.stem}.inventory.json").write_text(
                    json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
                )
            print(f"{pdf} → {args.out / (pdf.stem + '.inventory.md')}")
        else:
            print(md)
            if args.json:
                print(json.dumps(report, indent=2, ensure_ascii=False))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
