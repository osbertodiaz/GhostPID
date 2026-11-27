"""Licencias: la propia (MIT) y las de los componentes de terceros.

- `LICENSE` y `THIRD_PARTY_NOTICES.md` están en la raíz del repo y la app
  empaquetada los lleva en `licenses/` (ver scripts/build_app.py).
- Al construir la app, `write_license_texts` reúne en
  `THIRD_PARTY_LICENSES.txt` los textos completos de las licencias de las
  distribuciones que PyInstaller incluyó de verdad, leídos de sus metadatos.
- `about_text` es lo que muestra el diálogo «Acerca de y licencias…».
"""

from __future__ import annotations

import re
import sys
from importlib import metadata
from pathlib import Path

from sanitizer import __version__
from sanitizer.i18n import tr

APP_NAME = "GhostPID"
PROJECT_URL = "https://github.com/osbertodiaz/GhostPID"
LICENSE_FILE = "LICENSE"
NOTICES_FILE = "THIRD_PARTY_NOTICES.md"
TEXTS_FILE = "THIRD_PARTY_LICENSES.txt"
LICENSES_DIR = "licenses"
_LICENSE_NAME = re.compile(r"(^|/)(LICEN[CS]E|COPYING|NOTICE|AUTHORS)[^/]*$", re.IGNORECASE)


def normalize(name: str) -> str:
    """Nombre de distribución normalizado (PEP 503): `PyQt6_sip` → `pyqt6-sip`."""
    return re.sub(r"[-_.]+", "-", name).lower()


# --------------------------------------------------------------------------
# Dónde están los ficheros
# --------------------------------------------------------------------------


def search_dirs() -> list[Path]:
    """Carpetas donde buscar los ficheros de licencia, por orden."""
    dirs: list[Path] = []
    if getattr(sys, "frozen", False):
        bundle = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        dirs += [bundle / LICENSES_DIR, Path(sys.executable).parent / LICENSES_DIR]
        dirs += [p / LICENSES_DIR for p in Path(sys.executable).parents[:4]]
    dirs.append(Path(__file__).resolve().parents[2])  # raíz del repo
    return dirs


def find_file(name: str) -> Path | None:
    for folder in search_dirs():
        path = folder / name
        if path.is_file():
            return path
    return None


def read_file(name: str) -> str | None:
    path = find_file(name)
    return path.read_text(encoding="utf-8", errors="replace") if path else None


# --------------------------------------------------------------------------
# Distribuciones incluidas en la app
# --------------------------------------------------------------------------


def runtime_distributions(root: str = "ghostpid") -> list[metadata.Distribution]:
    """Las dependencias de ejecución de `root`, recursivamente (sin extras ni dev)."""
    seen: dict[str, metadata.Distribution] = {}
    pending = [root]
    while pending:
        name = normalize(pending.pop())
        if name in seen:
            continue
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        seen[name] = dist
        for req in dist.requires or []:
            if "extra ==" in req:
                continue
            match = re.match(r"[A-Za-z0-9_.\-]+", req)
            if match:
                pending.append(match.group(0))
    seen.pop(normalize(root), None)
    return sorted(seen.values(), key=lambda d: normalize(d.metadata["Name"]))


def top_level_modules(dist: metadata.Distribution) -> set[str]:
    """Los módulos de primer nivel que instala una distribución."""
    names: set[str] = set()
    for file in dist.files or []:
        first = file.parts[0]
        if first.endswith((".dist-info", ".data")) or first in ("..", "__pycache__"):
            continue
        names.add(first.split(".")[0] if len(file.parts) == 1 else first)
    return names


def bundled(dists: list[metadata.Distribution], modules: set[str]) -> list[metadata.Distribution]:
    """Las distribuciones con algún módulo entre `modules` (lo que PyInstaller incluyó)."""
    return [d for d in dists if top_level_modules(d) & modules]


