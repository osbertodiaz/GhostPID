"""Construye la app de escritorio de la UI de revisión con PyInstaller.

- macOS: `dist/GhostPID.app`. Trabaja con `data/` y `configs/` del
  proyecto, así que debe quedarse en `<proyecto>/dist/`.
- Windows: `dist/GhostPID/` (carpeta con el .exe). Con `--tesseract
  <carpeta>` se copia dentro tesseract con los idiomas eng, spa y osd, para
  una descarga que no necesita instalar nada más.
- Licencias: la app lleva `LICENSE` y `THIRD_PARTY_NOTICES.md`, y junto al
  ejecutable se escribe `licenses/THIRD_PARTY_LICENSES.txt` con el texto
  completo de las licencias de lo que PyInstaller incluyó (en macOS, en
  `dist/licenses/`, para no tocar el .app firmado). Si se incluye un paquete
  que THIRD_PARTY_NOTICES.md no menciona, el build falla.

Uso: `uv run python scripts/build_app.py [--tesseract "C:/Program Files/Tesseract-OCR"]`
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

import re

import PyInstaller.__main__

from sanitizer import licenses

ROOT = Path(__file__).resolve().parents[1]
NAME = "GhostPID"
LANGUAGES = ("eng", "spa", "osd")


def build() -> Path:
    sep = os.pathsep
    args = [
        "--noconfirm",
        "--clean",
        "--windowed",
        "--name",
        NAME,
        "--distpath",
        str(ROOT / "dist"),
        "--workpath",
        str(ROOT / "build" / "pyinstaller"),
        "--specpath",
        str(ROOT / "build"),
        "--collect-submodules",
        "sanitizer",
        "--collect-submodules",
        "ruamel.yaml",
        "--add-data",
        f"{ROOT / 'configs' / 'base.yaml'}{sep}configs",
        "--add-data",
        f"{ROOT / 'configs' / 'example.yaml'}{sep}configs",
        "--add-data",
        f"{ROOT / 'configs' / 'blacklist' / 'industria.csv'}{sep}configs/blacklist",
        "--add-data",
        f"{ROOT / licenses.LICENSE_FILE}{sep}{licenses.LICENSES_DIR}",
        "--add-data",
        f"{ROOT / licenses.NOTICES_FILE}{sep}{licenses.LICENSES_DIR}",
        "--exclude-module",
        "cv2",
        "--exclude-module",
        "pdfplumber",
        "--exclude-module",
        "tkinter",
    ]
    if sys.platform == "darwin":
        args += ["--osx-bundle-identifier", "local.ghostpid.review"]
    PyInstaller.__main__.run([*args, str(ROOT / "scripts" / "app_entry.py")])
    if sys.platform == "darwin":
        shutil.rmtree(ROOT / "dist" / NAME, ignore_errors=True)  # carpeta intermedia; la app es el .app
        return ROOT / "dist" / f"{NAME}.app"
    return ROOT / "dist" / NAME


def bundle_tesseract(app_dir: Path, source: Path) -> None:
    """Copia tesseract (binario, DLL y solo los idiomas necesarios) junto al ejecutable."""
    target = app_dir / "tesseract"
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("tessdata", "doc", "*.html", "unins*"))
    (target / "tessdata").mkdir()
    for lang in LANGUAGES:
        data = source / "tessdata" / f"{lang}.traineddata"
        if not data.exists():
            raise SystemExit(f"falta {data}")
        shutil.copyfile(data, target / "tessdata" / data.name)


def bundled_modules() -> set[str]:
    """Módulos de primer nivel que PyInstaller metió en la app (de sus TOC)."""
    work = ROOT / "build" / "pyinstaller" / NAME
    text = "".join(p.read_text(encoding="utf-8", errors="replace") for p in work.glob("*.toc"))
    return set(re.findall(r"\(\s*'([A-Za-z0-9_]+)[./'\\]", text))


def write_licenses(app: Path, tesseract: Path | None) -> Path:
    """Textos completos de las licencias de lo incluido, junto al ejecutable."""
    dists = licenses.bundled(licenses.runtime_distributions(), bundled_modules())
    notices = (ROOT / licenses.NOTICES_FILE).read_text(encoding="utf-8")
    missing = licenses.missing_from_notices(dists, notices)
    if missing:
        raise SystemExit(f"{licenses.NOTICES_FILE} no menciona: {', '.join(missing)}")
    folder = (app.parent if sys.platform == "darwin" else app) / licenses.LICENSES_DIR
    extra = []
    if tesseract is not None:
        found = [p for p in tesseract.rglob("*") if p.is_file() and re.match(r"(LICEN[CS]E|COPYING|NOTICE)", p.name, re.I)]
        extra.append(("Tesseract OCR", "Apache-2.0", found))
    target = licenses.write_license_texts(dists, folder / licenses.TEXTS_FILE, extra)
    for name in (licenses.LICENSE_FILE, licenses.NOTICES_FILE):
        shutil.copyfile(ROOT / name, folder / name)
    print(f"Licencias: {target} ({len(dists)} paquetes)")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tesseract", type=Path, help="carpeta de tesseract a incluir (Windows)")
    args = parser.parse_args()
    app = build()
    if args.tesseract is not None:
        bundle_tesseract(app, args.tesseract)
    write_licenses(app, args.tesseract)
    print(f"App lista: {app}")


if __name__ == "__main__":
    main()
