"""Fase 2 · Reglas: esquema YAML por cliente o plantilla.

Una config tiene cuatro bloques:

- `whitelist`: regex de lo que se conserva siempre (tags, líneas, tamaños,
  unidades, condiciones de diseño, alarmas, interlocks). Tiene prioridad
  absoluta: nada que encaje con la lista blanca se sustituye.
- `substitutions`: lo que se reemplaza por una denominación genérica (EPC, OPR,
  VENDOR-n, DOC-REF-n…), como términos literales o como regex.
- `zones`: rectángulos por página que se eliminan enteros (cajetín, bloques
  laterales, logos), en fracción de página o en milímetros.
- `detection`: umbrales de los detectores de diagnóstico, por si un set de
  planos se sale de lo calibrado.
- `metadata`: qué hacer con anotaciones, enlaces, capas y marcadores.
- `redaction`: cómo se redacta el texto (relleno, qué se borra bajo cada
  redacción según el tipo de página, capas ocultas, anotaciones).

Las configs pueden heredar de otra con `extends` (ruta relativa al fichero).
Al heredar:

- Las reglas y zonas se combinan por `id`: si el hijo repite un `id`, sus
  claves sobrescriben las del padre (p. ej. añadir `exceptions` a una regla
  base sin repetir su patrón); si no, se añaden.
- `detection`, `metadata` y `redaction` se combinan clave a clave.
- `disable: [id, …]` quita reglas o zonas heredadas.

Cada regla lleva ejemplos. Sirven para validar las regex y para detectar
solapes entre lista blanca y sustituciones (ver `check_overlaps`): comparar dos
regex de forma exacta no es viable, pero sí probar cada una contra los
ejemplos de la otra.

Este módulo no toca ningún PDF: solo carga, valida y explica reglas.
"""

from __future__ import annotations

import argparse
import re
import string
import sys
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from sanitizer.inventory import DetectionParams
from sanitizer.metadata import MetadataPolicy
from sanitizer.redact import RedactionPolicy

SCHEMA_VERSION = 1

# Caracteres que forman parte de un tag. Con `boundary: token`, un patrón solo
# encaja si no va pegado a ninguno de ellos: así "PID-001" no se considera un
# tag dentro de la referencia documental "ACM-2041-PID-001".
TOKEN_CHARS = r"A-Za-z0-9_\-/"
TOKEN_START = rf"(?<![{TOKEN_CHARS}])"
TOKEN_END = rf"(?![{TOKEN_CHARS}])"

# Términos más cortos que esto pueden aparecer dentro de otras palabras o tags
SHORT_TERM_LENGTH = 3

# Números sueltos típicos de un P&ID (números de lazo, de equipo, de línea):
# una sustitución que encaje con alguno puede destrozar tags apilados
BARE_NUMBER_SAMPLES = ("12", "101", "4711", "01234", "070100", "123456")

MM_TO_PT = 72 / 25.4

_ID = r"^[a-z0-9][a-z0-9_\-]*$"
_PAGE_RANGE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")


class ConfigError(Exception):
    """Config inválida: YAML mal formado, esquema incorrecto o herencia rota."""

    def __init__(self, source: str | Path, messages: list[str]):
        self.source = str(source)
        self.messages = messages
        super().__init__(f"{source}: " + "; ".join(messages))


@lru_cache(maxsize=1024)
def _compile(pattern: str, flags: int) -> re.Pattern[str]:
    return re.compile(pattern, flags)


def _check_regex(pattern: str, label: str) -> None:
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"regex inválida en {label} ({pattern!r}): {exc}") from None


def _wrap(pattern: str, boundary: str, exceptions: list[str] | None = None) -> str:
    """Aplica límites de token y excepciones a un patrón."""
    body = f"(?:{pattern})"
    if exceptions:
        # La excepción solo bloquea si ocupa el token entero
        alts = "|".join(f"(?:{e})" for e in exceptions)
        body = f"(?!(?:{alts}){TOKEN_END}){body}"
    if boundary == "token":
        return f"{TOKEN_START}{body}{TOKEN_END}"
    return body