def license_files(dist: metadata.Distribution) -> list[Path]:
    """Ficheros de licencia del paquete: LICENSE/COPYING/NOTICE y la carpeta `licenses/` de su dist-info."""
    found = []
    for f in dist.files or []:
        posix = f.as_posix()
        in_dist_info = ".dist-info/licenses/" in posix and not posix.endswith((".md", ".html"))
        if _LICENSE_NAME.search(posix) or in_dist_info:
            path = Path(str(dist.locate_file(f)))
            if path.is_file() and path not in found:
                found.append(path)
    return found


def missing_from_notices(dists: list[metadata.Distribution], notices: str) -> list[str]:
    """Distribuciones que THIRD_PARTY_NOTICES.md no menciona como `(\\`nombre\\`)`."""
    listed = {normalize(n) for n in re.findall(r"`([A-Za-z0-9_.\-]+)`", notices)}
    return [d.metadata["Name"] for d in dists if normalize(d.metadata["Name"]) not in listed]


def write_license_texts(
    dists: list[metadata.Distribution], target: Path, extra: list[tuple[str, str, list[Path]]] | None = None
) -> Path:
    """Escribe en `target` un fichero con el texto completo de cada licencia."""
    sections: list[tuple[str, str, list[Path]]] = []
    python_license = Path(sys.base_prefix) / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "LICENSE.txt"
    if not python_license.is_file():
        python_license = Path(sys.base_prefix) / "LICENSE.txt"
    sections.append((f"Python {sys.version.split()[0]}", "PSF-2.0", [python_license] if python_license.is_file() else []))
    for dist in dists:
        meta = dist.metadata
        lic = meta.get("License-Expression") or (meta.get("License") or "").splitlines()[0:1]
        lic = lic if isinstance(lic, str) else (lic[0] if lic else "")
        if len(lic) > 80:  # algunos paquetes ponen aquí el texto entero
            lic = lic[:77] + "…"
        sections.append((f"{meta['Name']} {meta['Version']}", lic, license_files(dist)))
    sections += extra or []

    out = [f"{APP_NAME} {__version__} · third-party licenses", f"Source code: {PROJECT_URL}", ""]
    for title, lic, files in sections:
        out += ["=" * 78, f"{title}" + (f" · {lic}" if lic else ""), "=" * 78, ""]
        if not files:
            out += ["(no license file in the package; see THIRD_PARTY_NOTICES.md and the project page)", ""]
        for path in files:
            out += [f"--- {path.name} ---", path.read_text(encoding="utf-8", errors="replace").rstrip(), ""]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(out) + "\n", encoding="utf-8")
    return target


# --------------------------------------------------------------------------
# Texto del diálogo
# --------------------------------------------------------------------------


def about_text() -> str:
    """Resumen corto para el diálogo «Acerca de»."""
    return tr(
        "{app} {v}\n\n"
        "Anonimización local y determinista de P&ID y PFD en PDF.\n"
        "Código fuente: {url}\n\n"
        "El código de {app} se publica con la licencia MIT.\n\n"
        "La app de escritorio incluye componentes de terceros con sus propias "
        "licencias. Dos son copyleft: PyMuPDF/MuPDF (AGPL-3.0) y PyQt6 (GPL-3.0), "
        "así que la app empaquetada se distribuye bajo los términos de la GNU AGPL-3.0. "
        "Tienes derecho a recibir el código fuente completo en la dirección de arriba.\n\n"
        "Se ofrece TAL CUAL, sin garantía de ningún tipo. Revisa siempre el resultado "
        "antes de compartir un documento.",
        "{app} {v}\n\n"
        "Local, deterministic anonymization of P&IDs and PFDs in PDF.\n"
        "Source code: {url}\n\n"
        "{app}'s code is released under the MIT License.\n\n"
        "The desktop app bundles third-party components under their own licenses. "
        "Two are copyleft: PyMuPDF/MuPDF (AGPL-3.0) and PyQt6 (GPL-3.0), so the "
        "bundled app is distributed under the terms of the GNU AGPL-3.0. You are "
        "entitled to the complete source code at the address above.\n\n"
        "It is provided AS IS, without warranty of any kind. Always check the result "
        "before sharing a document.",
        app=APP_NAME,
        v=__version__,
        url=PROJECT_URL,
    )
