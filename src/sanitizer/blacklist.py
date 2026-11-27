"""Lista negra de la industria: nombres que el software busca siempre.

Base de datos en CSV con empresas que aparecen en P&ID y PFD: ingenierías
EPC, licenciantes de tecnología, fabricantes de equipos e instrumentos y
operadores o propietarios de plantas (petróleo y gas, química, gases
industriales, farmacéutica, energía). La config la carga con el bloque
`blacklist` y la convierte en sustituciones `kb-<categoría>`:

    blacklist:
      files: [blacklist/*.csv]        # relativo a la config que lo declara
      user_file: blacklist/propia.csv # donde escriben `blacklist add` y la UI
      categories: [epc, licensor, manufacturer, operator]
      industries: []                  # vacío = todas
      replacements: {manufacturer: VENDOR-{n}}
      exclude: [Vega]                 # términos que no se buscan

Columnas del CSV (separador "," o ";", UTF-8; se abre bien en Excel):

    name,aliases,category,segment,industries,country,active,notes

- `name`: nombre principal de la entidad.
- `aliases`: otras formas separadas por "|" (siglas, marcas, nombres
  antiguos). Todas reciben la misma denominación: VENDOR-3 es una empresa,
  se escriba como se escriba.
- `category`: epc, licensor, manufacturer u operator (también en español:
  ingenieria, licenciante, fabricante, operador).
- `industries`: oil_gas, chemicals, gases, pharma, power, general
  separadas por "|" (vacío = general).
- `active`: "no" desactiva la fila sin borrarla.

Los términos se buscan como palabras completas, también en OCR y en la
auditoría: "Linde" no encaja dentro de "CYLINDER". Las siglas en mayúsculas
de hasta 5 caracteres (ABB, KBR, UOP) distinguen mayúsculas.

Salvaguardas: se rechazan términos de menos de 3 caracteres y los que son
solo vocabulario de P&ID ("Shell" es la carcasa de un cambiador, "Crane" una
grúa, "Total" un caudal), así como los duplicados entre entidades. Para esas
empresas se usan formas inequívocas ("Shell Global Solutions").
"""

from __future__ import annotations

import argparse
import csv
import glob
import io
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sanitizer.redact import ocr_normalize
from sanitizer.i18n import tr

COLUMNS = ("name", "aliases", "category", "segment", "industries", "country", "active", "notes")
ALIAS_SEP = "|"
RULE_PREFIX = "kb-"
MIN_TERM_LENGTH = 3

# Categoría del CSV → (categoría de la sustitución, denominación por defecto)
CATEGORIES: dict[str, tuple[str, str]] = {
    "epc": ("company", "EPC-{n}"),
    "licensor": ("company", "LICENSOR-{n}"),
    "manufacturer": ("manufacturer", "VENDOR-{n}"),
    "operator": ("operator", "OPR-{n}"),
}
_CATEGORY_LABELS = {
    "epc": ("ingenierías y contratistas EPC", "engineering firms and EPC contractors"),
    "licensor": ("licenciantes de tecnología", "technology licensors"),
    "manufacturer": ("fabricantes de equipos e instrumentos", "equipment and instrument manufacturers"),
    "operator": ("operadores y propietarios de plantas", "plant operators and owners"),
}


def category_label(category: str) -> str:
    """Descripción de una categoría del CSV en el idioma actual."""
    return tr(*_CATEGORY_LABELS[category])
_CATEGORY_SYNONYMS = {
    "ingenieria": "epc",
    "ingeniería": "epc",
    "contratista": "epc",
    "licenciante": "licensor",
    "fabricante": "manufacturer",
    "vendor": "manufacturer",
    "operador": "operator",
    "propietario": "operator",
    "owner": "operator",
}
INDUSTRIES = ("oil_gas", "chemicals", "gases", "pharma", "power", "general")
_TRUE = {"", "si", "sí", "s", "yes", "y", "true", "1", "x"}
_FALSE = {"no", "n", "false", "0"}