def term_regex(term: str, word_boundaries: bool = True) -> str:
    """Regex de un término literal: palabra completa y espacios flexibles.

    Los espacios admiten saltos de línea o varios espacios, porque la
    extracción de texto de un PDF rara vez conserva el espaciado original.
    """
    words = term.split()
    body = r"\s+".join(re.escape(w) for w in words)
    return rf"(?<!\w){body}(?!\w)" if word_boundaries else body


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------
# Lista blanca
# --------------------------------------------------------------------------


class WhitelistRule(_Strict):
    """Patrón de algo que se conserva siempre."""

    id: str = Field(pattern=_ID)
    description: str = ""
    pattern: str
    case_sensitive: bool = True
    boundary: Literal["token", "none"] = "token"
    # Formas que encajan con el patrón pero NO deben protegerse (p. ej. un
    # código de proyecto con aspecto de tag). Regex que deben ocupar el token.
    exceptions: list[str] = Field(default_factory=list)
    examples: list[str] = Field(min_length=1)
    counter_examples: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate(self) -> WhitelistRule:
        _check_regex(self.pattern, "pattern")
        for exc in self.exceptions:
            _check_regex(exc, "exceptions")
        _check_regex(self.effective_pattern, "pattern (con límites y excepciones)")
        rx = self.regex
        bad = [e for e in self.examples if not rx.fullmatch(e)]
        if bad:
            raise ValueError(f"estos ejemplos no encajan enteros con el patrón: {bad}")
        wrong = [e for e in self.counter_examples if rx.search(e)]
        if wrong:
            raise ValueError(f"estos contraejemplos encajan con el patrón y no deberían: {wrong}")
        return self

    @property
    def effective_pattern(self) -> str:
        return _wrap(self.pattern, self.boundary, self.exceptions)

    @property
    def regex(self) -> re.Pattern[str]:
        return _compile(self.effective_pattern, 0 if self.case_sensitive else re.IGNORECASE)


# --------------------------------------------------------------------------
# Sustituciones
# --------------------------------------------------------------------------


class Category(str, Enum):
    """Qué tipo de información sensible cubre una sustitución."""

    company = "company"  # empresa de ingeniería, contratista (→ EPC)
    operator = "operator"  # propietario u operador de la planta (→ OPR)
    manufacturer = "manufacturer"  # fabricantes visibles (→ VENDOR-n)
    person = "person"  # nombres, iniciales, usuarios CAD
    location = "location"  # planta, ciudad, dirección
    project = "project"  # códigos de proyecto o contrato
    document_ref = "document_ref"  # referencias documentales (→ DOC-REF-n)
    other = "other"


