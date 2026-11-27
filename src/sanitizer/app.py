"""Fase 8 · Lanzador de la UI de revisión como aplicación de escritorio.

Una app abierta desde el Finder o el Explorador no recibe argumentos ni
hereda el entorno de la terminal. Este lanzador:

- localiza la carpeta de trabajo y trabaja desde ella (`data/` y `configs/`):
  - macOS, app construida en `<proyecto>/dist/`: la carpeta del proyecto;
  - versión descargable (p. ej. el zip de Windows): la carpeta de la app,
    donde crea `data/` y copia `configs/base.yaml` y `example.yaml` si faltan;
- encuentra `tesseract`: el incluido en la descarga, el de Homebrew o el de
  la instalación habitual de Windows;
- pide el PDF y la config del cliente (o crea una a partir del ejemplo) y
  abre la ventana de revisión;
- muestra en un diálogo los errores que en la terminal irían a stderr.

`PID_ANONYMOUSE_SELFTEST=1` comprueba el paquete sin abrir ventanas.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QWidget,
)

APP_NAME = "PID Anonymouse"
SHARED_CONFIGS = ("base.yaml", "example.yaml")


# --------------------------------------------------------------------------
# Entorno
# --------------------------------------------------------------------------


def _bundle_dir() -> Path:
    """Recursos incluidos en el paquete (o la raíz del repo, sin empaquetar)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[2]


def project_root() -> Path:
    """Carpeta de trabajo, con `data/` y `configs/`."""
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parents[2]
    exe = Path(sys.executable).resolve()
    app = next((p for p in exe.parents if p.suffix == ".app"), None)
    if app is not None:
        folder = app.parent
        # App construida dentro del proyecto: <proyecto>/dist/<app>.app
        if folder.name == "dist" and (folder.parent / "configs").is_dir():
            return folder.parent
        return folder
    return exe.parent


def _ensure_workspace(root: Path) -> None:
    (root / "data").mkdir(exist_ok=True)
    configs = root / "configs"
    configs.mkdir(exist_ok=True)
    for name in SHARED_CONFIGS:
        bundled = _bundle_dir() / "configs" / name
        if not (configs / name).exists() and bundled.exists():
            shutil.copyfile(bundled, configs / name)


def _find_tesseract() -> Path | None:
    exe = "tesseract.exe" if sys.platform == "win32" else "tesseract"
    candidates = [
        _bundle_dir() / "tesseract" / exe,
        Path(sys.executable).parent / "tesseract" / exe,
        Path("/opt/homebrew/bin") / exe,
        Path("/usr/local/bin") / exe,
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tesseract-OCR" / exe,
    ]
    found = next((c for c in candidates if c.is_file()), None)
    if found is None and shutil.which(exe):
        found = Path(shutil.which(exe))
    return found