# Vocabulario de P&ID: un término de una sola palabra que esté aquí (o uno
# cuyas palabras estén todas aquí) destrozaría el plano. Se compara tras
# quitar puntuación y pasar a mayúsculas.
PID_VOCABULARY = frozenset(
    """
    SHELL TUBE TOTAL CRANE CARRIER BOURDON RASCHIG PALL INTALOX ANNUBAR TEMA NNE TCE HORA WEIR CLG CHECK GATE GLOBE BALL PLUG BUTTERFLY NEEDLE DIAPHRAGM PINCH
    UNION NATIONAL GENERAL STANDARD CONTINENTAL CONSOLIDATED UNIVERSAL INTERNATIONAL
    INDUSTRIAL INDUSTRIES GLOBAL ATLAS APOLLO DELTA ALPHA BETA GAMMA SIGMA OMEGA TITAN
    VULCAN PHOENIX EAGLE FALCON HUNTER MONO ECLIPSE VERTEX CRITERION ENTERPRISE PIONEER
    WOOD STEEL IRON COPPER AIR GAS OIL WATER STEAM POWER PROCESS PROCESSES CONTROL
    CONTROLS VALVE VALVES PUMP PUMPS FLOW LEVEL PRESSURE TEMPERATURE SYSTEM SYSTEMS
    ENGINEERING ENERGY CHEMICAL CHEMICALS VENT DRAIN FLARE FILTER FILTERS MIXER TANK
    VESSEL TOWER COLUMN REACTOR DRUM BOILER HEATER COOLER CONDENSER EXCHANGER
    COMPRESSOR TURBINE MOTOR FAN BLOWER SKID PACKAGE VENDOR SUPPLIER CLIENT OWNER
    CONTRACTOR LICENSOR MANUFACTURER MODEL TYPE SERIES NOTE NOTES DETAIL TYPICAL HOLD
    SPARE FUTURE EXISTING NEW OPEN CLOSED DESIGN SERVICE UTILITY UTILITIES PLANT UNIT
    AREA SECTION FEED PRODUCT RECYCLE RETURN SUPPLY SUCTION DISCHARGE INLET
    OUTLET SAMPLE ANALYZER SAFETY RELIEF RUPTURE DISC SEAL SEALS BEARING COUPLING
    GEAR DRIVE NITROGEN OXYGEN HYDROGEN ARGON CARBON SULPHUR SULFUR
    INSTRUMENT INSTRUMENTS COOLING HEATING TRACING INSULATION LINE PIPE PIPING
    FLANGE SPOOL REDUCER TEE ELBOW STRAINER TRAP SILENCER EJECTOR NOZZLE
    MOL NOV DEC CAT OXY BMS TLV ART SOL NEL API ASME ANSI DIN ISO ISA IEC NFPA MSS
    DCS PLC SIS ESD MCC VFD UPS HVAC LNG LPG CNG NGL FCC CCR HDS HDT SRU ASU PSA TYP
    BOP TOP BOC COP FAT SAT DATA SHEET REV REVISION DWG DRAWING SHEET SCALE DATE
    VALVULA VALVULAS BOMBA BOMBAS TANQUE AGUA AIRE VAPOR ACEITE COLUMNA REACTOR
    CALDERA HORNO FILTRO MEZCLADOR SEPARADOR INTERCAMBIADOR COMPRESOR MOTOR LINEA
    TUBERIA PLANTA UNIDAD AREA NOTA NOTAS DETALLE TIPICO FUTURO EXISTENTE NUEVO
    ABIERTO CERRADO DISENO SERVICIO PROCESO CONTROL SEGURIDAD ALIVIO
    """.split()
)


class BlacklistError(Exception):
    """CSV de la lista negra inválido: cada mensaje lleva fichero y línea."""

    def __init__(self, messages: list[str]):
        self.messages = messages
        super().__init__("; ".join(messages))


# --------------------------------------------------------------------------
# Política (bloque `blacklist` de la config)
# --------------------------------------------------------------------------


class BlacklistPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    # Ficheros o patrones glob; al cargar la config se resuelven respecto a la
    # config que los declara
    files: list[str] = Field(default_factory=list)
    # CSV donde añaden filas `blacklist add` y la UI de revisión
    user_file: str = ""
    categories: list[str] = Field(default_factory=lambda: list(CATEGORIES))
    # Si no está vacío, solo las filas de estas industrias (o sin industria)
    industries: list[str] = Field(default_factory=list)
    # Denominación por categoría, p. ej. {manufacturer: VENDOR-{n}}
    replacements: dict[str, str] = Field(default_factory=dict)
    # Términos que no se buscan (falsos positivos en un set concreto)
    exclude: list[str] = Field(default_factory=list)

    @field_validator("categories")
    @classmethod
    def _check_categories(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - set(CATEGORIES))
        if unknown:
            raise ValueError(tr("categorías desconocidas {unknown}; válidas: {valid}", "unknown categories {unknown}; valid: {valid}", unknown=unknown, valid=list(CATEGORIES)))
        return value

    @field_validator("replacements")
    @classmethod
    def _check_replacements(cls, value: dict[str, str]) -> dict[str, str]:
        unknown = sorted(set(value) - set(CATEGORIES))
        if unknown:
            raise ValueError(tr("replacements para categorías desconocidas {unknown}", "replacements for unknown categories {unknown}", unknown=unknown))
        return value

    @field_validator("industries")
    @classmethod
    def _check_industries(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - set(INDUSTRIES))
        if unknown:
            raise ValueError(tr("industrias desconocidas {unknown}; válidas: {valid}", "unknown industries {unknown}; valid: {valid}", unknown=unknown, valid=list(INDUSTRIES)))
        return value

    def paths(self) -> list[Path]:
        """Ficheros CSV que cubren `files`, sin repetir y en orden."""
        out: list[Path] = []
        for pattern in self.files:
            matches = sorted(glob.glob(pattern)) if glob.has_magic(pattern) else [pattern]
            for m in matches:
                p = Path(m)
                if p.is_file() and p not in out:
                    out.append(p)
        if self.user_file and Path(self.user_file).is_file() and Path(self.user_file) not in out:
            out.append(Path(self.user_file))
        return out


def resolve_paths(block: Any, base_dir: Path) -> Any:
    """Rutas de `files` y `user_file` relativas a la config que las declara."""
    if not isinstance(block, dict):
        return block
    out = dict(block)

    def resolve(value: Any) -> Any:
        return str((base_dir / value).resolve()) if isinstance(value, str) and value else value

    if isinstance(out.get("files"), list):
        out["files"] = [resolve(f) for f in out["files"]]
    if "user_file" in out:
        out["user_file"] = resolve(out["user_file"])
    return out