class SubstitutionRule(_Strict):
    """Información sensible y su denominación genérica."""

    id: str = Field(pattern=_ID)
    category: Category
    description: str = ""
    # Texto genérico de sustitución. Admite {n}: número correlativo por cada
    # valor distinto encontrado en el documento (VENDOR-1, VENDOR-2…).
    # Cadena vacía = eliminar sin sustituir.
    replacement: str
    terms: list[str] = Field(default_factory=list)
    patterns: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    case_sensitive: bool = False
    boundary: Literal["token", "none"] = "token"

    @field_validator("terms")
    @classmethod
    def _clean_terms(cls, terms: list[str]) -> list[str]:
        cleaned = [t.strip() for t in terms]
        if any(not t for t in cleaned):
            raise ValueError("hay términos vacíos")
        return cleaned

    @field_validator("replacement")
    @classmethod
    def _check_replacement(cls, value: str) -> str:
        try:
            fields = {f for _, f, _, _ in string.Formatter().parse(value) if f is not None}
        except ValueError as exc:
            raise ValueError(f"replacement mal formado: {exc}") from None
        if fields - {"n"}:
            raise ValueError(f"replacement solo admite {{n}}, no {sorted(fields - {'n'})}")
        return value

    @model_validator(mode="after")
    def _validate(self) -> SubstitutionRule:
        if not self.terms and not self.patterns:
            raise ValueError("hace falta al menos un término (terms) o un patrón (patterns)")
        for p in self.patterns:
            _check_regex(p, "patterns")
        if self.patterns:
            if not self.examples:
                raise ValueError("los patrones necesitan ejemplos (examples)")
            unused = [p for p, rx in zip(self.patterns, self.pattern_regexes) if not any(rx.fullmatch(e) for e in self.examples)]
            if unused:
                raise ValueError(f"estos patrones no encajan con ningún ejemplo: {unused}")
            orphan = [e for e in self.examples if not any(rx.fullmatch(e) for rx in self.pattern_regexes)]
            if orphan:
                raise ValueError(f"estos ejemplos no encajan enteros con ningún patrón: {orphan}")
        return self

    @property
    def _flags(self) -> int:
        return 0 if self.case_sensitive else re.IGNORECASE

    @property
    def pattern_regexes(self) -> list[re.Pattern[str]]:
        return [_compile(_wrap(p, self.boundary), self._flags) for p in self.patterns]

    @property
    def matchers(self) -> list[re.Pattern[str]]:
        """Regex de términos y patrones, en ese orden."""
        terms = [_compile(term_regex(t), self._flags) for t in self.terms]
        return terms + self.pattern_regexes

    @property
    def relaxed_matchers(self) -> list[re.Pattern[str]]:
        """Como `matchers`, pero los términos encajan también dentro de otras
        palabras (capas OCR con palabras pegadas)."""
        terms = [_compile(term_regex(t, word_boundaries=False), self._flags) for t in self.terms]
        return terms + self.pattern_regexes

    @property
    def samples(self) -> list[str]:
        """Textos que esta regla debe sustituir: términos y ejemplos."""
        return self.terms + self.examples


# --------------------------------------------------------------------------
# Zonas
# --------------------------------------------------------------------------


class Zone(_Strict):
    """Rectángulo que se elimina entero (texto, trazos e imágenes).

    Coordenadas sobre la página tal como se ve (ya rotada), con el origen en
    la esquina `anchor` y los ejes hacia el interior de la página:
    con `anchor: bottom-right`, `[0, 0, 60, 40]` en mm es un recuadro de
    60 × 40 mm pegado a la esquina inferior derecha, en cualquier tamaño de hoja.
    """

    id: str = Field(pattern=_ID)
    description: str = ""
    rect: tuple[float, float, float, float]
    unit: Literal["fraction", "mm"] = "fraction"
    anchor: Literal["top-left", "top-right", "bottom-left", "bottom-right"] = "top-left"
    # "all" o lista de páginas (1 = primera, -1 = última) y rangos "3-7"
    pages: Literal["all"] | list[int | str] = "all"
    orientation: Literal["any", "landscape", "portrait"] = "any"

    @model_validator(mode="after")
    def _validate(self) -> Zone:
        x0, y0, x1, y1 = self.rect
        if not (x0 < x1 and y0 < y1):
            raise ValueError(f"rect debe ser [x0, y0, x1, y1] con x0 < x1 e y0 < y1: {list(self.rect)}")
        if min(self.rect) < 0:
            raise ValueError(f"rect no admite valores negativos: {list(self.rect)}")
        if self.unit == "fraction" and max(self.rect) > 1:
            raise ValueError(f"con unit: fraction, rect va de 0 a 1: {list(self.rect)}")
        if self.pages != "all":
            if not self.pages:
                raise ValueError("pages vacío: usa 'all' o una lista de páginas")
            for item in self.pages:
                if isinstance(item, int):
                    if item == 0:
                        raise ValueError("las páginas empiezan en 1 (o -1 para la última)")
                else:
                    m = _PAGE_RANGE.match(item)
                    if not m or not 1 <= int(m.group(1)) <= int(m.group(2)):
                        raise ValueError(f"rango de páginas inválido: {item!r} (usa p. ej. '3-7')")
        return self

    def page_numbers(self, page_count: int) -> set[int]:
        """Páginas (1-based) a las que se aplica, sin mirar la orientación."""
        if self.pages == "all":
            return set(range(1, page_count + 1))
        out: set[int] = set()
        for item in self.pages:
            if isinstance(item, int):
                n = item if item > 0 else page_count + 1 + item
                if 1 <= n <= page_count:
                    out.add(n)
            else:
                a, b = (int(x) for x in _PAGE_RANGE.match(item).groups())
                out.update(range(a, min(b, page_count) + 1))
        return out

    def applies_to(self, page_number: int, page_count: int, width: float, height: float) -> bool:
        if page_number not in self.page_numbers(page_count):
            return False
        if self.orientation == "landscape":
            return width >= height
        if self.orientation == "portrait":
            return height > width
        return True

    def resolve(self, width: float, height: float) -> tuple[float, float, float, float]:
        """Rectángulo en puntos, origen arriba a la izquierda, recortado a la página."""
        x0, y0, x1, y1 = self.rect
        if self.unit == "fraction":
            x0, x1, y0, y1 = x0 * width, x1 * width, y0 * height, y1 * height
        else:
            x0, x1, y0, y1 = (v * MM_TO_PT for v in (x0, x1, y0, y1))
        if self.anchor.endswith("right"):
            x0, x1 = width - x1, width - x0
        if self.anchor.startswith("bottom"):
            y0, y1 = height - y1, height - y0
        return (max(x0, 0.0), max(y0, 0.0), min(x1, width), min(y1, height))


