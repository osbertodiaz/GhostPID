"""Fase 8 · Cambios en la config del cliente desde la UI de revisión.

La UI añade términos y excepciones a las reglas, zonas y decisiones sobre
logos. Se edita el YAML del cliente con ruamel.yaml para conservar
comentarios y orden, y antes de cada cambio se guarda una copia `.bak`.

Con `extends`, una regla repetida en el hijo sobrescribe solo las claves que
indica, y las listas se sustituyen enteras. Por eso, al ampliar una lista de
una regla heredada, se escribe la lista completa (la heredada más lo nuevo).

Los términos de las reglas de la lista negra (`kb-…`) no van al YAML: se
añaden como fila nueva al CSV del usuario (`blacklist.user_file`). Sus
excepciones sí van al YAML, en una entrada con el mismo id.

Tras escribir se vuelve a cargar y validar la config. Si queda inválida o con
errores de solape, se restaura la copia y se lanza `EditError`: la config
nunca queda rota.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from sanitizer.blacklist import RULE_PREFIX, BlacklistError, Entity, append_entity, load_entities
from sanitizer.rules import Config, ConfigError, check_overlaps, load_config


class EditError(ValueError):
    """Cambio rechazado: la config resultante no sería válida."""


def _yaml() -> YAML:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 4096
    return yaml


def _flow(items: list[Any]) -> CommentedSeq:
    seq = CommentedSeq(items)
    seq.fa.set_flow_style()
    return seq


class ConfigEditor:
    """Editor de la config de un cliente (el fichero que se pasa con --config)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.config: Config = load_config(self.path)

    # ------------------------------------------------------------------
    # Lectura y escritura
    # ------------------------------------------------------------------

    def _read(self) -> CommentedMap:
        data = _yaml().load(self.path.read_text(encoding="utf-8"))
        return data if data is not None else CommentedMap()

    def _write(self, data: CommentedMap) -> Config:
        backup = self.path.with_name(self.path.name + ".bak")
        shutil.copy2(self.path, backup)
        with self.path.open("w", encoding="utf-8") as fh:
            _yaml().dump(data, fh)
        try:
            config = load_config(self.path)
        except ConfigError as exc:
            shutil.copy2(backup, self.path)
            raise EditError("; ".join(exc.messages)) from None
        errors = [i.message for i in check_overlaps(config) if i.severity == "error"]
        if errors:
            shutil.copy2(backup, self.path)
            raise EditError("; ".join(errors))
        self.config = config
        return config

    def _rule_entry(self, data: CommentedMap, block: str, rule_id: str) -> CommentedMap:
        """Entrada de la regla en este fichero (se crea si solo está heredada)."""
        rules = data.get(block)
        if rules is None:
            rules = data[block] = CommentedSeq()
        for entry in rules:
            if isinstance(entry, dict) and entry.get("id") == rule_id:
                return entry
        entry = CommentedMap(id=rule_id)
        rules.append(entry)
        return entry

    def _substitution(self, rule_id: str) -> Any:
        rule = next((r for r in self.config.substitutions if r.id == rule_id), None)
        if rule is None:
            raise EditError(f"no hay ninguna sustitución con id '{rule_id}'")
        return rule

    def _extend_list(self, block: str, rule_id: str, key: str, current: list[str], new: str) -> Config:
        new = " ".join(new.split())
        if not new:
            raise EditError("texto vacío")
        if any(" ".join(x.split()).casefold() == new.casefold() for x in current):
            return self.config  # ya estaba
        data = self._read()
        entry = self._rule_entry(data, block, rule_id)
        entry[key] = _flow([*current, new])
        return self._write(data)

    # ------------------------------------------------------------------
    # Operaciones
    # ------------------------------------------------------------------

    def add_term(self, rule_id: str, term: str) -> Config:
        """Añade un término literal a una sustitución existente."""
        rule = self._substitution(rule_id)
        if rule.word_only and rule_id.startswith(RULE_PREFIX):
            return self.add_to_blacklist(rule_id.removeprefix(RULE_PREFIX), term)
        return self._extend_list("substitutions", rule_id, "terms", list(rule.terms), term)

    def add_to_blacklist(self, category: str, name: str, aliases: tuple[str, ...] = ()) -> Config:
        """Añade una entidad al CSV del usuario de la lista negra."""
        policy = self.config.blacklist
        if not policy.user_file:
            raise EditError("la config no define blacklist.user_file (CSV donde añadir)")
        target = Path(policy.user_file)
        name = " ".join(name.split())
        before = target.read_bytes() if target.exists() else None
        try:
            known = load_entities(policy.paths())
            append_entity(target, Entity(name, aliases, category, notes="añadido desde la revisión"), known)
            config = load_config(self.path)
        except (BlacklistError, ConfigError) as exc:
            if before is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(before)
            raise EditError("; ".join(exc.messages)) from None
        self.config = config
        return config

    def add_exception(self, rule_id: str, text: str) -> Config:
        """Declara `text` excepción de una sustitución (falso positivo)."""
        rule = self._substitution(rule_id)
        return self._extend_list("substitutions", rule_id, "exceptions", list(rule.exceptions), text)

    def add_rule(self, rule_id: str, category: str, replacement: str, terms: list[str]) -> Config:
        """Crea una sustitución nueva con términos literales."""
        if any(r.id == rule_id for r in self.config.substitutions):
            raise EditError(f"ya existe una sustitución con id '{rule_id}'")
        data = self._read()
        entry = self._rule_entry(data, "substitutions", rule_id)
        entry["category"] = category
        entry["replacement"] = replacement
        entry["terms"] = _flow([" ".join(t.split()) for t in terms])
        return self._write(data)

    def add_zone(
        self,
        rect: tuple[float, float, float, float],
        pages: str | list[int | str] = "all",
        label: str = "",
        description: str = "",
        zone_id: str | None = None,
    ) -> tuple[str, Config]:
        """Añade una zona en fracción de página (página tal como se ve)."""
        taken = {z.id for z in self.config.zones}
        if zone_id is None:
            n = 1
            while f"review_zone_{n}" in taken:
                n += 1
            zone_id = f"review_zone_{n}"
        elif zone_id in taken:
            raise EditError(f"ya existe una zona con id '{zone_id}'")
        data = self._read()
        entry = self._rule_entry(data, "zones", zone_id)
        if description:
            entry["description"] = description
        entry["rect"] = _flow([round(max(0.0, min(1.0, v)), 4) for v in rect])
        if pages != "all":
            entry["pages"] = _flow(list(pages))
        if label:
            entry["label"] = label
        return zone_id, self._write(data)

    def set_logo(self, image_hash: str, remove: bool) -> Config:
        """Fija la decisión sobre un grupo de imágenes: eliminar siempre o conservar."""
        if not re.fullmatch(r"[0-9a-f]{16}", image_hash):
            raise EditError(f"hash de imagen inválido: {image_hash!r}")
        logos = self.config.logos
        target, other = ("remove", "keep") if remove else ("keep", "remove")
        data = self._read()
        block = data.get("logos")
        if block is None:
            block = data["logos"] = CommentedMap()
        current = [h for h in getattr(logos, target) if h != image_hash]
        block[target] = _flow([*current, image_hash])
        rest = [h for h in getattr(logos, other) if h != image_hash]
        if other in block or len(rest) != len(getattr(logos, other)):
            block[other] = _flow(rest)
        return self._write(data)

    def disable(self, rule_id: str) -> Config:
        """Desactiva una regla o zona (también si es heredada) con `disable`."""
        known = {r.id for r in (*self.config.whitelist, *self.config.substitutions, *self.config.zones)}
        if rule_id not in known:
            raise EditError(f"no hay ninguna regla ni zona con id '{rule_id}'")
        data = self._read()
        current = list(data.get("disable") or [])
        if rule_id not in current:
            data["disable"] = _flow([*current, rule_id])
        return self._write(data)
