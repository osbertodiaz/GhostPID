"""Fase 8 · Modelo de la UI de revisión (sin dependencias de Qt).

Reúne en una lista de `Detection` lo que el anonimizado y la auditoría saben
de un documento:

- sustituciones, zonas y logos eliminados (informe del anonimizado);
- coincidencias bloqueadas por la lista blanca y coincidencias "rozadas";
- restos y avisos de la auditoría;
- texto restante para revisar (candidatos a término nuevo).

Las detecciones vienen de una de estas fuentes:

1. Informes de un `sanitize` previo (`<reports>/<doc>/anonimizado.json` y
   `auditoria.json`), si son más recientes que el PDF y que la config.
2. Vista previa: anonimizado y auditoría sin OCR (rápida). Con `ocr=True`,
   con OCR (lenta en escaneados).

La vista previa trabaja en `<reports>/<doc>/revision/` (dentro de data/), con
una copia del mapa del documento para numerar igual que el resultado final
sin modificar el mapa real. Todo lo que muestra la UI son valores originales:
nunca sale de data/.

Descartar una detección se traduce en un cambio en la config (`ConfigEditor`):
excepción de la regla, logo a conservar o zona desactivada.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pymupdf

from sanitizer.audit import audit
from sanitizer.config_edit import ConfigEditor
from sanitizer.mapping import inside_data, load_map
from sanitizer.pipeline import anonymize

# Tipo → descripción corta (también es el orden de la leyenda)
KINDS: dict[str, str] = {
    "sustitución": "texto sustituido",
    "zona": "zona eliminada",
    "logo": "imagen eliminada",
    "resto": "resto encontrado por la auditoría",
    "aviso": "aviso de la auditoría",
    "rozada": "coincidencia rozada (revisa la regla)",
    "bloqueada": "bloqueada por la lista blanca",
    "revisar": "texto restante para revisar",
}

Rect = tuple[float, float, float, float]


@dataclass
class Detection:
    kind: str
    page: int  # 1 = primera; 0 = varias páginas (ver `pages`)
    text: str  # texto original (o contexto)
    detail: str = ""  # sustitución, motivo…
    rule: str = ""  # id de la regla o zona; hash del logo
    rects: list[Rect] = field(default_factory=list)  # coordenadas de página sin rotar
    pages: list[int] = field(default_factory=list)

    @property
    def discardable(self) -> bool:
        return discard_action(self) is not None

    def on_page(self, page: int) -> bool:
        return self.page == page or page in self.pages


@dataclass
class ReviewState:
    source: Path
    anonymized: Path | None  # PDF para la vista "anonimizado"
    origin: str  # "informes", "vista previa", "vista previa con OCR", "generado"
    report: dict[str, Any]
    audit: dict[str, Any] | None
    detections: list[Detection]

    def on_page(self, page: int) -> list[Detection]:
        return [d for d in self.detections if d.on_page(page)]


# --------------------------------------------------------------------------
# Detecciones
# --------------------------------------------------------------------------


def _audit_rule(where: str) -> str:
    """Regla de un hallazgo de la auditoría ("patrón 'id'" → id)."""
    return where.split("'")[1] if where.startswith("patrón '") else ""


def collect_detections(
    report: dict[str, Any], audit_data: dict[str, Any] | None, original: pymupdf.Document | None = None
) -> list[Detection]:
    out: list[Detection] = []
    for r in report.get("replacements", []):
        detail = r["replacement"] or "(eliminado)"
        if r.get("source", "page") != "page":
            detail += f" · {r['source']}"
        out.append(Detection("sustitución", r["page"], r["original"], detail, r["rule"], [tuple(x) for x in r.get("rects", [])]))
    for z in report.get("zones", []):
        out.append(Detection("zona", z["page"], z["zone"], "zona de la config", z["zone"], [tuple(z["rect"])]))
    for g in report.get("logos", []):
        if not g.get("removed"):
            continue
        for page in g["pages"]:
            rects: list[Rect] = []
            if original is not None and 1 <= page <= len(original):
                for xref in g["xrefs"]:
                    try:
                        rects += [tuple(r) for r in original[page - 1].get_image_rects(xref)]
                    except (RuntimeError, ValueError):
                        pass
            size = "×".join(map(str, g["size"]))
            out.append(Detection("logo", page, f"imagen {size}", g.get("reason", ""), g["hash"], rects))
    for b in report.get("near_misses", []):
        out.append(Detection("rozada", b["page"], b["text"], b["protected_by"], b["rule"]))
    for b in report.get("blocked", []):
        out.append(Detection("bloqueada", b["page"], b["text"], f"protegido por '{b['protected_by']}'", b["rule"]))
    if audit_data:
        for f in audit_data.get("findings", []):
            kind = "resto" if f["severity"] == "error" else "aviso"
            rects = [tuple(f["rect"])] if f.get("rect") else []
            detail = f"{f['check']} · {f['where']}"
            out.append(Detection(kind, f.get("page") or 0, f["text"], detail, _audit_rule(f["where"]), rects))
        for line in audit_data.get("review_lines", []):
            pages = list(line.get("páginas", []))
            origin = ", ".join(line.get("origen", []))
            out.append(Detection("revisar", pages[0] if len(pages) == 1 else 0, line["texto"], origin, "", [], pages))
    return out


def locate(page: pymupdf.Page, text: str) -> list[Rect]:
    """Dónde aparece `text` en la capa de texto de la página (para resaltarlo)."""
    words = " ".join(text.split())
    if not words:
        return []
    return [tuple(r * page.derotation_matrix) for r in page.search_for(words)][:50]


# --------------------------------------------------------------------------
# Fuentes: informes existentes, vista previa y generación
# --------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_existing(src: Path, config_path: Path, out_dir: Path, reports_dir: Path) -> ReviewState | None:
    """Informes de un `sanitize` previo, si siguen vigentes."""
    report_dir = reports_dir / src.stem
    anon_json, audit_json = report_dir / "anonimizado.json", report_dir / "auditoria.json"
    output = out_dir / f"{src.stem}.sanitized.pdf"
    if not (anon_json.exists() and audit_json.exists() and output.exists()):
        return None
    newest_input = max(src.stat().st_mtime, config_path.stat().st_mtime)
    if min(anon_json.stat().st_mtime, audit_json.stat().st_mtime) < newest_input:
        return None  # el PDF o la config cambiaron después
    report, audit_data = _read_json(anon_json), _read_json(audit_json)
    if report is None or audit_data is None:
        return None
    with pymupdf.open(src) as doc:
        detections = collect_detections(report, audit_data, doc)
    return ReviewState(src, output, "informes", report, audit_data, detections)


def _without_ocr(config: Any) -> Any:
    return config.model_copy(
        update={
            "ocr": dataclasses.replace(config.ocr, enabled=False),
            "audit": dataclasses.replace(config.audit, ocr_pages="none"),
        }
    )


def preview(
    src: Path,
    config: Any,
    reports_dir: Path,
    maps_dir: Path,
    ocr: bool = False,
    progress: Callable[[str], None] | None = None,
) -> ReviewState:
    """Anonimizado y auditoría de prueba en `<reports>/<doc>/revision/`."""
    say = progress or (lambda _msg: None)
    workdir = reports_dir / src.stem / "revision"
    if not inside_data(workdir / "x"):
        raise ValueError("la carpeta de informes debe estar dentro de data/: contiene valores originales")
    workdir.mkdir(parents=True, exist_ok=True)
    run_config = config if ocr else _without_ocr(config)
    # Copia del mapa: misma numeración que el resultado, sin tocar el real
    map_copy = workdir / "vista.map.json"
    real_map = maps_dir / f"{src.stem}.map.json"
    if real_map.exists():
        shutil.copyfile(real_map, map_copy)
    else:
        map_copy.unlink(missing_ok=True)
    output = workdir / "vista.pdf"
    say("Anonimizando" + (" con OCR" if ocr else " (sin OCR)") + "…")
    report = json.loads(anonymize(src, output, run_config, map_copy).to_json())
    say("Auditando…")
    result = audit(output, run_config, workdir, src, report, load_map(map_copy), write=False)
    audit_data = json.loads(json.dumps(_audit_dict(result), default=str))
    with pymupdf.open(src) as doc:
        detections = collect_detections(report, audit_data, doc)
    return ReviewState(src, output, "vista previa con OCR" if ocr else "vista previa", report, audit_data, detections)


def _audit_dict(result: Any) -> dict[str, Any]:
    return {
        "findings": [dataclasses.asdict(f) for f in result.findings],
        "review_lines": result.review_lines,
        "checks": result.checks,
        "ok": result.ok,
    }


def generate(
    src: Path,
    config: Any,
    config_path: Path,
    out_dir: Path,
    reports_dir: Path,
    maps_dir: Path,
    map_path: Path | None = None,
    repair_passes: int = 1,
) -> tuple[Any, ReviewState | None]:
    """Proceso completo (`sanitize`) y estado con los informes resultantes."""
    from sanitizer.cli import sanitize_document  # cli importa este módulo indirectamente

    result = sanitize_document(src, config, out_dir, reports_dir, map_path or maps_dir / f"{src.stem}.map.json", repair_passes)
    state = load_existing(src, config_path, out_dir, reports_dir)
    if state is not None:
        state.origin = "generado"
    return result, state


# --------------------------------------------------------------------------
# Descartes
# --------------------------------------------------------------------------


def discard_action(d: Detection) -> str | None:
    """Qué cambio en la config descarta la detección (None si no se puede)."""
    if d.kind == "sustitución" and d.rule:
        return f"excepción '{d.text}' en la regla '{d.rule}'"
    if d.kind in ("resto", "aviso") and d.rule:
        return f"excepción '{d.text}' en la regla '{d.rule}'"
    if d.kind == "logo":
        return f"conservar siempre la imagen {d.rule} (logos.keep)"
    if d.kind == "zona":
        return f"desactivar la zona '{d.rule}' (disable)"
    return None


def discard(editor: ConfigEditor, d: Detection) -> str:
    """Aplica el descarte en la config; devuelve la descripción del cambio."""
    action = discard_action(d)
    if action is None:
        raise ValueError(f"esta detección no se puede descartar ({d.kind})")
    if d.kind in ("sustitución", "resto", "aviso"):
        editor.add_exception(d.rule, d.text)
    elif d.kind == "logo":
        editor.set_logo(d.rule, remove=False)
    elif d.kind == "zona":
        editor.disable(d.rule)
    return action


def zone_fraction(page: pymupdf.Page, view_rect: Rect) -> Rect:
    """Rectángulo en coordenadas de la página tal como se ve → fracción de página."""
    w, h = page.rect.width, page.rect.height
    x0, y0, x1, y1 = view_rect
    return (min(x0, x1) / w, min(y0, y1) / h, max(x0, x1) / w, max(y0, y1) / h)
