"""Fase 8 · UI de revisión (PyQt6): `review <pdf> --config configs/<cliente>.yaml`.

Flujo:

1. Al abrir, carga los informes de un `sanitize` previo si siguen vigentes; si
   no, hace una vista previa rápida (sin OCR). "Con OCR" la repite con OCR.
2. La página original se ve con las detecciones marcadas por colores; la
   lista de la derecha se filtra por tipo y por página.
3. Desde la lista: descartar una detección (excepción en la regla, logo a
   conservar o zona desactivada) y añadir términos a reglas (o crear una).
   Con "Dibujar zona" se arrastra un rectángulo que pasa a la config.
4. Cada cambio se guarda en la config del cliente (con copia `.bak`) y deja
   las detecciones desactualizadas hasta "Recalcular".
5. "Generar" ejecuta el proceso completo (`sanitize`) y muestra la auditoría.

La UI muestra valores originales: todo lo que escribe va a data/ o a la
config del cliente (que no se versiona).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Callable

import pymupdf
from PyQt6.QtCore import QLibraryInfo, QObject, QPointF, QRectF, QRunnable, Qt, QThreadPool, QTranslator, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QBrush, QColor, QDesktopServices, QImage, QPen, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from sanitizer.config_edit import ConfigEditor, EditError
from sanitizer.mapping import inside_data
from sanitizer.review import (
    KINDS,
    Detection,
    ReviewState,
    discard,
    discard_action,
    generate,
    kind_description,
    kind_name,
    load_existing,
    locate,
    origin_name,
    preview,
    zone_fraction,
)
from sanitizer.rules import Category
from sanitizer.i18n import current_language, tr

COLORS: dict[str, str] = {
    "sustitución": "#2e9d4f",
    "zona": "#2f6fd6",
    "logo": "#8a3ad6",
    "resto": "#e02424",
    "aviso": "#f08c00",
    "rozada": "#c9a400",
    "bloqueada": "#808080",
    "revisar": "#0aa3b8",
    "seleccion": "#ff00aa",
}
MAX_RENDER_SIDE = 4000  # px del lado mayor al renderizar una página


def install_translations(app: QApplication) -> None:
    """Botones y diálogos estándar de Qt (Aceptar, Cancelar, Sí…) en el idioma de la app."""
    if getattr(app, "_pid_translator", None) is not None:
        return
    translator = QTranslator(app)
    folder = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    if translator.load(f"qtbase_{current_language()}", folder):
        app.installTranslator(translator)
        app._pid_translator = translator  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# Tareas en segundo plano
# --------------------------------------------------------------------------


class _Signals(QObject):
    done = pyqtSignal(object)
    failed = pyqtSignal(str)
    progress = pyqtSignal(str)


class _Job(QRunnable):
    def __init__(self, fn: Callable[[Callable[[str], None]], Any]) -> None:
        super().__init__()
        self.fn = fn
        self.signals = _Signals()

    def run(self) -> None:
        try:
            result = self.fn(self.signals.progress.emit)
        except Exception as exc:  # se muestra en la UI
            self.signals.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.signals.done.emit(result)


# --------------------------------------------------------------------------
# Vista de página
# --------------------------------------------------------------------------


class PageView(QGraphicsView):
    """Página renderizada con las detecciones encima; dibuja zonas."""

    zone_drawn = pyqtSignal(tuple)  # rectángulo en coordenadas de la página tal como se ve
    detection_clicked = pyqtSignal(int)

    def __init__(self) -> None:
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.scale_factor = 1.0  # px de escena por punto de página
        self.drawing = False
        self._start: QPointF | None = None
        self._band: QGraphicsRectItem | None = None
        self._items: dict[int, list[QGraphicsRectItem]] = {}

    def show_page(self, page: pymupdf.Page) -> None:
        scene = self.scene()
        scene.clear()
        self._items = {}
        self._band = None
        self.scale_factor = min(2.0, MAX_RENDER_SIDE / max(page.rect.width, page.rect.height))
        pix = page.get_pixmap(matrix=pymupdf.Matrix(self.scale_factor, self.scale_factor), alpha=False, annots=False)
        image = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
        scene.addPixmap(QPixmap.fromImage(image))
        scene.setSceneRect(QRectF(0, 0, pix.width, pix.height))

    def to_scene(self, page: pymupdf.Page, rect: tuple) -> QRectF:
        r = (pymupdf.Rect(rect) * page.rotation_matrix).normalize() * self.scale_factor
        return QRectF(r.x0, r.y0, max(r.width, 2), max(r.height, 2))

    def add_marks(self, page: pymupdf.Page, index: int, kind: str, rects: list[tuple]) -> None:
        color = QColor(COLORS.get(kind, "#000000"))
        fill = QColor(color)
        fill.setAlpha(45)
        pen = QPen(color, 2)
        pen.setCosmetic(True)
        for rect in rects:
            item = self.scene().addRect(self.to_scene(page, rect), pen, QBrush(fill))
            item.setData(0, index)
            item.setToolTip(kind_name(kind))
            self._items.setdefault(index, []).append(item)

    def highlight(self, page: pymupdf.Page, rects: list[tuple]) -> None:
        for item in self._items.pop(-1, []):
            self.scene().removeItem(item)
        pen = QPen(QColor(COLORS["seleccion"]), 3)
        pen.setCosmetic(True)
        for rect in rects:
            item = self.scene().addRect(self.to_scene(page, rect).adjusted(-4, -4, 4, 4), pen)
            self._items.setdefault(-1, []).append(item)
        if rects:
            self.centerOn(self.to_scene(page, rects[0]).center())

    def marks(self, index: int) -> list[QGraphicsRectItem]:
        return self._items.get(index, [])

    def zoom(self, factor: float) -> None:
        self.scale(factor, factor)

    def fit(self) -> None:
        self.fitInView(self.scene().sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def set_drawing(self, on: bool) -> None:
        self.drawing = on
        self.setDragMode(QGraphicsView.DragMode.NoDrag if on else QGraphicsView.DragMode.ScrollHandDrag)
        self.viewport().setCursor(Qt.CursorShape.CrossCursor if on else Qt.CursorShape.OpenHandCursor)

    # Eventos de ratón ---------------------------------------------------

    def wheelEvent(self, event: Any) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom(1.15 if event.angleDelta().y() > 0 else 1 / 1.15)
        else:
            super().wheelEvent(event)

    def mousePressEvent(self, event: Any) -> None:
        if self.drawing and event.button() == Qt.MouseButton.LeftButton:
            self._start = self.mapToScene(event.position().toPoint())
            pen = QPen(QColor(COLORS["zona"]), 2, Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            self._band = self.scene().addRect(QRectF(self._start, self._start), pen)
            return
        item = self.itemAt(event.position().toPoint())
        if item is not None and item.data(0) is not None and not self.drawing:
            self.detection_clicked.emit(int(item.data(0)))
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        if self.drawing and self._start is not None and self._band is not None:
            end = self.mapToScene(event.position().toPoint())
            self._band.setRect(QRectF(self._start, end).normalized())
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        if self.drawing and self._start is not None and self._band is not None:
            r = self._band.rect()
            self.scene().removeItem(self._band)
            self._band, self._start = None, None
            if r.width() > 4 and r.height() > 4:
                s = self.scale_factor
                self.zone_drawn.emit((r.left() / s, r.top() / s, r.right() / s, r.bottom() / s))
            return
        super().mouseReleaseEvent(event)


# --------------------------------------------------------------------------
# Diálogos
# --------------------------------------------------------------------------


class TermDialog(QDialog):
    """Añadir un término a una sustitución existente o crear una nueva."""

    def __init__(self, parent: QWidget, rules: list[Any], text: str, rule_id: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Añadir término a las reglas", "Add term to the rules"))
        form = QFormLayout(self)
        self.text = QLineEdit(" ".join(text.split()))
        form.addRow(tr("Término (texto literal):", "Term (literal text):"), self.text)
        self.rule = QComboBox()
        for r in rules:
            where = tr("  · lista negra (CSV)", "  · blacklist (CSV)") if r.word_only else ""
            self.rule.addItem(f"{r.id} → {r.replacement or tr('(eliminar)', '(remove)')}  [{r.category.value}]{where}", r.id)
        self.rule.addItem(tr("➕ Nueva regla…", "➕ New rule…"), "")
        if rule_id:
            self.rule.setCurrentIndex(max(0, self.rule.findData(rule_id)))
        form.addRow(tr("Regla:", "Rule:"), self.rule)
        self.new_id = QLineEdit()
        self.new_id.setPlaceholderText(tr("p. ej. plant_name", "e.g. plant_name"))
        self.category = QComboBox()
        self.category.addItems([c.value for c in Category])
        self.replacement = QLineEdit()
        self.replacement.setPlaceholderText(tr("p. ej. SITE o VENDOR-{n}", "e.g. SITE or VENDOR-{n}"))
        form.addRow(tr("Id de la nueva regla:", "New rule id:"), self.new_id)
        form.addRow(tr("Categoría:", "Category:"), self.category)
        form.addRow(tr("Sustitución:", "Replacement:"), self.replacement)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.rule.currentIndexChanged.connect(self._toggle)
        self._toggle()

    def _toggle(self) -> None:
        new = self.rule.currentData() == ""
        for w in (self.new_id, self.category, self.replacement):
            w.setEnabled(new)

    def values(self) -> dict[str, str]:
        return {
            "text": self.text.text(),
            "rule": self.rule.currentData(),
            "new_id": self.new_id.text().strip(),
            "category": self.category.currentText(),
            "replacement": self.replacement.text().strip(),
        }


class ZoneDialog(QDialog):
    def __init__(self, parent: QWidget, page: int, fraction: tuple) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Nueva zona", "New zone"))
        form = QFormLayout(self)
        form.addRow(QLabel(tr("Rectángulo (fracción de página): ", "Rectangle (page fraction): ") + ", ".join(f"{v:.3f}" for v in fraction)))
        self.scope = QComboBox()
        self.scope.addItem(tr("Solo esta página ({p})", "This page only ({p})", p=page), "page")
        self.scope.addItem(tr("Todas las páginas", "All pages"), "all")
        self.scope.addItem(tr("Páginas…", "Pages…"), "list")
        form.addRow(tr("Aplicar a:", "Apply to:"), self.scope)
        self.pages = QLineEdit()
        self.pages.setPlaceholderText(tr("p. ej. 1, 3-7, -1", "e.g. 1, 3-7, -1"))
        form.addRow(tr("Páginas:", "Pages:"), self.pages)
        self.label = QLineEdit()
        self.label.setPlaceholderText(tr("opcional, p. ej. [CAJETÍN]", "optional, e.g. [TITLE BLOCK]"))
        form.addRow(tr("Texto en la zona:", "Text in the zone:"), self.label)
        self.description = QLineEdit()
        form.addRow(tr("Descripción:", "Description:"), self.description)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.page = page

    def pages_value(self) -> str | list[int | str]:
        scope = self.scope.currentData()
        if scope == "all":
            return "all"
        if scope == "page":
            return [self.page]
        return parse_pages(self.pages.text())


def parse_pages(text: str) -> list[int | str]:
    """"1, 3-7, -1" → [1, "3-7", -1]."""
    out: list[int | str] = []
    for part in text.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        out.append(int(part) if part.lstrip("-").isdigit() else part)
    if not out:
        raise ValueError(tr("indica al menos una página", "give at least one page"))
    return out


# --------------------------------------------------------------------------
# Ventana principal
# --------------------------------------------------------------------------


class ReviewWindow(QMainWindow):
    def __init__(
        self,
        src: Path,
        config_path: Path,
        out_dir: Path = Path("data/out"),
        reports_dir: Path = Path("data/reports"),
        maps_dir: Path = Path("data/maps"),
        map_path: Path | None = None,
        repair_passes: int = 1,
        synchronous: bool = False,
    ) -> None:
        super().__init__()
        self.src, self.config_path = Path(src), Path(config_path)
        self.out_dir, self.reports_dir, self.maps_dir = Path(out_dir), Path(reports_dir), Path(maps_dir)
        self.map_path, self.repair_passes = map_path, repair_passes
        self.synchronous = synchronous  # tests: las tareas corren en el hilo de la UI
        self.editor = ConfigEditor(self.config_path)
        self.state: ReviewState | None = None
        self.stale = False
        self.busy = False
        self.page_number = 1
        self.show_anonymized = False
        self.original = pymupdf.open(self.src)
        self.anonymized: pymupdf.Document | None = None
        self._jobs: list[_Job] = []
        self._rows: list[int] = []  # fila → índice en state.detections
        self.setWindowTitle(tr("Revisión · {pdf} · {cfg}", "Review · {pdf} · {cfg}", pdf=self.src.name, cfg=self.config_path.name))
        self.resize(1500, 950)
        self._build()
        self._show_page()

    # Construcción ---------------------------------------------------------

    def _build(self) -> None:
        bar = QToolBar(tr("Acciones", "Actions"))
        self.addToolBar(bar)
        self.prev_btn = QPushButton("◀")
        self.prev_btn.setToolTip(tr("Página anterior", "Previous page"))
        self.next_btn = QPushButton("▶")
        self.next_btn.setToolTip(tr("Página siguiente", "Next page"))
        self.page_box = QSpinBox()
        self.page_box.setRange(1, len(self.original))
        self.page_box.setSuffix(f" / {len(self.original)}")
        self.prev_btn.clicked.connect(lambda: self.go_to(self.page_number - 1))
        self.next_btn.clicked.connect(lambda: self.go_to(self.page_number + 1))
        self.page_box.valueChanged.connect(self.go_to)
        for w in (self.prev_btn, self.page_box, self.next_btn):
            bar.addWidget(w)
        bar.addSeparator()
        self.view_toggle = QAction(tr("Ver anonimizado", "Show anonymized"), self, checkable=True)
        self.view_toggle.toggled.connect(self.set_anonymized_view)
        bar.addAction(self.view_toggle)
        bar.addAction(tr("Ajustar", "Fit"), lambda: self.view.fit())
        bar.addAction("＋", lambda: self.view.zoom(1.25))
        bar.addAction("－", lambda: self.view.zoom(0.8))
        bar.addSeparator()
        self.zone_action = QAction(tr("Dibujar zona", "Draw zone"), self, checkable=True)
        self.zone_action.toggled.connect(lambda on: self.view.set_drawing(on))
        bar.addAction(self.zone_action)
        bar.addSeparator()
        self.refresh_action = bar.addAction(tr("Recalcular", "Recalculate"), lambda: self.run_preview(ocr=False))
        self.ocr_action = bar.addAction(tr("Con OCR", "With OCR"), lambda: self.run_preview(ocr=True))
        self.generate_action = bar.addAction(tr("Confirmar y generar", "Confirm and generate"), self.on_generate)
        bar.addSeparator()
        self.about_action = bar.addAction(tr("Acerca de y licencias…", "About and licenses…"), self.show_about)

        self.view = PageView()
        self.view.zone_drawn.connect(self.on_zone_drawn)
        self.view.detection_clicked.connect(self.select_detection)

        right = QWidget()
        layout = QVBoxLayout(right)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        filters = QWidget()
        grid = QHBoxLayout(filters)
        grid.setContentsMargins(0, 0, 0, 0)
        self.kind_boxes: dict[str, QCheckBox] = {}
        for kind in KINDS:
            box = QCheckBox(kind_name(kind))
            box.setChecked(True)
            box.setToolTip(kind_description(kind))
            box.setStyleSheet(f"color: {COLORS[kind]}; font-weight: bold;")
            box.toggled.connect(self._refresh)
            self.kind_boxes[kind] = box
        for kind in list(KINDS)[:4]:
            grid.addWidget(self.kind_boxes[kind])
        layout.addWidget(filters)
        filters2 = QWidget()
        grid2 = QHBoxLayout(filters2)
        grid2.setContentsMargins(0, 0, 0, 0)
        for kind in list(KINDS)[4:]:
            grid2.addWidget(self.kind_boxes[kind])
        layout.addWidget(filters2)
        self.page_only = QCheckBox(tr("Solo esta página", "This page only"))
        self.page_only.setChecked(True)
        self.page_only.toggled.connect(self._refresh)
        layout.addWidget(self.page_only)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            [tr("Tipo", "Type"), tr("Pág.", "Page"), tr("Texto", "Text"), tr("Detalle", "Detail"), tr("Regla", "Rule")]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._on_selection)
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        self.discard_btn = QPushButton(tr("Descartar", "Discard"))
        self.discard_btn.clicked.connect(self.on_discard)
        self.term_btn = QPushButton(tr("Añadir término…", "Add term…"))
        self.term_btn.clicked.connect(self.on_add_term)
        buttons.addWidget(self.discard_btn)
        buttons.addWidget(self.term_btn)
        layout.addLayout(buttons)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        layout.addWidget(self.log)
        self.report_btn = QPushButton(tr("Abrir informe de auditoría", "Open audit report"))
        self.report_btn.clicked.connect(self.open_report)
        self.report_btn.setEnabled(False)
        layout.addWidget(self.report_btn)

        splitter = QSplitter()
        splitter.addWidget(self.view)
        splitter.addWidget(right)
        splitter.setSizes([1000, 500])
        self.setCentralWidget(splitter)
        self._update_status()

    # Estado ---------------------------------------------------------------

    def start(self) -> None:
        """Carga informes vigentes o lanza la vista previa rápida."""
        state = load_existing(self.src, self.config_path, self.out_dir, self.reports_dir)
        if state is not None:
            self.set_state(state)
            self.say(tr("Informes de un proceso anterior cargados.", "Reports from a previous run loaded."))
        else:
            self.run_preview(ocr=False)

    def set_state(self, state: ReviewState | None) -> None:
        self.state = state
        self.stale = False
        if self.anonymized is not None:
            self.anonymized.close()
            self.anonymized = None
        if state is not None and state.anonymized is not None and state.anonymized.exists():
            # En memoria: un PDF abierto queda bloqueado en Windows y la
            # siguiente vista previa no podría sobrescribirlo
            self.anonymized = pymupdf.open(stream=state.anonymized.read_bytes(), filetype="pdf")
        self.report_btn.setEnabled(self._report_path().exists())
        self._update_status()
        self._show_page()

    def _update_status(self) -> None:
        if self.busy:
            text = tr("⏳ Trabajando…", "⏳ Working…")
        elif self.state is None:
            text = tr("Sin detecciones todavía.", "No detections yet.")
        else:
            counts: dict[str, int] = {}
            for d in self.state.detections:
                counts[d.kind] = counts.get(d.kind, 0) + 1
            text = tr("Origen: {o} · ", "Source: {o} · ", o=origin_name(self.state.origin)) + ", ".join(
                f"{kind_name(k)} {v}" for k, v in counts.items()
            )
            if self.state.audit is not None and self.state.origin in ("informes", "generado"):
                text += tr(" · auditoría ", " · audit ") + ("OK" if self.state.audit.get("ok") else tr("con FALLOS", "with FAILURES"))
            if self.state.origin == "vista previa":
                text += tr(
                    "\nSin OCR: no aparece el texto en trazos ni el escaneado. Para verlo, «Con OCR».",
                    "\nNo OCR: text drawn as paths and scanned text are not shown. To see them, use “With OCR”.",
                )
        if self.stale:
            text += tr("\n⚠ La config cambió: pulsa «Recalcular» para ver el efecto.", "\n⚠ The config changed: press “Recalculate” to see the effect.")
        self.status.setText(text)
        for action in (self.refresh_action, self.ocr_action, self.generate_action):
            action.setEnabled(not self.busy)

    def say(self, message: str) -> None:
        self.log.appendPlainText(message)

    def mark_stale(self, message: str) -> None:
        self.stale = True
        self.say(message)
        self._update_status()

    # Tareas -----------------------------------------------------------

    def _run(self, fn: Callable, on_done: Callable[[Any], None]) -> None:
        if self.busy:
            return
        self.busy = True
        self._update_status()

        def finish(result: Any) -> None:
            self.busy = False
            on_done(result)
            self._update_status()

        def fail(message: str) -> None:
            self.busy = False
            self.say(f"ERROR: {message}")
            self._update_status()

        if self.synchronous:
            try:
                result = fn(self.say)
            except Exception as exc:
                fail(f"{type(exc).__name__}: {exc}")
                return
            finish(result)
            return
        job = _Job(fn)
        job.signals.progress.connect(self.say)
        job.signals.done.connect(finish)
        job.signals.failed.connect(fail)
        job.signals.done.connect(lambda _r, j=job: self._jobs.remove(j))
        job.signals.failed.connect(lambda _m, j=job: self._jobs.remove(j))
        self._jobs.append(job)
        QThreadPool.globalInstance().start(job)

    def run_preview(self, ocr: bool = False) -> None:
        config = self.editor.config
        self.say(tr("Vista previa con OCR (puede tardar)…", "Preview with OCR (may take a while)…") if ocr else tr("Vista previa rápida (sin OCR)…", "Quick preview (no OCR)…"))
        self._run(
            lambda progress: preview(self.src, config, self.reports_dir, self.maps_dir, ocr, progress),
            lambda state: (self.set_state(state), self.say(tr("Listo: {n} detecciones.", "Done: {n} detections.", n=len(state.detections)))),
        )

    def run_generate(self) -> None:
        config = self.editor.config
        self.say(tr("Generando: anonimizado, metadatos, auditoría y autocorrección…", "Generating: anonymization, metadata, audit and self-repair…"))

        def done(result: Any) -> None:
            doc_result, state = result
            if state is not None:
                self.set_state(state)
            status = "OK" if doc_result.ok else tr("con FALLOS", "with FAILURES")
            self.say(
                tr(
                    "Generado {st}: {out} · {n} sustituciones · restos {l} · para revisar {r} · {s:.0f} s",
                    "Generated {st}: {out} · {n} substitutions · leftovers {l} · to review {r} · {s:.0f} s",
                    st=status, out=doc_result.output, n=doc_result.substitutions,
                    l=doc_result.leftovers, r=doc_result.review_lines, s=doc_result.seconds,
                )
            )
            self.say(tr("Informe: {p}", "Report: {p}", p=self._report_path()))
            self.view_toggle.setChecked(True)

        self._run(
            lambda progress: generate(
                self.src, config, self.config_path, self.out_dir, self.reports_dir, self.maps_dir, self.map_path, self.repair_passes
            ),
            done,
        )

    # Páginas y lista ----------------------------------------------------

    def _document(self) -> pymupdf.Document:
        if self.show_anonymized and self.anonymized is not None:
            return self.anonymized
        return self.original

    def go_to(self, number: int) -> None:
        number = max(1, min(len(self.original), int(number)))
        if number == self.page_number and self.page_box.value() == number:
            return
        self.page_number = number
        self.page_box.blockSignals(True)
        self.page_box.setValue(number)
        self.page_box.blockSignals(False)
        self._show_page()

    def set_anonymized_view(self, on: bool) -> None:
        if on and self.anonymized is None:
            self.say(tr("Todavía no hay versión anonimizada.", "There is no anonymized version yet."))
        self.show_anonymized = on
        self._show_page()

    def _visible(self, d: Detection) -> bool:
        if not self.kind_boxes[d.kind].isChecked():
            return False
        return d.on_page(self.page_number) or not self.page_only.isChecked()

    def _show_page(self) -> None:
        page = self._document()[self.page_number - 1]
        self.view.show_page(page)
        if self.state is not None:
            for i, d in enumerate(self.state.detections):
                if d.on_page(self.page_number) and d.rects and self.kind_boxes[d.kind].isChecked():
                    self.view.add_marks(page, i, d.kind, d.rects)
        self._fill_table()

    def _refresh(self) -> None:
        self._show_page()

    def _fill_table(self) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self._rows = []
        if self.state is not None:
            for i, d in enumerate(self.state.detections):
                if not self._visible(d):
                    continue
                row = self.table.rowCount()
                self.table.insertRow(row)
                page = str(d.page) if d.page else ", ".join(map(str, d.pages[:6])) + ("…" if len(d.pages) > 6 else "")
                for col, value in enumerate((kind_name(d.kind), page, d.text, d.detail, d.rule)):
                    item = QTableWidgetItem(value)
                    if col == 0:
                        item.setForeground(QBrush(QColor(COLORS[d.kind])))
                    self.table.setItem(row, col, item)
                self._rows.append(i)
        self.table.blockSignals(False)
        self.table.resizeColumnToContents(0)
        self.table.resizeColumnToContents(1)
        self._on_selection()

    def selected(self) -> Detection | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows or self.state is None:
            return None
        return self.state.detections[self._rows[rows[0].row()]]

    def select_detection(self, index: int) -> None:
        if index in self._rows:
            self.table.selectRow(self._rows.index(index))

    def _on_selection(self) -> None:
        d = self.selected()
        self.discard_btn.setEnabled(d is not None and d.discardable and not self.busy)
        self.discard_btn.setToolTip(discard_action(d) or "" if d else "")
        self.term_btn.setEnabled(not self.busy)
        if d is None:
            return
        if not d.on_page(self.page_number):
            target = d.page or (d.pages[0] if d.pages else self.page_number)
            if target != self.page_number:
                self.go_to(target)
                self.select_detection(self.state.detections.index(d))
                return
        page = self._document()[self.page_number - 1]
        rects = d.rects or locate(self.original[self.page_number - 1], d.text)
        self.view.highlight(page, rects)

    # Acciones -------------------------------------------------------------

    def discard_selected(self) -> str:
        d = self.selected()
        if d is None:
            raise ValueError(tr("no hay ninguna detección seleccionada", "no detection is selected"))
        action = discard(self.editor, d)
        self.mark_stale(tr("Descartado: {a}.", "Discarded: {a}.", a=action))
        return action

    def add_term(self, rule_id: str, text: str) -> None:
        self.editor.add_term(rule_id, text)
        self.mark_stale(tr("Término '{t}' añadido a la regla '{r}'.", "Term '{t}' added to rule '{r}'.", t=" ".join(text.split()), r=rule_id))

    def add_rule(self, rule_id: str, category: str, replacement: str, text: str) -> None:
        self.editor.add_rule(rule_id, category, replacement, [text])
        self.mark_stale(
            tr(
                "Regla nueva '{r}' ({c} → {rep}) con '{t}'.",
                "New rule '{r}' ({c} → {rep}) with '{t}'.",
                r=rule_id, c=category, rep=replacement or tr("(eliminar)", "(remove)"), t=text,
            )
        )

    def create_zone(self, view_rect: tuple, pages: str | list[int | str], label: str = "", description: str = "") -> str:
        page = self.original[self.page_number - 1]
        zone_id, _ = self.editor.add_zone(zone_fraction(page, view_rect), pages, label, description)
        self.mark_stale(tr("Zona '{z}' añadida (páginas: {p}).", "Zone '{z}' added (pages: {p}).", z=zone_id, p=pages))
        return zone_id

    def _guard(self, fn: Callable[[], Any]) -> None:
        try:
            fn()
        except (EditError, ValueError) as exc:
            QMessageBox.warning(self, tr("Cambio rechazado", "Change rejected"), str(exc))

    def on_discard(self) -> None:
        d = self.selected()
        if d is None:
            return
        answer = QMessageBox.question(
            self,
            tr("Descartar detección", "Discard detection"),
            tr("Se guardará en la config: {a}.\n¿Seguir?", "This will be saved in the config: {a}.\nContinue?", a=discard_action(d)),
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._guard(self.discard_selected)

    def on_add_term(self) -> None:
        d = self.selected()
        text = d.text if d is not None and d.kind != "rozada" else ""
        rule = d.rule if d is not None and d.kind in ("sustitución", "rozada", "resto", "aviso") else ""
        dialog = TermDialog(self, list(self.editor.config.substitutions), text, rule)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        v = dialog.values()
        if v["rule"]:
            self._guard(lambda: self.add_term(v["rule"], v["text"]))
        else:
            self._guard(lambda: self.add_rule(v["new_id"], v["category"], v["replacement"], v["text"]))

    def on_zone_drawn(self, view_rect: tuple) -> None:
        page = self.original[self.page_number - 1]
        dialog = ZoneDialog(self, self.page_number, zone_fraction(page, view_rect))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._guard(lambda: self.create_zone(view_rect, dialog.pages_value(), dialog.label.text(), dialog.description.text()))
        self.zone_action.setChecked(False)

    def show_about(self) -> None:
        from sanitizer.about import AboutDialog

        AboutDialog(self).exec()

    def on_generate(self) -> None:
        note = tr("\n\nOjo: hay cambios en la config que aún no has recalculado.", "\n\nNote: there are config changes you have not recalculated yet.") if self.stale else ""
        answer = QMessageBox.question(
            self,
            tr("Confirmar y generar", "Confirm and generate"),
            tr(
                "Se ejecutará el proceso completo con OCR, metadatos, auditoría y autocorrección "
                "({n} pasada/s).\nSalida: {out}{note}\n\n¿Generar?",
                "The full process will run with OCR, metadata, audit and self-repair "
                "({n} pass(es)).\nOutput: {out}{note}\n\nGenerate?",
                n=self.repair_passes, out=self.out_dir / (self.src.stem + ".sanitized.pdf"), note=note,
            ),
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.run_generate()

    def _report_path(self) -> Path:
        return self.reports_dir / self.src.stem / "auditoria.md"

    def open_report(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._report_path().resolve())))

    def closeEvent(self, event: Any) -> None:
        QThreadPool.globalInstance().waitForDone(100)
        self.original.close()
        if self.anonymized is not None:
            self.anonymized.close()
        super().closeEvent(event)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="review",
        description=tr(
            "UI de revisión: ver y descartar detecciones, añadir términos y zonas, y generar el PDF anonimizado.",
            "Review UI: view and discard detections, add terms and zones, and generate the anonymized PDF.",
        ),
    )
    parser.add_argument("pdf", type=Path, help=tr("PDF original", "original PDF"))
    parser.add_argument("--config", type=Path, required=True, help=tr("config YAML del cliente (se edita desde la UI)", "client YAML config (edited from the UI)"))
    parser.add_argument("--out", type=Path, default=Path("data/out"))
    parser.add_argument("--reports", type=Path, default=Path("data/reports"))
    parser.add_argument("--maps", type=Path, default=Path("data/maps"))
    parser.add_argument("--map", type=Path, help=tr("mapa común del proyecto", "shared project map"))
    parser.add_argument("--repair", type=int, default=1, metavar="N", help=tr("pasadas de autocorrección al generar", "self-repair passes when generating"))
    args = parser.parse_args(argv)
    if not args.pdf.is_file():
        parser.error(tr("no existe {p}", "{p} does not exist", p=args.pdf))
    for folder, label in ((args.reports, "--reports"), (args.maps, "--maps")):
        if not inside_data(folder / "x"):
            parser.error(tr("{label} debe estar dentro de data/: contiene valores originales", "{label} must be inside data/: it contains original values", label=label))
    if args.map is not None and not inside_data(args.map):
        parser.error(tr("--map debe estar dentro de data/", "--map must be inside data/"))
    if args.config.name in ("base.yaml", "example.yaml"):
        parser.error(
            tr(
                "la UI edita la config: usa la de un cliente (configs/<cliente>.yaml), no base ni example",
                "the UI edits the config: use a client one (configs/<client>.yaml), not base or example",
            )
        )

    app = QApplication.instance() or QApplication(sys.argv[:1])
    install_translations(app)
    window = ReviewWindow(args.pdf, args.config, args.out, args.reports, args.maps, args.map, args.repair)
    window.show()
    window.start()
    return app.exec()


if __name__ == "__main__":
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    raise SystemExit(main())