# --------------------------------------------------------------------------
# Config completa
# --------------------------------------------------------------------------


class Config(_Strict):
    version: Literal[1] = SCHEMA_VERSION
    name: str
    description: str = ""
    whitelist: list[WhitelistRule] = Field(default_factory=list)
    substitutions: list[SubstitutionRule] = Field(default_factory=list)
    zones: list[Zone] = Field(default_factory=list)
    detection: DetectionParams = Field(default_factory=DetectionParams)
    metadata: MetadataPolicy = Field(default_factory=MetadataPolicy)
    redaction: RedactionPolicy = Field(default_factory=RedactionPolicy)

    @model_validator(mode="after")
    def _unique_ids(self) -> Config:
        for block in ("whitelist", "substitutions", "zones"):
            ids = [r.id for r in getattr(self, block)]
            dup = sorted({i for i in ids if ids.count(i) > 1})
            if dup:
                raise ValueError(f"ids repetidos en {block}: {dup}")
        return self


# --------------------------------------------------------------------------
# Carga y herencia
# --------------------------------------------------------------------------

_RULE_BLOCKS = ("whitelist", "substitutions", "zones")


def load_config(path: str | Path) -> Config:
    """Carga una config YAML resolviendo `extends` y la valida."""
    path = Path(path)
    raw = _load_raw(path, ())
    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(path, _format_errors(exc, raw)) from None


def _load_raw(path: Path, chain: tuple[Path, ...]) -> dict[str, Any]:
    path = path.resolve()
    if path in chain:
        cycle = " → ".join(p.name for p in (*chain, path))
        raise ConfigError(path, [f"herencia circular: {cycle}"])
    if not path.is_file():
        raise ConfigError(path, ["no existe el fichero"])
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(path, [f"YAML mal formado: {exc}"]) from None
    if not isinstance(data, dict):
        raise ConfigError(path, ["la config debe ser un diccionario YAML"])

    parent_ref = data.pop("extends", None)
    disabled = data.pop("disable", [])
    if not isinstance(disabled, list):
        raise ConfigError(path, ["disable debe ser una lista de ids"])

    if parent_ref is None:
        merged = data
    else:
        if not isinstance(parent_ref, str):
            raise ConfigError(path, ["extends debe ser una ruta a otra config"])
        parent = _load_raw(path.parent / parent_ref, (*chain, path))
        merged = _merge(parent, data)

    if disabled:
        known = {r.get("id") for b in _RULE_BLOCKS for r in merged.get(b, []) if isinstance(r, dict)}
        missing = sorted(set(disabled) - known)
        if missing:
            raise ConfigError(path, [f"disable menciona ids que no existen: {missing}"])
        for block in _RULE_BLOCKS:
            if block in merged:
                merged[block] = [r for r in merged[block] if r.get("id") not in disabled]
    return merged


