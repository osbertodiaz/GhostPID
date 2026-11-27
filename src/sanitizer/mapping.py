"""Fase 6 · Mapa de sustitución y reversión.

El mapa guarda, por cada denominación genérica (EPC, VENDOR-1, DOC-REF-3…),
los valores originales a los que sustituye. Sirve para dos cosas:

- que la numeración sea la misma en todos los documentos de un proyecto: si
  se anonimiza otro PDF con el mismo mapa, se reutiliza y se amplía;
- revertir la respuesta de un LLM: devolver los nombres reales a un texto
  escrito con las denominaciones genéricas.

**El mapa contiene los valores que se querían ocultar**: solo se guarda dentro
de una carpeta `data/` (fuera de git) y con permisos 600. `save_map` se niega
a escribirlo en cualquier otro sitio.

Formato (JSON):

    {
      "version": 1,
      "documents": [{"source": "plano.pdf", "sha256": "…", "output": "…",
                     "output_sha256": "…", "config": "…", "date": "…"}],
      "entries": {
        "VENDOR-1": {"template": "VENDOR-{n}", "n": 1, "rule": "vendors",
                     "category": "manufacturer",
                     "originals": {"Initech Pumps": {"count": 3, "exact": 3}}}
      }
    }

`exact` cuenta las apariciones leídas del texto real (no de OCR): al
revertir se prefiere ese original, porque el OCR puede leer "GL0BEX".
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sanitizer.redact import Namer, ocr_normalize

MAP_VERSION = 1


class MapError(Exception):
    """Mapa inválido o en un sitio no permitido."""


# --------------------------------------------------------------------------
# Guardar y cargar
# --------------------------------------------------------------------------


def inside_data(path: Path) -> bool:
    """¿La ruta está dentro de una carpeta data/?"""
    return "data" in path.resolve().parent.parts


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def empty_map() -> dict[str, Any]:
    return {"version": MAP_VERSION, "documents": [], "entries": {}}


def load_map(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise MapError(f"no existe el mapa {path}") from None
    except json.JSONDecodeError as exc:
        raise MapError(f"{path}: JSON mal formado: {exc}") from None
    if not isinstance(data, dict) or data.get("version") != MAP_VERSION:
        raise MapError(f"{path}: no es un mapa de sustitución (versión {MAP_VERSION})")
    for key in ("documents", "entries"):
        if not isinstance(data.get(key), (list if key == "documents" else dict)):
            raise MapError(f"{path}: falta o es inválido '{key}'")
    for value, entry in data["entries"].items():
        if not isinstance(entry, dict) or "template" not in entry or not isinstance(entry.get("originals"), dict):
            raise MapError(f"{path}: entrada inválida para '{value}'")
    return data


def save_map(data: dict[str, Any], path: str | Path) -> Path:
    """Guarda el mapa; solo dentro de una carpeta data/ y con permisos 600."""
    path = Path(path)
    if not inside_data(path):
        raise MapError(
            f"el mapa contiene los valores originales: solo se guarda dentro de data/ (no en {path})"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return path


def namer_from_map(data: dict[str, Any] | None) -> Namer:
    return Namer.from_entries(data["entries"]) if data else Namer()


def update_map(
    data: dict[str, Any] | None,
    namer: Namer,
    source: str | Path,
    output: str | Path,
    config_name: str,
) -> dict[str, Any]:
    """Mapa con las entradas del `namer` y el registro del documento procesado."""
    data = dict(data) if data else empty_map()
    data["entries"] = {k: v for k, v in namer.entries.items()}
    source, output = Path(source), Path(output)
    record = {
        "source": source.name,
        "sha256": file_sha256(source),
        "output": output.name,
        "output_sha256": file_sha256(output),
        "config": config_name,
        "date": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    # Si el mismo documento se vuelve a procesar, se sustituye su registro
    data["documents"] = [d for d in data.get("documents", []) if d.get("sha256") != record["sha256"]]
    data["documents"].append(record)
    return data


# --------------------------------------------------------------------------
# Reversión
# --------------------------------------------------------------------------


def _same_entity(a: str, b: str) -> bool:
    """¿Dos originales son la misma entidad?

    Sí si coinciden tras normalizar las confusiones de OCR, si uno contiene al
    otro ("ACME" ⊂ "ACME Engineering") o si uno abrevia al otro palabra a
    palabra, ignorando la puntuación ("ACME Eng." → "ACME Engineering").
    """
    na, nb = (" ".join(ocr_normalize(x).split()) for x in (a, b))
    if na == nb or na in nb or nb in na:
        return True
    ta, tb = (re.findall(r"[A-Z0-9]+", x) for x in (na, nb))
    short, long = sorted((ta, tb), key=len)
    return bool(short) and all(t.startswith(s) for s, t in zip(short, long))


def entities(entry: dict[str, Any]) -> list[list[str]]:
    """Originales de una entrada agrupados por entidad."""
    groups: list[list[str]] = []
    for original in entry["originals"]:
        for group in groups:
            if any(_same_entity(original, other) for other in group):
                group.append(original)
                break
        else:
            groups.append([original])
    return groups


def canonical(entry: dict[str, Any]) -> str:
    """Original preferido: leído del texto real, el más frecuente y, a igualdad, el más largo."""
    return max(
        entry["originals"].items(),
        key=lambda kv: (kv[1].get("exact", 0) > 0, kv[1].get("exact", 0), kv[1].get("count", 0), len(kv[0])),
    )[0]


@dataclass
class RevertReport:
    counts: dict[str, int] = field(default_factory=dict)  # denominación → reversiones
    ambiguous: dict[str, list[str]] = field(default_factory=dict)  # denominación → entidades


def revert(text: str, data: dict[str, Any]) -> tuple[str, RevertReport]:
    """Devuelve los valores originales a un texto con denominaciones genéricas.

    - De la denominación más larga a la más corta (VENDOR-10 antes que VENDOR-1).
    - Solo palabras completas, sin distinguir mayúsculas (el LLM puede escribir
      "Vendor-1").
    - Si una denominación agrupa varias entidades distintas (p. ej. SITE para
      tres lugares), se usa la preferida y se informa.
    """
    report = RevertReport()
    targets = {}
    for value, entry in data["entries"].items():
        if not value or not entry["originals"]:
            continue  # sustituciones por vacío: no hay nada que revertir
        targets[value.casefold()] = (value, canonical(entry))
        groups = entities(entry)
        if len(groups) > 1:
            report.ambiguous[value] = [g[0] for g in groups]
    if not targets:
        return text, report

    alternation = "|".join(re.escape(v) for v, _ in sorted(targets.values(), key=lambda t: -len(t[0])))
    rx = re.compile(rf"(?<![\w-])(?:{alternation})(?![\w-])", re.IGNORECASE)

    def swap(m: re.Match[str]) -> str:
        value, original = targets[m.group(0).casefold()]
        report.counts[value] = report.counts.get(value, 0) + 1
        return original

    return rx.sub(swap, text), report


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="revert",
        description="Devuelve los nombres reales a un texto escrito con denominaciones genéricas.",
    )
    parser.add_argument("--map", type=Path, required=True, help="mapa de sustitución (data/maps/…)")
    parser.add_argument("input", nargs="?", type=Path, help="fichero de texto (por defecto, la entrada estándar)")
    parser.add_argument("-o", "--output", type=Path, help="fichero de salida (por defecto, la salida estándar)")
    args = parser.parse_args(argv)

    try:
        data = load_map(args.map)
    except MapError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    text = args.input.read_text(encoding="utf-8") if args.input else sys.stdin.read()
    result, report = revert(text, data)
    if args.output:
        args.output.write_text(result, encoding="utf-8")
    else:
        sys.stdout.write(result)

    total = sum(report.counts.values())
    print(f"revertidas: {total} ({', '.join(f'{k} ×{v}' for k, v in report.counts.items()) or '-'})", file=sys.stderr)
    for value, groups in report.ambiguous.items():
        if value in report.counts:
            print(
                f"AVISO: '{value}' agrupa varias entidades ({' | '.join(groups)}); "
                f"se ha usado '{canonical(data['entries'][value])}'",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