def prepare_environment(root: Path) -> None:
    if getattr(sys, "frozen", False):
        _ensure_workspace(root)
    os.chdir(root)
    # PyInstaller apunta las rutas de bibliotecas a las del paquete; tesseract
    # (proceso hijo) debe usar las suyas
    for key in ("DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH", "LD_LIBRARY_PATH"):
        original = os.environ.pop(f"{key}_ORIG", None)
        if original is not None:
            os.environ[key] = original
        elif getattr(sys, "frozen", False):
            os.environ.pop(key, None)
    # macOS sin TMPDIR: Python usaría /tmp, que la leptonica de Homebrew
    # reescribe al directorio temporal del usuario (tesseract no vería el fichero)
    if sys.platform == "darwin" and not os.environ.get("TMPDIR"):
        try:
            tmp = subprocess.run(["getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True, check=True).stdout.strip()
            if tmp:
                os.environ["TMPDIR"] = tmp
        except (OSError, subprocess.CalledProcessError):
            pass
    tesseract = _find_tesseract()
    if tesseract is not None:
        import pytesseract

        pytesseract.pytesseract.tesseract_cmd = str(tesseract)
        os.environ["PATH"] = os.pathsep.join([str(tesseract.parent), os.environ.get("PATH", "")])
        tessdata = tesseract.parent / "tessdata"
        if tessdata.is_dir() and tesseract.parent.name == "tesseract":  # el incluido en la descarga
            os.environ["TESSDATA_PREFIX"] = str(tessdata)


def client_configs(root: Path) -> list[Path]:
    """Configs de cliente (las editables), la más reciente primero."""
    folder = root / "configs"
    found = [p for p in folder.glob("*.yaml") if p.name not in SHARED_CONFIGS] if folder.is_dir() else []
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def new_config(root: Path, name: str) -> Path:
    """Config de cliente nueva a partir del ejemplo (hereda de base.yaml)."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower()).strip("-")
    if not slug or slug in ("base", "example"):
        raise ValueError("nombre de cliente no válido")
    target = root / "configs" / f"{slug}.yaml"
    if target.exists():
        raise ValueError(f"ya existe {target.name}")
    example = root / "configs" / "example.yaml"
    text = example.read_text(encoding="utf-8")
    text = re.sub(r"^name:.*$", f"name: {name.strip()}", text, count=1, flags=re.MULTILINE)
    target.write_text(text, encoding="utf-8")
    return target


# --------------------------------------------------------------------------
# Diálogo inicial
# --------------------------------------------------------------------------


class StartDialog(QDialog):
    """Elegir el PDF, la config del cliente y las pasadas de autocorrección."""

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        self.setWindowTitle(f"{APP_NAME} · abrir documento")
        self.setMinimumWidth(680)
        form = QFormLayout(self)
        form.addRow(QLabel(f"Carpeta de trabajo: {root}"))
        tesseract = _find_tesseract()
        form.addRow(QLabel(f"OCR (tesseract): {tesseract or 'no encontrado: el OCR no funcionará'}"))

        self.pdf = QLineEdit()
        pick_pdf = QPushButton("Elegir…")
        pick_pdf.clicked.connect(self._pick_pdf)
        form.addRow("PDF original:", self._row(self.pdf, pick_pdf))

        self.config = QComboBox()
        self.config.setEditable(True)
        self._load_configs()
        pick_cfg = QPushButton("Elegir…")
        pick_cfg.clicked.connect(self._pick_config)
        new_cfg = QPushButton("Nueva…")
        new_cfg.setToolTip("Crea configs/<cliente>.yaml a partir del ejemplo")
        new_cfg.clicked.connect(self._new_config)
        form.addRow("Config del cliente:", self._row(self.config, pick_cfg, new_cfg))

        self.repair = QSpinBox()
        self.repair.setRange(0, 5)
        self.repair.setValue(1)
        form.addRow("Pasadas de autocorrección:", self.repair)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Open | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _load_configs(self, select: Path | None = None) -> None:
        self.config.clear()
        for path in client_configs(self.root):
            self.config.addItem(str(path.relative_to(self.root)), str(path))
        if select is not None:
            self.config.setCurrentIndex(max(0, self.config.findData(str(select))))

    @staticmethod
    def _row(*widgets: QWidget) -> QWidget:
        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        for w in widgets:
            layout.addWidget(w)
        layout.setStretch(0, 1)
        return box

    def _pick_pdf(self) -> None:
        start = self.root / "data"
        path, _ = QFileDialog.getOpenFileName(self, "PDF original", str(start if start.is_dir() else self.root), "PDF (*.pdf)")
        if path:
            self.pdf.setText(path)

    def _pick_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Config del cliente", str(self.root / "configs"), "YAML (*.yaml *.yml)")
        if path:
            self.config.setEditText(path)

    def _new_config(self) -> None:
        name, ok = QInputDialog.getText(self, "Nueva config", "Nombre del cliente:")
        if not ok or not name.strip():
            return
        try:
            path = new_config(self.root, name)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        self._load_configs(path)
        QMessageBox.information(
            self,
            APP_NAME,
            f"Creada {path.relative_to(self.root)} a partir del ejemplo.\n\n"
            "Contiene nombres ficticios: cámbialos por los de tu cliente desde la UI "
            "(Añadir término) o editando el YAML.",
        )

    def config_path(self) -> Path:
        data = self.config.currentData()
        text = self.config.currentText().strip()
        if data and text == self.config.itemText(self.config.currentIndex()):
            return Path(data)
        path = Path(text)
        return path if path.is_absolute() else self.root / path

    def _accept(self) -> None:
        pdf, cfg = Path(self.pdf.text().strip()), self.config_path()
        if not pdf.is_file():
            QMessageBox.warning(self, APP_NAME, "Elige un PDF que exista.")
            return
        if not cfg.is_file():
            QMessageBox.warning(self, APP_NAME, "Elige la config YAML del cliente (o crea una con «Nueva…»).")
            return
        if cfg.name in SHARED_CONFIGS:
            QMessageBox.warning(self, APP_NAME, "La UI edita la config: usa la de un cliente, no base ni example.")
            return
        self.accept()


# --------------------------------------------------------------------------
# Arranque
# --------------------------------------------------------------------------


def _excepthook(kind: type, value: BaseException, tb: object) -> None:
    text = "".join(traceback.format_exception(kind, value, tb))
    if sys.__stderr__ is not None:
        sys.__stderr__.write(text)
    QMessageBox.critical(None, APP_NAME, f"Error inesperado:\n\n{value}\n\n{text[-2000:]}")


def self_test() -> int:
    """Comprobación del paquete sin ventanas: módulos, tesseract y OCR."""
    import tempfile

    import pymupdf
    import pytesseract

    from sanitizer import audit, cli, config_edit, ocr, pipeline, review, ui  # noqa: F401

    # La app de ventanas no tiene consola (Windows): el informe va a un fichero
    out = sys.stdout or open(project_root() / "selftest.log", "w", encoding="utf-8")

    def print(*items: object) -> None:  # noqa: A001
        out.write(" ".join(map(str, items)) + "\n")
        out.flush()

    print("raíz:", project_root())
    print("configs:", sorted(p.name for p in (project_root() / "configs").glob("*.yaml")))
    print("tesseract:", pytesseract.pytesseract.tesseract_cmd, pytesseract.get_tesseract_version())
    print("idiomas:", pytesseract.get_languages())
    with tempfile.TemporaryDirectory() as tmp:
        doc = pymupdf.open()
        doc.new_page().insert_text((72, 72), "SELFTEST P-101A", fontsize=14)
        lines = ocr.ocr_page(doc[0], ocr.OcrPolicy(rotations=[0]))
        text = " ".join(line.text for line in lines)
        print("ocr:", text)
        doc.save(Path(tmp) / "x.pdf")
    if "P-101A" not in text:
        print("FALLO: el OCR no leyó el texto de prueba")
        return 1
    print("ok")
    return 0


def main() -> int:
    root = project_root()
    prepare_environment(root)
    if os.environ.get("PID_ANONYMOUSE_SELFTEST"):
        return self_test()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    sys.excepthook = _excepthook

    from sanitizer.rules import ConfigError  # import diferido: arranque más rápido
    from sanitizer.ui import ReviewWindow

    dialog = StartDialog(root)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return 0
    try:
        window = ReviewWindow(Path(dialog.pdf.text().strip()), dialog.config_path(), repair_passes=dialog.repair.value())
    except ConfigError as exc:
        QMessageBox.critical(None, APP_NAME, "La config no es válida:\n\n" + "\n".join(exc.messages))
        return 1
    window.show()
    window.start()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