def _merge(parent: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    out = dict(parent)
    for key, value in child.items():
        if key in _RULE_BLOCKS and isinstance(value, list) and isinstance(out.get(key), list):
            out[key] = _merge_rules(out[key], value)
        elif key in ("detection", "metadata", "redaction") and isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _merge_rules(parent: list[Any], child: list[Any]) -> list[Any]:
    """Combina por id: mismo id → el hijo sobrescribe claves; id nuevo → se añade."""
    result = [dict(r) if isinstance(r, dict) else r for r in parent]
    index = {r.get("id"): i for i, r in enumerate(result) if isinstance(r, dict)}
    for rule in child:
        rid = rule.get("id") if isinstance(rule, dict) else None
        if rid is not None and rid in index:
            result[index[rid]] = {**result[index[rid]], **rule}
        else:
            result.append(rule)
    return result


def _deep_merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _format_errors(exc: ValidationError, raw: dict[str, Any]) -> list[str]:
    """Convierte los errores de pydantic en mensajes con la ruta del campo.

    En listas de reglas se muestra el id además del índice: whitelist[3:line_number].
    """
    messages = []
    for err in exc.errors():
        parts: list[str] = []
        node: Any = raw
        for loc in err["loc"]:
            if isinstance(loc, int):
                label = f"[{loc}]"
                if isinstance(node, list) and loc < len(node) and isinstance(node[loc], dict):
                    rid = node[loc].get("id")
                    label = f"[{loc}:{rid}]" if rid else label
                parts.append(label)
                node = node[loc] if isinstance(node, list) and loc < len(node) else None
            else:
                parts.append(("." if parts else "") + str(loc))
                node = node.get(loc) if isinstance(node, dict) else None
        msg = err["msg"].removeprefix("Value error, ")
        messages.append(f"{''.join(parts) or '(raíz)'}: {msg}")
    return messages


# --------------------------------------------------------------------------
# Solapes entre lista blanca y sustituciones
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Issue:
    severity: Literal["error", "warning"]
    message: str


def check_overlaps(config: Config) -> list[Issue]:
    """Detecta conflictos entre reglas probando cada una contra los ejemplos de las demás.

    - error: una sustitución modificaría un ejemplo protegido por la lista blanca.
    - error: un término o ejemplo a sustituir queda protegido entero por la
      lista blanca, así que nunca se sustituiría.
    - aviso: la lista blanca protege solo una parte de un término a sustituir
      (la sustitución quedaría a medias).
    - aviso: el mismo término aparece en dos sustituciones.
    - aviso: términos muy cortos, que pueden aparecer dentro de otras palabras.
    - aviso: patrones que encajan con números sueltos (números de lazo).
    - error: el texto de sustitución vuelve a encajar con una sustitución
      (la verificación posterior nunca pasaría).
    """
    issues: list[Issue] = []

    for wl in config.whitelist:
        for example in wl.examples:
            for sub in config.substitutions:
                for rx in sub.matchers:
                    m = rx.search(example)
                    if m:
                        issues.append(
                            Issue(
                                "error",
                                f"la sustitución '{sub.id}' modificaría '{m.group(0)}' dentro de "
                                f"'{example}', protegido por la lista blanca '{wl.id}'",
                            )
                        )
                        break

    for sub in config.substitutions:
        for sample in sub.samples:
            for wl in config.whitelist:
                m = wl.regex.search(sample)
                if not m:
                    continue
                if m.group(0) == sample:
                    issues.append(
                        Issue(
                            "error",
                            f"'{sample}' (sustitución '{sub.id}') encaja entero con la lista "
                            f"blanca '{wl.id}' y nunca se sustituiría. Si no es un tag, "
                            f"añade una excepción a '{wl.id}' (exceptions)",
                        )
                    )
                else:
                    issues.append(
                        Issue(
                            "warning",
                            f"'{sample}' (sustitución '{sub.id}') contiene '{m.group(0)}', "
                            f"protegido por la lista blanca '{wl.id}': la sustitución quedaría a medias",
                        )
                    )

    for sub in config.substitutions:
        numeric = [s for s in BARE_NUMBER_SAMPLES if any(rx.fullmatch(s) for rx in sub.pattern_regexes)]
        if numeric:
            issues.append(
                Issue(
                    "warning",
                    f"la sustitución '{sub.id}' encaja con números sueltos ({', '.join(numeric)}): "
                    f"puede alcanzar números de lazo o de equipo. Hazla más específica "
                    f"(prefijo, longitud exacta) o comprueba el resultado",
                )
            )
        sample = sub.replacement.format(n=1) if "{n}" in sub.replacement else sub.replacement
        if sample:
            for other in config.substitutions:
                if any(rx.search(sample) for rx in other.matchers):
                    issues.append(
                        Issue(
                            "error",
                            f"el texto de sustitución '{sample}' de '{sub.id}' vuelve a encajar "
                            f"con la sustitución '{other.id}'",
                        )
                    )

    seen: dict[str, str] = {}
    for sub in config.substitutions:
        for term in sub.terms:
            key = term.casefold()
            if key in seen and seen[key] != sub.id:
                issues.append(
                    Issue("warning", f"el término '{term}' está en '{seen[key]}' y en '{sub.id}'")
                )
            seen.setdefault(key, sub.id)
            if len(term) < SHORT_TERM_LENGTH:
                issues.append(
                    Issue(
                        "warning",
                        f"el término '{term}' (sustitución '{sub.id}') es muy corto y puede "
                        f"aparecer como palabra suelta en otros contextos",
                    )
                )
    return issues


# --------------------------------------------------------------------------
# Explicación de un texto (para afinar reglas)
# --------------------------------------------------------------------------


def explain(config: Config, text: str) -> list[str]:
    """Qué reglas encajan en `text` y qué pasaría con cada coincidencia."""
    lines = []
    protected: list[tuple[int, int]] = []
    for wl in config.whitelist:
        for m in wl.regex.finditer(text):
            protected.append(m.span())
            lines.append(f"  protegido  '{m.group(0)}' ← whitelist '{wl.id}'")
    for sub in config.substitutions:
        for rx in sub.matchers:
            for m in rx.finditer(text):
                a, b = m.span()
                clash = any(a < pb and pa < b for pa, pb in protected)
                verdict = "BLOQUEADO por la lista blanca" if clash else f"→ '{sub.replacement}'"
                lines.append(f"  sustituir  '{m.group(0)}' ← '{sub.id}' ({sub.category.value}) {verdict}")
    return lines or ["  (ninguna regla encaja)"]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="validate-config",
        description="Valida una config YAML de reglas y detecta solapes entre lista blanca y sustituciones.",
    )
    parser.add_argument("config", type=Path, help="config YAML (p. ej. configs/cliente.yaml)")
    parser.add_argument(
        "--check",
        action="append",
        default=[],
        metavar="TEXTO",
        help="muestra qué reglas encajan en TEXTO (se puede repetir)",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"ERROR en {exc.source}:", file=sys.stderr)
        for msg in exc.messages:
            print(f"  - {msg}", file=sys.stderr)
        return 1

    print(f"{config.name} ({args.config})")
    print(
        f"  lista blanca: {len(config.whitelist)} · sustituciones: {len(config.substitutions)}"
        f" · zonas: {len(config.zones)}"
    )
    issues = check_overlaps(config)
    errors = [i for i in issues if i.severity == "error"]
    for issue in issues:
        label = "ERROR" if issue.severity == "error" else "aviso"
        print(f"  {label}: {issue.message}")
    if not issues:
        print("  sin solapes entre lista blanca y sustituciones")

    for text in args.check:
        print(f"\n'{text}':")
        print("\n".join(explain(config, text)))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
