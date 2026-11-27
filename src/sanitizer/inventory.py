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
import json
import re
import sys
from pathlib import Path
from typing import Any

import pikepdf
import pymupdf

# Fracción de la página cubierta por imágenes a partir de la cual se
# considera que la página es "de imagen" (escaneado o plano rasterizado).
IMAGE_COVERAGE_THRESHOLD = 0.5

# Longitud máxima de los textos citados en el informe (contenido de anotaciones)
PREVIEW_CHARS = 80

# Claves PDF que indican JavaScript (/JS o /S /JavaScript), como nombre completo
_JS_TOKEN = re.compile(r"/(JS|JavaScript)(?![A-Za-z0-9])")

PAGE_KIND_LABELS = {
    "text": "texto vectorial",
    "image_only": "solo imagen (escaneado o raster, necesita OCR)",
    "image_with_text": "imagen con capa de texto (posible OCR previo)",
    "vector_only": "solo trazos, sin texto (¿texto convertido a curvas?)",
    "empty": "vacía",
}


# --------------------------------------------------------------------------
# Recogida de datos
# --------------------------------------------------------------------------


def inspect_pdf(path: str | Path) -> dict[str, Any]:
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
        report["outline_entries"] = len(doc.get_toc())
        report["embedded_files"] = _embedded_files(doc)
        report["layers"] = _layers(doc)
        report["javascript"] = _javascript(doc)
        report["pages"] = [_page_info(page) for page in doc]
        report["fonts"] = _fonts(doc)

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


def _page_info(page: pymupdf.Page) -> dict[str, Any]:
    rect = page.rect
    page_area = rect.width * rect.height or 1.0

    words = page.get_text("words")
    images = page.get_image_info()
    covered = 0.0
    for img in images:
        bbox = pymupdf.Rect(img["bbox"]) & rect  # recortar a la página
        if not bbox.is_empty:
            covered += bbox.width * bbox.height
    coverage = min(covered / page_area, 1.0)

    drawings = len(page.get_cdrawings())

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
        "images": len(images),
        "image_coverage": round(coverage, 3),
        "drawings": drawings,
        "links": len(page.get_links()),
        "annotations": annots,
        "kind": classify_page(len(words), coverage, drawings),
    }


def classify_page(words: int, image_coverage: float, drawings: int) -> str:
    """Clasifica una página según su contenido (ver PAGE_KIND_LABELS)."""
    is_image = image_coverage >= IMAGE_COVERAGE_THRESHOLD
    if words and is_image:
        return "image_with_text"
    if words:
        return "text"
    if is_image:
        return "image_only"
    if drawings:
        return "vector_only"
    return "empty"


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
        "needs_ocr": any(p["kind"] in ("image_only", "vector_only") for p in report["pages"]),
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
        f"- Fuentes: {s['fonts']}",
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
        "| Pág | Tamaño (mm) | Rot | Palabras | Imágenes | Cobertura img | Trazos | Anot. | Tipo |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for p in report["pages"]:
        lines.append(
            f"| {p['number']} | {p['width_mm']}×{p['height_mm']} | {p['rotation']} "
            f"| {p['words']} | {p['images']} | {p['image_coverage']:.0%} | {p['drawings']} "
            f"| {len(p['annotations'])} | {PAGE_KIND_LABELS[p['kind']]} |"
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
    args = parser.parse_args(argv)

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
            report = inspect_pdf(pdf)
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
