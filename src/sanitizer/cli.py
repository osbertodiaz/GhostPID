"""Fase 8 · CLI completa: `sanitize <pdf|carpeta>... --config configs/<cliente>.yaml`.

Por cada documento:

1. `anonymize`: zonas, logos, texto y OCR (fases 4 y 5), con mapa (fase 6).
2. `clean-metadata` (fase 3).
3. `audit` (fase 7).
4. Autocorrección: los restos que la auditoría encuentra por OCR se corrigen,
   se vuelven a limpiar los metadatos y se reaudita, con OCR solo en las
   páginas corregidas (`--repair N`, 1 por defecto; 0 la desactiva).

Salidas:

- `<out>/<doc>.sanitized.pdf`: el PDF que se puede compartir.
- `<reports>/<doc>/`: informes (anonimizado, metadatos, auditoría y PNG).
- `<maps>/<doc>.map.json` o el mapa común de `--map`: mapa de sustitución.

Informes y mapas contienen valores originales: solo dentro de data/. Los
intermedios (PDF todavía con metadatos) se guardan en el directorio del
informe y se borran al terminar, salvo con `--keep-intermediate`.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sanitizer import __version__
from sanitizer.audit import audit, write_audit
from sanitizer.mapping import MapError, inside_data, load_map, namer_from_map, save_map, update_map
from sanitizer.metadata import clean_metadata
from sanitizer.pipeline import anonymize
from sanitizer.repair import repair, repairable
from sanitizer.i18n import tr

# Nombres de salidas propias que no se vuelven a procesar al recorrer carpetas
_OWN_OUTPUTS = (".sanitized.", ".anon.", ".final.", ".meta.", ".redact.")


@dataclass
class DocResult:
    source: str
    output: str | None = None
    ok: bool = False
    error: str = ""
    substitutions: int = 0
    repaired_pages: list[int] = field(default_factory=list)
    leftovers: int = 0
    review_lines: int = 0
    seconds: float = 0.0


def sanitize_document(
    src: Path,
    config: Any,
    out_dir: Path,
    reports_dir: Path,
    map_path: Path,
    repair_passes: int = 1,
    run_audit: bool = True,
    keep_intermediate: bool = False,
) -> DocResult:
    """Pipeline completo para un documento."""
    started = time.monotonic()
    result = DocResult(src.name)
    report_dir = reports_dir / src.stem
    tmp = report_dir / "intermedio"
    tmp.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    output = out_dir / f"{src.stem}.sanitized.pdf"

    # 1-2) Anonimizar y limpiar metadatos
    anon = tmp / "anonimizado.pdf"
    anon_report = anonymize(src, anon, config, map_path)
    report_data = json.loads(anon_report.to_json())
    (report_dir / "anonimizado.json").write_text(json.dumps(report_data, indent=2, ensure_ascii=False), encoding="utf-8")
    meta = clean_metadata(anon, output, config.metadata)
    (report_dir / "metadatos.json").write_text(json.dumps(asdict(meta), indent=2, ensure_ascii=False), encoding="utf-8")
    result.output = str(output)
    result.substitutions = len(anon_report.replacements)

    if not run_audit:
        result.ok = not anon_report.problems
        result.leftovers = len(anon_report.problems)
    else:
        # 3) Auditoría
        findings = audit(output, config, report_dir, src, report_data, load_map(map_path), write=False)
        # 4) Autocorrección de los restos encontrados por OCR
        for n in range(repair_passes):
            if not repairable(findings.findings):
                break
            namer = namer_from_map(load_map(map_path))
            repaired = tmp / f"corregido-{n + 1}.pdf"
            pages = repair(output, repaired, config, findings.findings, namer)
            cleaned = tmp / f"corregido-{n + 1}.limpio.pdf"
            clean_metadata(repaired, cleaned, config.metadata)
            shutil.move(cleaned, output)
            save_map(update_map(load_map(map_path), namer, src, output, config.name), map_path)
            result.repaired_pages = sorted(set(result.repaired_pages) | {p + 1 for p in pages})
            findings = audit(
                output,
                config,
                report_dir,
                src,
                report_data,
                load_map(map_path),
                ocr_only=pages,
                previous=findings,
                write=False,
            )
        findings.checks["autocorrección"] = {"páginas": result.repaired_pages or "-"}
        write_audit(findings, report_dir)
        result.ok = findings.ok
        result.leftovers = sum(1 for f in findings.findings if f.severity == "error")
        result.review_lines = len(findings.review_lines)

    # El mapa apunta al PDF final
    data = load_map(map_path)
    save_map(update_map(data, namer_from_map(data), src, output, config.name), map_path)
    if not keep_intermediate:
        shutil.rmtree(tmp, ignore_errors=True)
    result.seconds = time.monotonic() - started
    return result


def collect_inputs(inputs: list[Path], exclude: list[Path]) -> list[Path]:
    """PDF de entrada: ficheros sueltos y carpetas (recursivo), sin salidas propias."""
    excluded = [e.resolve() for e in exclude]
    found: list[Path] = []
    for item in inputs:
        candidates = sorted(p for p in item.rglob("*") if p.suffix.lower() == ".pdf") if item.is_dir() else [item]
        for p in candidates:
            resolved = p.resolve()
            if any(resolved.is_relative_to(e) for e in excluded):
                continue
            if any(tag in p.name for tag in _OWN_OUTPUTS):
                continue
            if resolved not in [f.resolve() for f in found]:
                found.append(p)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sanitize",
        description=tr(
            "Anonimiza P&ID y PFD en PDF: zonas, logos, texto, OCR, metadatos y auditoría.",
            "Anonymizes P&IDs and PFDs in PDF: zones, logos, text, OCR, metadata and audit.",
        ),
    )
    parser.add_argument("inputs", nargs="+", type=Path, help=tr("PDF o carpetas (se recorren recursivamente)", "PDFs or folders (walked recursively)"))
    parser.add_argument("--config", type=Path, required=True, help=tr("config YAML del cliente (configs/<cliente>.yaml)", "client YAML config (configs/<client>.yaml)"))
    parser.add_argument("--out", type=Path, default=Path("data/out"), help=tr("carpeta de los PDF anonimizados", "folder for the anonymized PDFs"))
    parser.add_argument("--reports", type=Path, default=Path("data/reports"), help=tr("carpeta de informes (en data/)", "reports folder (inside data/)"))
    parser.add_argument("--maps", type=Path, default=Path("data/maps"), help=tr("carpeta de mapas por documento (en data/)", "per-document maps folder (inside data/)"))
    parser.add_argument("--map", type=Path, help=tr("mapa común para todo el proyecto (numeración compartida)", "shared map for the whole project (shared numbering)"))
    parser.add_argument("--repair", type=int, default=1, metavar="N", help=tr("pasadas de autocorrección (0 = ninguna)", "self-repair passes (0 = none)"))
    parser.add_argument("--no-audit", action="store_true", help=tr("no auditar (más rápido, menos seguro)", "skip the audit (faster, less safe)"))
    parser.add_argument("--keep-intermediate", action="store_true", help=tr("conservar los PDF intermedios", "keep the intermediate PDFs"))
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    from sanitizer.rules import ConfigError, check_overlaps, load_config  # evita import circular

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        parser.error(tr("config inválida: {exc}", "invalid config: {exc}", exc=exc))
    errors = [i for i in check_overlaps(config) if i.severity == "error"]
    if errors:
        for issue in errors:
            print(tr("ERROR en la config: {msg}", "ERROR in the config: {msg}", msg=issue.message), file=sys.stderr)
        return 1
    for folder, label in ((args.reports, "--reports"), (args.maps, "--maps")):
        if not inside_data(folder / "x"):
            parser.error(tr("{label} debe estar dentro de data/: contiene valores originales", "{label} must be inside data/: it contains original values", label=label))
    if args.map is not None and not inside_data(args.map):
        parser.error(tr("--map debe estar dentro de data/: contiene valores originales", "--map must be inside data/: it contains original values"))
    if args.repair < 0:
        parser.error(tr("--repair no puede ser negativo", "--repair cannot be negative"))

    pdfs = collect_inputs(args.inputs, [args.out, args.reports, args.maps])
    if not pdfs:
        parser.error(tr("no se encontró ningún PDF", "no PDF found"))

    results: list[DocResult] = []
    for pdf in pdfs:
        map_path = args.map or args.maps / f"{pdf.stem}.map.json"
        print(f"→ {pdf}", flush=True)
        try:
            doc = sanitize_document(
                pdf, config, args.out, args.reports, map_path, args.repair, not args.no_audit, args.keep_intermediate
            )
        except (ValueError, MapError, OSError) as exc:
            doc = DocResult(pdf.name, error=str(exc))
        except Exception as exc:  # un PDF problemático no debe parar el lote
            doc = DocResult(pdf.name, error=f"{type(exc).__name__}: {exc}")
        results.append(doc)
        status = "OK" if doc.ok else ("ERROR" if doc.error else tr("FALLOS", "FAILED"))
        print(
            tr("  {status} · {n} sustituciones · {s:.0f} s", "  {status} · {n} substitutions · {s:.0f} s", status=status, n=doc.substitutions, s=doc.seconds)
            + (f" · {doc.error}" if doc.error else "")
        )

    print()
    print(
        tr(
            "| Documento | Resultado | Sustituciones | Páginas corregidas | Restos | Revisar | Tiempo |",
            "| Document | Result | Substitutions | Repaired pages | Leftovers | Review | Time |",
        )
    )
    print("|---|---|---:|---|---:|---:|---:|")
    for d in results:
        status = "OK" if d.ok else ("ERROR" if d.error else tr("FALLOS", "FAILED"))
        pages = ", ".join(map(str, d.repaired_pages)) or "-"
        print(f"| {d.source} | {status} | {d.substitutions} | {pages} | {d.leftovers} | {d.review_lines} | {d.seconds:.0f} s |")
    print(tr("\nInformes en {dir}/<documento>/auditoria.md", "\nReports in {dir}/<document>/auditoria.md", dir=args.reports))
    return 0 if all(d.ok for d in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
