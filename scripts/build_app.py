"""Construye la app de escritorio de la UI de revisión con PyInstaller.

- macOS: `dist/PID Anonymouse.app`. Trabaja con `data/` y `configs/` del
  proyecto, así que debe quedarse en `<proyecto>/dist/`.
- Windows: `dist/PID Anonymouse/` (carpeta con el .exe). Con `--tesseract
  <carpeta>` se copia dentro tesseract con los idiomas eng, spa y osd, para
  una descarga que no necesita instalar nada más.

Uso: `uv run python scripts/build_app.py [--tesseract "C:/Program Files/Tesseract-OCR"]`
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parents[1]
NAME = "PID Anonymouse"
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
        "--exclude-module",
        "cv2",
        "--exclude-module",
        "pdfplumber",
        "--exclude-module",
        "tkinter",
    ]
    if sys.platform == "darwin":
        args += ["--osx-bundle-identifier", "local.pid-anonymouse.review"]
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tesseract", type=Path, help="carpeta de tesseract a incluir (Windows)")
    args = parser.parse_args()
    app = build()
    if args.tesseract is not None:
        bundle_tesseract(app, args.tesseract)
    print(f"App lista: {app}")


if __name__ == "__main__":
    main()
