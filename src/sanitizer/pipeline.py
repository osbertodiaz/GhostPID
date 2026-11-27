"""Fases 4 y 5 encadenadas: zonas → logos → texto → OCR → guardado y verificación.

El orden importa:

1. Zonas: se eliminan enteras y así el resto de etapas no trabaja en ellas.
2. Logos: imágenes repetidas o fijadas en la config.
3. Texto (fase 4), con todas las capas visibles.
4. Se restauran las capas y se quitan los trazos pendientes (zonas y texto
   SHX), para que el OCR vea la página como quedará y no vuelva a encontrar
   lo que ya se ha quitado.
5. OCR de las páginas con texto en trazos o escaneadas, en varias pasadas.
6. Guardado (con los trazos que haya dejado el OCR) y verificación.

La limpieza de metadatos (fase 3) va después, sobre el PDF resultante.

Con `map_path`, el mapa de sustitución (fase 6) se carga si existe (la
numeración sigue la de los documentos anteriores) y se guarda ampliado.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from sanitizer.logos import remove_logos
from sanitizer.mapping import MapError, inside_data, load_map, namer_from_map, save_map, update_map
from sanitizer.ocr import ocr_passes
from sanitizer.redact import RedactionReport, Session
from sanitizer.zones import apply_zones
from sanitizer.i18n import tr


def anonymize(
    src: str | Path, dst: str | Path, config: Any, map_path: str | Path | None = None
) -> RedactionReport:
    """Anonimiza el contenido de `src` (fases 4 y 5) y lo guarda en `dst`."""
    if Path(src).resolve() == Path(dst).resolve():
        raise ValueError(tr("la salida no puede sobrescribir el PDF original", "the output cannot overwrite the original PDF"))
    existing = None
    if map_path is not None:
        map_path = Path(map_path)
        # Se comprueba antes de trabajar, no al final del proceso
        if not inside_data(map_path):
            raise MapError(
                tr(
                    "el mapa contiene los valores originales: solo se guarda dentro de data/ (no en {path})",
                    "the map contains the original values: it is only saved inside data/ (not in {path})",
                    path=map_path,
                )
            )
        existing = load_map(map_path) if map_path.exists() else None
    with Session(src, config, namer=namer_from_map(existing)) as session:
        session.snapshot()
        apply_zones(session)
        remove_logos(session)
        session.redact_text()
        session.restore_layers()
        session.flush_line_art()
        ocr_passes(session)
        session.save(dst)
    session.verify(dst)
    if map_path is not None:
        save_map(update_map(existing, session.namer, src, dst, config.name), map_path)
    return session.report


def print_summary(report: RedactionReport, out=None) -> None:
    out = out or sys.stdout  # se resuelve al llamar: stdout puede estar redirigido
    modes: dict[str, int] = {}
    for mode in report.page_modes.values():
        modes[mode] = modes.get(mode, 0) + 1
    print(tr("  páginas por tipo: {modes} · tags apilados protegidos: {n}", "  pages by type: {modes} · protected stacked tags: {n}", modes=modes, n=report.stacked_tags), file=out)
    if report.zones:
        print(tr("  zonas eliminadas: {n}", "  zones removed: {n}", n=len(report.zones)), file=out)
    removed = [g for g in report.logos if g["removed"]]
    if report.logos:
        print(tr("  imágenes: {g} grupos, {r} eliminados", "  images: {g} groups, {r} removed", g=len(report.logos), r=len(removed)), file=out)
        for g in removed:
            pages = tr("págs", "pages")
            print(f"    {g['hash']} {g['size'][0]}×{g['size'][1]} {pages} {len(g['pages'])} ({g['reason']})", file=out)
    if report.ocr:
        matches = sum(v["coincidencias"] for v in report.ocr.values())
        print(tr("  OCR: {p} páginas, {m} coincidencias", "  OCR: {p} pages, {m} matches", p=len(report.ocr), m=matches), file=out)
    counts = report.counts()
    print(tr("  sustituciones: ", "  substitutions: ") + (", ".join(f"{k} ×{v}" for k, v in counts.items()) or tr("ninguna", "none")), file=out)
    for w in report.warnings:
        print(tr("  aviso: {w}", "  warning: {w}", w=w), file=out)
    if report.near_misses:
        print(tr("  coincidencias rozadas (revisa las reglas): {n}", "  near misses (check the rules): {n}", n=len(report.near_misses)), file=out)
        for n in report.near_misses[:10]:
            print(tr("    pág {p}: …{t}… ('{r}')", "    page {p}: …{t}… ('{r}')", p=n.page, t=n.text, r=n.rule), file=out)
    for b in report.blocked:
        print(
            tr(
                "  BLOQUEADA (pág {p}, {src}): '{t}' de '{r}' toca '{wl}'",
                "  BLOCKED (page {p}, {src}): '{t}' from '{r}' touches '{wl}'",
                p=b.page, src=b.source, t=b.text, r=b.rule, wl=b.protected_by,
            ),
            file=out,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="anonymize",
        description=tr("Zonas, logos, texto y OCR (fases 4 y 5) según una config YAML.", "Zones, logos, text and OCR (phases 4 and 5) according to a YAML config."),
    )
    parser.add_argument("pdf", type=Path, help=tr("PDF de entrada", "input PDF"))
    parser.add_argument("-o", "--output", type=Path, required=True, help=tr("PDF de salida", "output PDF"))
    parser.add_argument("--config", type=Path, required=True, help=tr("config YAML del cliente", "client YAML config"))
    parser.add_argument(
        "--report", type=Path, help=tr("JSON con cada sustitución (contiene valores originales: en data/)", "JSON with every substitution (contains original values: keep in data/)")
    )
    parser.add_argument(
        "--map",
        type=Path,
        help=tr(
            "mapa de sustitución (data/maps/…): se reutiliza si existe y se guarda ampliado",
            "substitution map (data/maps/…): reused if it exists and saved extended",
        ),
    )
    args = parser.parse_args(argv)

    from sanitizer.rules import ConfigError, load_config  # evita import circular

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        parser.error(tr("config inválida: {exc}", "invalid config: {exc}", exc=exc))
    try:
        report = anonymize(args.pdf, args.output, config, args.map)
    except (ValueError, MapError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"{args.pdf} → {args.output}")
    print_summary(report)
    if args.report:
        if "data" not in args.report.resolve().parts:
            print(tr("AVISO: {r} está fuera de data/ y contiene valores originales.", "WARNING: {r} is outside data/ and contains original values.", r=args.report), file=sys.stderr)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report.to_json(), encoding="utf-8")
        print(tr("  informe: {r}", "  report: {r}", r=args.report))
    if args.map:
        print(tr("  mapa: {m}", "  map: {m}", m=args.map))
    if report.problems:
        print(tr("VERIFICACIÓN FALLIDA:", "VERIFICATION FAILED:"), file=sys.stderr)
        for p in report.problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print(tr("  verificación (relectura del texto): OK", "  verification (text re-read): OK"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
