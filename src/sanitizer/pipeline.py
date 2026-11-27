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


def anonymize(
    src: str | Path, dst: str | Path, config: Any, map_path: str | Path | None = None
) -> RedactionReport:
    """Anonimiza el contenido de `src` (fases 4 y 5) y lo guarda en `dst`."""
    if Path(src).resolve() == Path(dst).resolve():
        raise ValueError("la salida no puede sobrescribir el PDF original")
    existing = None
    if map_path is not None:
        map_path = Path(map_path)
        # Se comprueba antes de trabajar, no al final del proceso
        if not inside_data(map_path):
            raise MapError(f"el mapa contiene los valores originales: solo se guarda dentro de data/ (no en {map_path})")
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
    print(f"  páginas por tipo: {modes} · tags apilados protegidos: {report.stacked_tags}", file=out)
    if report.zones:
        print(f"  zonas eliminadas: {len(report.zones)}", file=out)
    removed = [g for g in report.logos if g["removed"]]
    if report.logos:
        print(f"  imágenes: {len(report.logos)} grupos, {len(removed)} eliminados", file=out)
        for g in removed:
            print(f"    {g['hash']} {g['size'][0]}×{g['size'][1]} págs {len(g['pages'])} ({g['reason']})", file=out)
    if report.ocr:
        matches = sum(v["coincidencias"] for v in report.ocr.values())
        print(f"  OCR: {len(report.ocr)} páginas, {matches} coincidencias", file=out)
    counts = report.counts()
    print("  sustituciones: " + (", ".join(f"{k} ×{v}" for k, v in counts.items()) or "ninguna"), file=out)
    for w in report.warnings:
        print(f"  aviso: {w}", file=out)
    if report.near_misses:
        print(f"  coincidencias rozadas (revisa las reglas): {len(report.near_misses)}", file=out)
        for n in report.near_misses[:10]:
            print(f"    pág {n.page}: …{n.text}… ('{n.rule}')", file=out)
    for b in report.blocked:
        print(f"  BLOQUEADA (pág {b.page}, {b.source}): '{b.text}' de '{b.rule}' toca '{b.protected_by}'", file=out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="anonymize",
        description="Zonas, logos, texto y OCR (fases 4 y 5) según una config YAML.",
    )
    parser.add_argument("pdf", type=Path, help="PDF de entrada")
    parser.add_argument("-o", "--output", type=Path, required=True, help="PDF de salida")
    parser.add_argument("--config", type=Path, required=True, help="config YAML del cliente")
    parser.add_argument(
        "--report", type=Path, help="JSON con cada sustitución (contiene valores originales: en data/)"
    )
    parser.add_argument(
        "--map",
        type=Path,
        help="mapa de sustitución (data/maps/…): se reutiliza si existe y se guarda ampliado",
    )
    args = parser.parse_args(argv)

    from sanitizer.rules import ConfigError, load_config  # evita import circular

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        parser.error(f"config inválida: {exc}")
    try:
        report = anonymize(args.pdf, args.output, config, args.map)
    except (ValueError, MapError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"{args.pdf} → {args.output}")
    print_summary(report)
    if args.report:
        if "data" not in args.report.resolve().parts:
            print(f"AVISO: {args.report} está fuera de data/ y contiene valores originales.", file=sys.stderr)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report.to_json(), encoding="utf-8")
        print(f"  informe: {args.report}")
    if args.map:
        print(f"  mapa: {args.map}")
    if report.problems:
        print("VERIFICACIÓN FALLIDA:", file=sys.stderr)
        for p in report.problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print("  verificación (relectura del texto): OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
