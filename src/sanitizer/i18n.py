"""Idioma de los textos al usuario: español si el sistema está en español, si no inglés.

Mecanismo: cada texto se escribe en los dos idiomas en el mismo sitio,
`tr("texto", "text")`, en vez de un catálogo gettext aparte. Con solo dos
idiomas así no hay ficheros `.po`/`.mo` que compilar ni empaquetar con
PyInstaller, ni claves que se desincronicen del código: es imposible añadir
un texto sin su traducción. Los marcadores de `str.format` (`{n}`) son los
mismos en ambos textos: `tr("{n} páginas", "{n} pages", n=3)`.

Detección (`current_language()`), por orden:

1. `GHOSTPID_LANG=es|en` (tests y soporte).
2. Idioma de la interfaz del sistema: en macOS `AppleLanguages` de las
   preferencias globales (lo mismo que usa `QLocale.system()`), en Windows
   `GetUserDefaultUILanguage`.
3. Variables `LANGUAGE`, `LC_ALL`, `LC_MESSAGES` y `LANG` (Linux, o si lo
   anterior falla) y, por último, `locale.getlocale()`.

Cualquier `es`, `es_*` o `es-*` da español; todo lo demás, inglés.
"""

from __future__ import annotations

import locale
import os
import plistlib
import sys
from functools import lru_cache
from pathlib import Path

ENV_VAR = "GHOSTPID_LANG"
# Nombre anterior del proyecto (PID_Anonymouse); se sigue aceptando.
LEGACY_ENV_VAR = "PID_ANONYMOUSE_LANG"
LANGUAGES = ("es", "en")


def _normalize(tag: str | None) -> str | None:
    """'es_ES.UTF-8', 'es-419', 'es' → 'es'; cualquier otro idioma → 'en'; vacío/C → None."""
    if not tag:
        return None
    code = tag.strip().split(":")[0].split(".")[0].replace("-", "_").split("_")[0].lower()
    if not code or code in ("c", "posix"):
        return None
    return "es" if code == "es" else "en"


def _macos_language() -> str | None:
    plist = Path.home() / "Library" / "Preferences" / ".GlobalPreferences.plist"
    try:
        with plist.open("rb") as fh:
            languages = plistlib.load(fh).get("AppleLanguages") or []
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    return _normalize(languages[0]) if languages else None


def _windows_language() -> str | None:
    try:
        import ctypes

        langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return None
    # Idioma primario: los 10 bits bajos del LANGID; 0x0A es español
    return "es" if (langid & 0x3FF) == 0x0A else "en"


def _env_language() -> str | None:
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        found = _normalize(os.environ.get(var))
        if found:
            return found
    return None


@lru_cache(maxsize=1)
def system_language() -> str:
    """Idioma del sistema operativo ('es' o 'en'), sin tener en cuenta la variable de entorno."""
    detectors = []
    if sys.platform == "darwin":
        detectors.append(_macos_language)
    elif sys.platform == "win32":
        detectors.append(_windows_language)
    detectors.append(_env_language)
    for detect in detectors:
        found = detect()
        if found:
            return found
    try:
        return _normalize(locale.getlocale()[0]) or "en"
    except ValueError:
        return "en"


def current_language() -> str:
    """'es' o 'en': la variable `GHOSTPID_LANG` manda; si no, el idioma del sistema."""
    forced = (os.environ.get(ENV_VAR) or os.environ.get(LEGACY_ENV_VAR, "")).strip().lower()
    if forced in LANGUAGES:
        return forced
    return system_language()


def is_spanish() -> bool:
    return current_language() == "es"


def tr(es: str, en: str, /, **values: object) -> str:
    """El texto en el idioma actual, formateado con `values` si se pasan."""
    text = es if is_spanish() else en
    return text.format(**values) if values else text