# --------------------------------------------------------------------------
# Entidades
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Entity:
    name: str
    aliases: tuple[str, ...]
    category: str
    segment: str = ""
    industries: tuple[str, ...] = ()
    country: str = ""
    active: bool = True
    notes: str = ""
    source: str = ""  # fichero:línea

    @property
    def terms(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


def entity_key(text: str) -> str:
    """Clave de comparación: sin espacios ni puntuación y con las confusiones
    de OCR unificadas ("Endress + Hauser" = "ENDRESS+HAUSER" = "ENDRE55HAUSER")."""
    return re.sub(r"[^0-9A-Z]", "", ocr_normalize(text))


def is_acronym(term: str) -> bool:
    """Siglas en mayúsculas (ABB, KBR, CB&I): se buscan distinguiendo mayúsculas."""
    return " " not in term and len(term) <= 5 and not any(c.islower() for c in term) and any(c.isalpha() for c in term)


def term_problem(term: str) -> str | None:
    """Motivo por el que un término no se admite en la lista negra, o None."""
    if len(term) < MIN_TERM_LENGTH:
        return tr("'{term}' es demasiado corto (mínimo {n} caracteres)", "'{term}' is too short (minimum {n} characters)", term=term, n=MIN_TERM_LENGTH)
    if not any(c.isalpha() for c in term):
        return tr("'{term}' no tiene letras", "'{term}' has no letters", term=term)
    plain = unicodedata.normalize("NFKD", term).encode("ascii", "ignore").decode().upper()
    words = [w for w in (re.sub(r"[^0-9A-Z]", "", w) for w in re.split(r"[\s\-/]+", plain)) if w]
    if words and all(w in PID_VOCABULARY for w in words):
        return tr(
            "'{term}' es vocabulario de P&ID: usa una forma más específica (p. ej. el nombre completo)",
            "'{term}' is P&ID vocabulary: use a more specific form (e.g. the full name)",
            term=term,
        )
    return None


def _norm_category(value: str) -> str:
    v = value.strip().lower()
    return _CATEGORY_SYNONYMS.get(v, v)


def _split(value: str | None) -> list[str]:
    return [" ".join(p.split()) for p in (value or "").split(ALIAS_SEP) if p.strip()]


def read_csv(path: Path) -> tuple[list[Entity], list[str]]:
    """Lee un CSV de la lista negra. Devuelve entidades y errores (con línea)."""
    text = path.read_text(encoding="utf-8-sig")
    first = text.split("\n", 1)[0]
    delimiter = ";" if first.count(";") > first.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    headers = [h.strip().lower() for h in (reader.fieldnames or [])]
    reader.fieldnames = headers
    errors: list[str] = []
    missing = [c for c in ("name", "category") if c not in headers]
    if missing:
        return [], [
            tr(
                "{file}: faltan columnas {missing} (cabecera: {header})",
                "{file}: missing columns {missing} (header: {header})",
                file=path.name, missing=missing, header=", ".join(COLUMNS),
            )
        ]
    entities: list[Entity] = []
    for row in reader:
        line = reader.line_num
        where = f"{path.name}:{line}"
        name = " ".join((row.get("name") or "").split())
        if not name or name.startswith("#"):
            continue
        category = _norm_category(row.get("category") or "")
        if category not in CATEGORIES:
            errors.append(
                tr(
                    "{where}: categoría '{cat}' desconocida (válidas: {valid})",
                    "{where}: unknown category '{cat}' (valid: {valid})",
                    where=where, cat=row.get("category"), valid=", ".join(CATEGORIES),
                )
            )
            continue
        industries = tuple(i.lower() for i in _split(row.get("industries")))
        bad = [i for i in industries if i not in INDUSTRIES]
        if bad:
            errors.append(
                tr(
                    "{where}: industrias desconocidas {bad} (válidas: {valid})",
                    "{where}: unknown industries {bad} (valid: {valid})",
                    where=where, bad=bad, valid=", ".join(INDUSTRIES),
                )
            )
            continue
        active_raw = (row.get("active") or "").strip().lower()
        if active_raw not in _TRUE | _FALSE:
            errors.append(
                tr(
                    "{where}: active debe ser sí o no, no '{value}'",
                    "{where}: active must be yes or no, not '{value}'",
                    where=where, value=row.get("active"),
                )
            )
            continue
        aliases = tuple(a for a in _split(row.get("aliases")) if a.casefold() != name.casefold())
        entity = Entity(
            name=name,
            aliases=aliases,
            category=category,
            segment=(row.get("segment") or "").strip(),
            industries=industries,
            country=(row.get("country") or "").strip(),
            active=active_raw not in _FALSE,
            notes=(row.get("notes") or "").strip(),
            source=where,
        )
        problems = [p for p in map(term_problem, entity.terms) if p]
        if problems:
            errors += [f"{where}: {p}" for p in problems]
            continue
        entities.append(entity)
    return entities, errors


def load_entities(paths: list[Path]) -> list[Entity]:
    """Consolida los CSV. Un término repetido entre entidades es un error."""
    entities: list[Entity] = []
    errors: list[str] = []
    for path in paths:
        try:
            found, errs = read_csv(path)
        except (OSError, UnicodeDecodeError, csv.Error) as exc:
            errors.append(tr("{path}: no se puede leer ({exc})", "{path}: cannot be read ({exc})", path=path, exc=exc))
            continue
        entities += found
        errors += errs
    owners: dict[str, Entity] = {}
    for entity in entities:
        seen_here: set[str] = set()
        for term in entity.terms:
            key = entity_key(term)
            if key in seen_here:
                continue
            seen_here.add(key)
            other = owners.get(key)
            if other is not None:
                errors.append(
                    tr(
                        "{src}: '{term}' ya figura en '{other}' ({other_src}); "
                        "cada término solo puede pertenecer a una entidad",
                        "{src}: '{term}' is already listed under '{other}' ({other_src}); "
                        "each term can only belong to one entity",
                        src=entity.source, term=term, other=other.name, other_src=other.source,
                    )
                )
            else:
                owners[key] = entity
    if errors:
        raise BlacklistError(errors)
    return entities


def selected(policy: BlacklistPolicy, entities: list[Entity]) -> list[Entity]:
    """Entidades activas de las categorías e industrias elegidas."""
    wanted = set(policy.industries)
    return [
        e
        for e in entities
        if e.active
        and e.category in policy.categories
        and (not wanted or not e.industries or "general" in e.industries or wanted & set(e.industries))
    ]


def build_rules(policy: BlacklistPolicy, overrides: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Sustituciones `kb-<categoría>` en forma de dict (las valida la config).

    `overrides`: entradas de la config con id `kb-…` (p. ej. excepciones
    añadidas desde la UI). Sus claves se respetan; sus términos se suman.
    """
    if not policy.enabled or not policy.files:
        return []
    overrides = overrides or {}
    entities = selected(policy, load_entities(policy.paths()))
    exclude = {entity_key(t) for t in policy.exclude}
    rules = []
    for category in policy.categories:
        rule_id = RULE_PREFIX + category
        extra = dict(overrides.get(rule_id, {}))
        excepted = {entity_key(t) for t in extra.get("exceptions", []) or []} | exclude
        terms: list[str] = []
        owners: dict[str, str] = {}
        members = [e for e in entities if e.category == category]
        seen: set[str] = set()

        def add(term: str, owner: str) -> None:
            key = entity_key(term)
            # Variantes con la misma clave ("R. STAHL", "R.STAHL") se buscan
            # todas: la clave une entidades, la regex busca el texto literal
            if key in excepted or term.casefold() in seen:
                return
            owners.setdefault(key, owner)
            seen.add(term.casefold())
            terms.append(term)

        for entity in members:
            for term in entity.terms:
                add(term, entity.name)
        for term in extra.pop("terms", []) or []:
            add(term, term)
        if not terms:
            continue
        sub_category, replacement = CATEGORIES[category]
        rule = {
            "id": rule_id,
            "category": sub_category,
            "description": tr(
                "Lista negra: {label} ({n} entidades)",
                "Blacklist: {label} ({n} entities)",
                label=category_label(category), n=len(members),
            ),
            "replacement": policy.replacements.get(category, replacement),
            "terms": terms,
            "entities": owners,
            "word_only": True,
        }
        rule.update(extra)
        rules.append(rule)
    return rules


# --------------------------------------------------------------------------
# Escritura (CLI y UI)
# --------------------------------------------------------------------------


def append_entity(path: Path, entity: Entity, known: list[Entity] | None = None) -> None:
    """Añade una fila al CSV (lo crea con cabecera si no existe).

    Comprueba los términos y que no estén ya en `known` (lo ya cargado).
    """
    problems = [p for p in map(term_problem, entity.terms) if p]
    if entity.category not in CATEGORIES:
        problems.append(
            tr(
                "categoría '{cat}' desconocida (válidas: {valid})",
                "unknown category '{cat}' (valid: {valid})",
                cat=entity.category, valid=", ".join(CATEGORIES),
            )
        )
    owners = {entity_key(t): e for e in (known or []) for t in e.terms}
    for term in entity.terms:
        if entity_key(term) in owners:
            other = owners[entity_key(term)]
            problems.append(
                tr("'{term}' ya figura en '{other}' ({src})", "'{term}' is already listed under '{other}' ({src})", term=term, other=other.name, src=other.source)
            )
    if problems:
        raise BlacklistError(problems)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    delimiter = ","
    if not new:
        first = path.read_text(encoding="utf-8-sig").split("\n", 1)[0]
        delimiter = ";" if first.count(";") > first.count(",") else ","
        needs_newline = not path.read_bytes().endswith(b"\n")
    with path.open("a", encoding="utf-8", newline="") as f:
        if not new and needs_newline:
            f.write("\n")
        writer = csv.writer(f, delimiter=delimiter, lineterminator="\n")
        if new:
            writer.writerow(COLUMNS)
        writer.writerow(
            [
                entity.name,
                ALIAS_SEP.join(entity.aliases),
                entity.category,
                entity.segment,
                ALIAS_SEP.join(entity.industries),
                entity.country,
                "sí" if entity.active else "no",
                entity.notes,
            ]
        )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _policy_from(config_path: Path) -> BlacklistPolicy:
    from sanitizer.rules import load_config  # rules importa este módulo

    return load_config(config_path).blacklist


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="blacklist",
        description=tr(
            "Lista negra de la industria (CSV): consultar, comprobar y añadir empresas.",
            "Industry blacklist (CSV): look up, check and add companies.",
        ),
    )
    parser.add_argument("--config", type=Path, default=Path("configs/base.yaml"), help=tr("config que declara la lista (por defecto configs/base.yaml)", "config that declares the list (default configs/base.yaml)"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stats", help=tr("resumen por categoría, industria y fichero", "summary by category, industry and file"))
    p_find = sub.add_parser("find", help=tr("busca entidades por nombre, alias o segmento", "search entities by name, alias or segment"))
    p_find.add_argument("text")
    p_check = sub.add_parser("check", help=tr("qué términos de la lista encajan en un texto", "which blacklist terms match a text"))
    p_check.add_argument("text", nargs="+")
    p_add = sub.add_parser("add", help=tr("añade una entidad al CSV del usuario (user_file)", "add an entity to the user CSV (user_file)"))
    p_add.add_argument("name")
    p_add.add_argument("--category", required=True, help=", ".join(CATEGORIES))
    p_add.add_argument("--aliases", default="", help=tr('separados por "|"', 'separated by "|"'))
    p_add.add_argument("--segment", default="")
    p_add.add_argument("--industries", default="", help=" | ".join(INDUSTRIES))
    p_add.add_argument("--country", default="")
    p_add.add_argument("--notes", default="")
    p_add.add_argument("--file", type=Path, help=tr("CSV de destino (por defecto user_file de la config)", "target CSV (default: the config's user_file)"))
    args = parser.parse_args(argv)

    try:
        policy = _policy_from(args.config)
        entities = load_entities(policy.paths())
    except BlacklistError as exc:
        print(tr("ERROR en la lista negra:", "ERROR in the blacklist:"), file=sys.stderr)
        for m in exc.messages:
            print(f"  - {m}", file=sys.stderr)
        return 1
    except Exception as exc:  # ConfigError y similares
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.cmd == "stats":
        files = policy.paths()
        print(
            tr(
                "Lista negra: {e} entidades, {t} términos",
                "Blacklist: {e} entities, {t} terms",
                e=len(entities), t=sum(len(e.terms) for e in entities),
            )
        )
        yes, no = tr("sí", "yes"), "no"
        print(
            tr("  activa: {on} · categorías: {cats}", "  enabled: {on} · categories: {cats}", on=yes if policy.enabled else no, cats=", ".join(policy.categories))
        )
        for f in files:
            n = sum(1 for e in entities if e.source.startswith(f.name + ":"))
            print(tr("  fichero: {f} ({n} entidades)", "  file: {f} ({n} entities)", f=f, n=n))
        for cat in CATEGORIES:
            n = sum(1 for e in entities if e.category == cat)
            print(f"  {cat:<13} {n:>5}  {category_label(cat)}")
        for ind in INDUSTRIES:
            n = sum(1 for e in entities if ind in e.industries)
            print(f"  · {ind:<11} {n:>5}")
        inactive = sum(1 for e in entities if not e.active)
        if inactive:
            print(tr("  desactivadas: {n}", "  disabled: {n}", n=inactive))
        return 0

    if args.cmd == "find":
        needle = args.text.casefold()
        hits = [e for e in entities if any(needle in t.casefold() for t in (*e.terms, e.segment))]
        for e in hits:
            alias = f" ({', '.join(e.aliases)})" if e.aliases else ""
            print(f"{e.category:<13} {e.name}{alias} · {e.segment} · {e.country} · {e.source}")
        if not hits:
            print(tr("(sin resultados)", "(no results)"))
        return 0

    if args.cmd == "check":
        from sanitizer.rules import load_config

        config = load_config(args.config)
        rules = [r for r in config.substitutions if r.word_only]
        text = " ".join(args.text)
        found = False
        for rule in rules:
            for rx in rule.matchers:
                for m in rx.finditer(text):
                    found = True
                    print(f"  '{m.group(0)}' → {rule.entity_of(m.group(0))} [{rule.id} → {rule.replacement}]")
        if not found:
            print(tr("  (ningún término de la lista negra encaja)", "  (no blacklist term matches)"))
        return 0

    # add
    target = args.file or (Path(policy.user_file) if policy.user_file else None)
    if target is None:
        print(tr("ERROR: la config no define blacklist.user_file; indica --file", "ERROR: the config does not define blacklist.user_file; pass --file"), file=sys.stderr)
        return 1
    entity = Entity(
        name=" ".join(args.name.split()),
        aliases=tuple(_split(args.aliases)),
        category=_norm_category(args.category),
        segment=args.segment,
        industries=tuple(i.lower() for i in _split(args.industries)),
        country=args.country,
        notes=args.notes,
    )
    bad = [i for i in entity.industries if i not in INDUSTRIES]
    if bad:
        print(tr("ERROR: industrias desconocidas {bad}", "ERROR: unknown industries {bad}", bad=bad), file=sys.stderr)
        return 1
    try:
        append_entity(target, entity, entities)
    except BlacklistError as exc:
        for m in exc.messages:
            print(f"ERROR: {m}", file=sys.stderr)
        return 1
    print(tr("Añadida '{name}' ({cat}) a {target}", "Added '{name}' ({cat}) to {target}", name=entity.name, cat=entity.category, target=target))
    if not any(Path(target).resolve() == p.resolve() for p in policy.paths()) and Path(target).resolve() != Path(policy.user_file or "").resolve():
        print(
            tr(
                "  ojo: ese fichero no está en blacklist.files de la config; no se cargará",
                "  note: that file is not in the config's blacklist.files; it will not be loaded",
            ),
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
