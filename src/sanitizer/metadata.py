"""Fase 3 · Metadatos: limpieza de todo lo que no es contenido de página.

Elimina o neutraliza, con pikepdf y a bajo nivel:

- Diccionario Info (en el trailer y, por rarezas de algunos generadores,
  también dentro del catálogo) y XMP del documento y de cualquier objeto.
- `/ID` del trailer: se sustituye por un valor fijo e igual para todos los
  documentos (no identifica nada y la salida es determinista).
- Datos privados de aplicación: `/PieceInfo`, `/LastModified`, `/OPI`.
- Adjuntos (`EmbeddedFiles`, `/AF`, portfolios) y anotaciones de fichero o
  multimedia.
- JavaScript y acciones peligrosas (`/OpenAction`, `/AA`, `/JS`, Launch, URI…).
- Formularios: se conservan (su apariencia es parte del plano) pero sin XFA,
  con nombres de campo genéricos y sin tooltips ni acciones.
- Etiquetas de página, árbol de estructura
  (`/Alt` y `/ActualText` pueden llevar texto), hilos de artículo, miniaturas
  y destinos con nombre.
- Anotaciones, capas (OCG) y marcadores según la política (`MetadataPolicy`).

Los destinos (marcadores, enlaces, /OpenAction) que apuntan a páginas que
no están en el árbol de páginas se descartan: esas páginas "fantasma" (restos
de documentos combinados) se escribirían en la salida con su contenido.

Al guardar se reescribe el fichero entero: desaparecen las revisiones
incrementales anteriores y los objetos huérfanos, que también pueden contener
texto antiguo.

No toca el contenido de las páginas (texto, trazos, imágenes): eso es cosa de
las fases 4 y 5. En el pipeline completo esta limpieza va al final, para que
ninguna herramienta reintroduzca metadatos al guardar.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Literal

import pikepdf
from pikepdf import Array, Dictionary, Name, Stream, String
from pydantic import ConfigDict

from sanitizer.i18n import tr

# /ID fijo que escribe qpdf con static_id
STATIC_ID = bytes.fromhex("31415926535897932384626433832795")

# Claves que se eliminan de cualquier objeto en que aparezcan
PRIVATE_KEYS = ("/Metadata", "/PieceInfo", "/LastModified", "/OPI", "/AF")

# Claves del catálogo que se eliminan siempre
CATALOG_KEYS = (
    "/Info",  # algunos generadores lo duplican aquí
    "/Metadata",
    "/PageLabels",
    "/StructTreeRoot",
    "/MarkInfo",
    "/Threads",
    "/Collection",
    "/AA",
    "/URI",
    "/SpiderInfo",
    "/Legal",
    "/Perms",
    "/Dests",
    "/AF",
)

# Claves de página que se eliminan siempre
PAGE_KEYS = ("/Thumb", "/AA", "/StructParents", "/B", "/ID", "/PZ")

# Anotaciones que se eliminan siempre: adjuntos y multimedia
ALWAYS_REMOVED_ANNOTS = {"/FileAttachment", "/Sound", "/Movie", "/Screen", "/RichMedia", "/3D"}

# Claves de autoría y flujo de revisión de una anotación
AUTHOR_KEYS = ("/T", "/M", "/CreationDate", "/NM", "/IRT", "/RT", "/Popup", "/RC", "/ExData")

# Claves de un widget o campo de formulario que identifican o describen
# (el nombre /T no se borra: se sustituye por uno genérico)
WIDGET_KEYS = ("/TU", "/TM", "/M", "/NM", "/AA")

# Acciones que se consideran internas y seguras
SAFE_ACTIONS = {"/GoTo"}


@dataclass(frozen=True)
class MetadataPolicy:
    """Qué hacer con los elementos que mezclan datos sensibles y técnicos."""

    __pydantic_config__ = ConfigDict(extra="forbid")

    # strip_authors: se conservan con su contenido, sin autor, fechas, IDs,
    # respuestas, popups ni marcas de estado de revisión
    annotations: Literal["strip_authors", "remove", "keep"] = "strip_authors"
    # Enlaces: se eliminan, o se conservan solo los internos (a otra página)
    links: Literal["remove", "keep_internal"] = "remove"
    # Capas: nombre genérico conservando su estado visible u oculto
    layers: Literal["rename", "keep"] = "rename"
    layer_name: str = "Capa-{n:03d}"
    layer_group_name: str = "Grupo-{n:03d}"
    # Marcadores: estructura conservada con títulos genéricos
    outline: Literal["generic", "remove", "keep"] = "generic"
    outline_title: str = "Página {page}"
    outline_title_no_page: str = "Marcador {n}"
    # Formularios: nombre genérico de cada campo
    field_name: str = "Campo-{n:03d}"

    def __post_init__(self) -> None:
        for name, value, sample in (
            ("layer_name", self.layer_name, {"n": 1}),
            ("layer_group_name", self.layer_group_name, {"n": 1}),
            ("outline_title", self.outline_title, {"page": 1}),
            ("outline_title_no_page", self.outline_title_no_page, {"n": 1}),
            ("field_name", self.field_name, {"n": 1}),
        ):
            try:
                value.format(**sample)
            except (KeyError, IndexError, ValueError) as exc:
                raise ValueError(tr("{name} mal formado ({value}): {exc}", "malformed {name} ({value}): {exc}", name=name, value=repr(value), exc=exc)) from None


DEFAULT_POLICY = MetadataPolicy()


@dataclass(frozen=True)
class _Dests:
    """Destinos con nombre y páginas válidas, para resolver enlaces y marcadores."""

    named: dict[str, Any]
    pages: dict[tuple[int, int], int]  # objgen de página → número (1-based)


@dataclass
class CleanReport:
    """Qué se ha eliminado o cambiado, para el informe de auditoría."""

    info_keys: list[str] = field(default_factory=list)
    xmp_streams: int = 0
    private_keys: int = 0
    catalog_keys: list[str] = field(default_factory=list)
    embedded_files: int = 0
    javascript: int = 0
    actions: int = 0
    annotations_removed: dict[str, int] = field(default_factory=dict)
    annotations_stripped: int = 0
    layers_renamed: int = 0
    fields_renamed: int = 0
    outline_items: int = 0
    document_id_replaced: bool = False

    def count_removed(self, subtype: str) -> None:
        key = subtype.lstrip("/")
        self.annotations_removed[key] = self.annotations_removed.get(key, 0) + 1


# --------------------------------------------------------------------------
# Limpieza
# --------------------------------------------------------------------------


def clean_metadata(
    src: str | Path, dst: str | Path, policy: MetadataPolicy = DEFAULT_POLICY
) -> CleanReport:
    """Limpia los metadatos de `src` y guarda el resultado en `dst`."""
    src, dst = Path(src), Path(dst)
    if src.resolve() == dst.resolve():
        raise ValueError(tr("la salida no puede sobrescribir el PDF original", "the output cannot overwrite the original PDF"))

    report = CleanReport()
    with pikepdf.open(src) as pdf:
        dests = _Dests(
            _named_destinations(pdf), {p.obj.objgen: i + 1 for i, p in enumerate(pdf.pages)}
        )

        _clean_info_and_id(pdf, report)
        _clean_javascript_and_files(pdf, report)
        _clean_outline(pdf, policy, dests, report)
        _clean_open_action(pdf, dests, report)
        for page in pdf.pages:
            _clean_page(page.obj, policy, dests, report)
        _clean_forms(pdf, policy, report)
        _clean_layers(pdf, policy, report)
        _clean_catalog(pdf, report)
        _sweep_objects(pdf, report)

        pdf.save(dst, static_id=True, encryption=False)
    return report


def _clean_info_and_id(pdf: pikepdf.Pdf, report: CleanReport) -> None:
    if "/Info" in pdf.trailer:
        info = pdf.trailer.Info
        report.info_keys = [str(k) for k in info.keys()] if isinstance(info, Dictionary) else []
        del pdf.trailer["/Info"]
    if "/ID" in pdf.trailer:
        # Sin /ID, qpdf escribe uno fijo con static_id (ver STATIC_ID)
        del pdf.trailer["/ID"]
        report.document_id_replaced = True


def _clean_javascript_and_files(pdf: pikepdf.Pdf, report: CleanReport) -> None:
    names = pdf.Root.get("/Names")
    if isinstance(names, Dictionary):
        if "/JavaScript" in names:
            report.javascript += max(1, len(_name_tree_items(names.JavaScript)))
            del names["/JavaScript"]
        if "/EmbeddedFiles" in names:
            report.embedded_files += len(_name_tree_items(names.EmbeddedFiles))
            del names["/EmbeddedFiles"]
        for key in ("/Dests", "/AP", "/Pages", "/Templates", "/IDS", "/URLS", "/Renditions"):
            if key in names:
                del names[key]
        if not names.keys():
            del pdf.Root["/Names"]


def _clean_open_action(pdf: pikepdf.Pdf, dests: _Dests, report: CleanReport) -> None:
    action = pdf.Root.get("/OpenAction")
    if action is None:
        return
    dest = (
        _resolve_dest(action, dests)
        if isinstance(action, Array)
        else _internal_destination(action, dests)
    )
    if dest is None:
        del pdf.Root["/OpenAction"]
        report.actions += 1
    else:
        pdf.Root.OpenAction = pdf.make_indirect(Dictionary(S=Name.GoTo, D=dest))


def _clean_catalog(pdf: pikepdf.Pdf, report: CleanReport) -> None:
    for key in CATALOG_KEYS:
        if key in pdf.Root:
            report.catalog_keys.append(key.lstrip("/"))
            if key == "/Metadata":
                report.xmp_streams += 1
            del pdf.Root[key]


def _clean_page(
    page: Dictionary, policy: MetadataPolicy, dests: _Dests, report: CleanReport
) -> None:
    for key in PAGE_KEYS:
        if key in page:
            del page[key]
    annots = page.get("/Annots")
    if not isinstance(annots, Array):
        return

    # Los popups se resuelven por su objeto: se quitan con su anotación padre
    keep: list[Any] = []
    for annot in annots:
        if not isinstance(annot, Dictionary):
            continue
        subtype = str(annot.get("/Subtype", "/Unknown"))
        if _drop_annotation(annot, subtype, policy, dests):
            report.count_removed(subtype)
            continue
        if policy.annotations == "strip_authors":
            _strip_annotation(annot, subtype)
            report.annotations_stripped += 1
        else:
            _remove_actions(annot, report)
        keep.append(annot)

    if keep:
        page.Annots = Array(keep)
    else:
        del page["/Annots"]


def _drop_annotation(
    annot: Dictionary, subtype: str, policy: MetadataPolicy, dests: _Dests
) -> bool:
    if subtype in ALWAYS_REMOVED_ANNOTS:
        return True
    if subtype == "/Link":
        if policy.annotations == "remove" or policy.links == "remove":
            return True
        dest = _link_destination(annot, dests)
        if dest is None:
            return True
        for key in ("/A", "/Dest", "/PA"):
            if key in annot:
                del annot[key]
        annot.Dest = dest
        return False
    if policy.annotations == "remove":
        return True
    if policy.annotations == "strip_authors":
        # Ventanas emergentes y marcas de estado ("Accepted set by …"): flujo de revisión
        if subtype == "/Popup" or "/StateModel" in annot or "/State" in annot:
            return True
    return False


def _strip_annotation(annot: Dictionary, subtype: str) -> None:
    keys = WIDGET_KEYS if subtype == "/Widget" else AUTHOR_KEYS
    for key in keys:
        if key in annot:
            del annot[key]
    for key in ("/A", "/AA"):
        if key in annot:
            del annot[key]


def _clean_forms(pdf: pikepdf.Pdf, policy: MetadataPolicy, report: CleanReport) -> None:
    """Formularios: sin XFA, nombres de campo genéricos, sin tooltips ni acciones."""
    acro = pdf.Root.get("/AcroForm")
    if policy.annotations == "remove":
        if acro is not None:
            del pdf.Root["/AcroForm"]
            report.catalog_keys.append("AcroForm")
        return
    if isinstance(acro, Dictionary) and "/XFA" in acro:
        del acro["/XFA"]
        report.catalog_keys.append("AcroForm/XFA")

    # Campos del árbol /Fields y widgets sueltos (hay PDF con widgets que no
    # cuelgan de /AcroForm): todos los que tengan nombre
    fields: list[Dictionary] = []
    seen: set[tuple[int, int]] = set()

    def add(node: Any) -> None:
        if not isinstance(node, Dictionary):
            return
        key = node.objgen if node.is_indirect else (id(node), 0)
        if key in seen:
            return
        seen.add(key)
        fields.append(node)
        for kid in node.get("/Kids", Array()):
            add(kid)

    if isinstance(acro, Dictionary):
        for f in acro.get("/Fields", Array()):
            add(f)
    for page in pdf.pages:
        for annot in page.obj.get("/Annots", Array()):
            if isinstance(annot, Dictionary) and annot.get("/Subtype") == Name.Widget:
                add(annot)
                parent = annot.get("/Parent")
                while isinstance(parent, Dictionary):
                    add(parent)
                    parent = parent.get("/Parent")

    n = 0
    for f in fields:
        for key in WIDGET_KEYS + ("/A",):
            if key in f:
                del f[key]
        if "/T" in f:
            n += 1
            f.T = String(policy.field_name.format(n=n))
    report.fields_renamed = n


def _remove_actions(annot: Dictionary, report: CleanReport) -> None:
    if "/AA" in annot:
        del annot["/AA"]
        report.actions += 1
    action = annot.get("/A")
    if isinstance(action, Dictionary) and str(action.get("/S")) not in SAFE_ACTIONS:
        del annot["/A"]
        report.actions += 1


def _clean_layers(pdf: pikepdf.Pdf, policy: MetadataPolicy, report: CleanReport) -> None:
    props = pdf.Root.get("/OCProperties")
    if policy.layers == "keep":
        return
    if not isinstance(props, Dictionary):
        props = Dictionary()
    # Primero las capas registradas, en su orden; después cualquier otro OCG
    # (hay PDF con OCG referenciados desde el contenido pero no registrados)
    ocgs = [o for o in props.get("/OCGs", Array()) if isinstance(o, Dictionary)]
    registered = {o.objgen for o in ocgs if o.is_indirect}
    ocgs += [
        o
        for o in pdf.objects
        if isinstance(o, Dictionary) and o.get("/Type") == Name.OCG and o.objgen not in registered
    ]
    for n, ocg in enumerate(ocgs, start=1):
        ocg.Name = String(policy.layer_name.format(n=n))
        usage = ocg.get("/Usage")
        if isinstance(usage, Dictionary) and "/CreatorInfo" in usage:
            del usage["/CreatorInfo"]
        report.layers_renamed += 1

    configs = [props.get("/D")] + list(props.get("/Configs", Array()))
    counter = [0]
    for cfg in configs:
        if not isinstance(cfg, Dictionary):
            continue
        for key in ("/Name", "/Creator"):
            if key in cfg:
                del cfg[key]
        if "/Order" in cfg:
            cfg.Order = _rename_order(cfg.Order, policy, counter)


def _rename_order(order: Any, policy: MetadataPolicy, counter: list[int]) -> Any:
    """En /Order, las cadenas son etiquetas de grupos de capas: también se renombran."""
    if isinstance(order, String):
        counter[0] += 1
        return String(policy.layer_group_name.format(n=counter[0]))
    if isinstance(order, Array) and not order.is_indirect:
        return Array([_rename_order(item, policy, counter) for item in order])
    return order


def _clean_outline(
    pdf: pikepdf.Pdf, policy: MetadataPolicy, dests: _Dests, report: CleanReport
) -> None:
    if "/Outlines" not in pdf.Root:
        return
    if policy.outline == "remove":
        report.outline_items = sum(1 for _ in _outline_items(pdf.Root.Outlines))
        del pdf.Root["/Outlines"]
        if str(pdf.Root.get("/PageMode")) == "/UseOutlines":
            del pdf.Root["/PageMode"]
        return
    for n, item in enumerate(_outline_items(pdf.Root.Outlines), start=1):
        report.outline_items += 1
        dest = _link_destination(item, dests)
        for key in ("/A", "/Dest", "/SE"):
            if key in item:
                del item[key]
        if dest is not None:
            item.Dest = dest
        if policy.outline == "generic":
            page = dests.pages[dest[0].objgen] if dest is not None else None
            title = (
                policy.outline_title.format(page=page)
                if page
                else policy.outline_title_no_page.format(n=n)
            )
            item.Title = String(title)


def _sweep_objects(pdf: pikepdf.Pdf, report: CleanReport) -> None:
    """Recorre todos los objetos: XMP, datos privados y JavaScript fuera de sitio."""
    for obj in pdf.objects:
        if not isinstance(obj, (Dictionary, Stream)):
            continue
        for key in PRIVATE_KEYS:
            if key in obj:
                if key == "/Metadata":
                    report.xmp_streams += 1
                else:
                    report.private_keys += 1
                del obj[key]
        if "/JS" in obj or str(obj.get("/S")) == "/JavaScript":
            # Acción JavaScript que no estaba en un sitio conocido: se vacía
            for key in list(obj.keys()):
                del obj[key]
            report.javascript += 1


# --------------------------------------------------------------------------
# Destinos
# --------------------------------------------------------------------------


def _name_tree_items(node: Any) -> list[tuple[str, Any]]:
    try:
        return [(str(k), v) for k, v in pikepdf.NameTree(node).items()]
    except Exception:  # árbol mal formado: se elimina igualmente
        return []


def _named_destinations(pdf: pikepdf.Pdf) -> dict[str, Any]:
    dests: dict[str, Any] = {}
    names = pdf.Root.get("/Names")
    if isinstance(names, Dictionary) and "/Dests" in names:
        dests.update(_name_tree_items(names.Dests))
    old = pdf.Root.get("/Dests")
    if isinstance(old, Dictionary):
        dests.update((str(k).lstrip("/"), v) for k, v in old.items())
    return dests


def _resolve_dest(dest: Any, dests: _Dests) -> Array | None:
    """Destino explícito [página, …] o None si no se resuelve a una página real."""
    if isinstance(dest, (Name, String)):
        key = str(dest).lstrip("/") if isinstance(dest, Name) else str(dest)
        dest = dests.named.get(key)
    if isinstance(dest, Dictionary):
        dest = dest.get("/D")
    if (
        isinstance(dest, Array)
        and len(dest)
        and isinstance(dest[0], Dictionary)
        and dest[0].is_indirect
        and dest[0].objgen in dests.pages  # descarta páginas fantasma
    ):
        return Array(list(dest))
    return None


def _internal_destination(action: Any, dests: _Dests) -> Array | None:
    if isinstance(action, Dictionary) and str(action.get("/S")) == "/GoTo":
        return _resolve_dest(action.get("/D"), dests)
    return None


def _link_destination(obj: Dictionary, dests: _Dests) -> Array | None:
    """Destino interno de un enlace o marcador, ya resuelto a página explícita."""
    if "/Dest" in obj:
        return _resolve_dest(obj.Dest, dests)
    return _internal_destination(obj.get("/A"), dests)


def _outline_items(root: Dictionary) -> Iterator[Dictionary]:
    """Marcadores en orden de documento (en profundidad), a prueba de ciclos."""
    seen: set[tuple[int, int]] = set()
    stack = [root.get("/First")]
    while stack:
        item = stack.pop()
        if not isinstance(item, Dictionary):
            continue
        if item.is_indirect:
            if item.objgen in seen:
                continue
            seen.add(item.objgen)
        yield item
        # Se apila primero el hermano para visitar antes a los hijos
        if "/Next" in item:
            stack.append(item.Next)
        if "/First" in item:
            stack.append(item.First)


# --------------------------------------------------------------------------
# Verificación
# --------------------------------------------------------------------------


def verify_metadata(path: str | Path, policy: MetadataPolicy = DEFAULT_POLICY) -> list[str]:
    """Relee el PDF con pikepdf y devuelve lo que quede por limpiar (vacío = limpio)."""
    problems: list[str] = []
    layer_rx = _template_regex(policy.layer_name)
    group_rx = _template_regex(policy.layer_group_name)
    title_rxs = [_template_regex(policy.outline_title), _template_regex(policy.outline_title_no_page)]

    with pikepdf.open(path) as pdf:
        if "/Info" in pdf.trailer:
            problems.append(tr("el trailer tiene diccionario /Info", "the trailer has an /Info dictionary"))
        ids = pdf.trailer.get("/ID")
        if ids is not None and any(bytes(x) != STATIC_ID for x in ids):
            problems.append(tr("el /ID del documento no es el valor fijo anónimo", "the document /ID is not the fixed anonymous value"))
        for key in CATALOG_KEYS:
            if key in pdf.Root:
                problems.append(tr("el catálogo conserva {key}", "the catalog keeps {key}", key=key))
        names = pdf.Root.get("/Names")
        if isinstance(names, Dictionary):
            for key in ("/JavaScript", "/EmbeddedFiles", "/Dests"):
                if key in names:
                    problems.append(tr("el árbol /Names conserva {key}", "the /Names tree keeps {key}", key=key))
        action = pdf.Root.get("/OpenAction")
        if isinstance(action, Dictionary) and str(action.get("/S")) not in SAFE_ACTIONS:
            problems.append(tr("el catálogo tiene /OpenAction no interno", "the catalog has a non-internal /OpenAction"))

        field_rx = _template_regex(policy.field_name)
        tree = {p.obj.objgen for p in pdf.pages}
        acro = pdf.Root.get("/AcroForm")
        if isinstance(acro, Dictionary) and "/XFA" in acro:
            problems.append(tr("el formulario conserva XFA", "the form keeps XFA"))
        for obj in pdf.objects:
            if not isinstance(obj, (Dictionary, Stream)):
                continue
            for key in PRIVATE_KEYS:
                if key in obj:
                    problems.append(tr("objeto {obj} conserva {key}", "object {obj} keeps {key}", obj=obj.objgen, key=key))
            if policy.layers == "rename" and obj.get("/Type") == Name.OCG:
                name = str(obj.get("/Name", ""))
                if not layer_rx.fullmatch(name):
                    problems.append(tr("capa con nombre no genérico: {name}", "layer with a non-generic name: {name}", name=repr(name)))
            if obj.get("/Type") == Name.Page and obj.objgen not in tree:
                problems.append(tr("página fantasma {obj} fuera del árbol de páginas", "ghost page {obj} outside the page tree", obj=obj.objgen))
            is_field = "/FT" in obj or obj.get("/Subtype") == Name.Widget
            if is_field and "/T" in obj and not field_rx.fullmatch(str(obj.T)):
                problems.append(tr("campo de formulario con nombre no genérico: {name}", "form field with a non-generic name: {name}", name=repr(str(obj.T))))
            if is_field and any(k in obj for k in ("/TU", "/TM")):
                problems.append(tr("campo de formulario {obj} conserva /TU o /TM", "form field {obj} keeps /TU or /TM", obj=obj.objgen))
            if "/JS" in obj or str(obj.get("/S")) == "/JavaScript":
                problems.append(tr("objeto {obj} contiene JavaScript", "object {obj} contains JavaScript", obj=obj.objgen))

        for number, page in enumerate(pdf.pages, start=1):
            for key in PAGE_KEYS:
                if key in page.obj:
                    problems.append(tr("página {n} conserva {key}", "page {n} keeps {key}", n=number, key=key))
            for annot in page.obj.get("/Annots", Array()):
                problems += _verify_annotation(annot, number, policy)

        props = pdf.Root.get("/OCProperties")
        if isinstance(props, Dictionary) and policy.layers == "rename":
            for cfg in [props.get("/D")] + list(props.get("/Configs", Array())):
                if isinstance(cfg, Dictionary):
                    for label in _order_strings(cfg.get("/Order")):
                        if not group_rx.fullmatch(label):
                            problems.append(tr("grupo de capas con nombre no genérico: {label}", "layer group with a non-generic name: {label}", label=repr(label)))

        if "/Outlines" in pdf.Root:
            if policy.outline == "remove":
                problems.append(tr("quedan marcadores", "bookmarks remain"))
            elif policy.outline == "generic":
                for item in _outline_items(pdf.Root.Outlines):
                    title = str(item.get("/Title", ""))
                    if not any(rx.fullmatch(title) for rx in title_rxs):
                        problems.append(tr("marcador con título no genérico: {title}", "bookmark with a non-generic title: {title}", title=repr(title)))
    return problems


def _verify_annotation(annot: Any, page: int, policy: MetadataPolicy) -> list[str]:
    if not isinstance(annot, Dictionary):
        return []
    subtype = str(annot.get("/Subtype", "/Unknown"))
    where = tr("página {p}: anotación {s}", "page {p}: {s} annotation", p=page, s=subtype.lstrip("/"))
    if subtype in ALWAYS_REMOVED_ANNOTS:
        return [tr("{where} (adjunto o multimedia) no eliminada", "{where} (attachment or multimedia) not removed", where=where)]
    if subtype == "/Link":
        if policy.links == "remove" or policy.annotations == "remove":
            return [tr("{where} no eliminada", "{where} not removed", where=where)]
        if "/A" in annot or "/Dest" not in annot:
            return [tr("{where} con acción no interna", "{where} with a non-internal action", where=where)]
        return []
    if policy.annotations == "remove":
        return [tr("{where} no eliminada", "{where} not removed", where=where)]
    problems = []
    if policy.annotations == "strip_authors":
        keys = WIDGET_KEYS if subtype == "/Widget" else AUTHOR_KEYS
        left = [k for k in keys if k in annot]
        if left:
            problems.append(tr("{where} conserva {keys}", "{where} keeps {keys}", where=where, keys=", ".join(left)))
        if subtype == "/Popup" or "/State" in annot or "/StateModel" in annot:
            problems.append(tr("{where} de revisión no eliminada", "review {where} not removed", where=where))
    if "/AA" in annot:
        problems.append(tr("{where} conserva acciones /AA", "{where} keeps /AA actions", where=where))
    return problems


def _order_strings(order: Any) -> Iterator[str]:
    if isinstance(order, String):
        yield str(order)
    elif isinstance(order, Array):
        for item in order:
            yield from _order_strings(item)


def _template_regex(template: str) -> re.Pattern[str]:
    """Regex que reconoce lo que genera una plantilla como 'Capa-{n:03d}'."""
    parts = re.split(r"\{[^}]*\}", template)
    return re.compile(r"\d+".join(re.escape(p) for p in parts))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="clean-metadata",
        description=tr(
            "Elimina metadatos, adjuntos, JavaScript, autoría de anotaciones, nombres de capas…",
            "Removes metadata, attachments, JavaScript, annotation authorship, layer names…",
        ),
    )
    parser.add_argument("pdf", type=Path, help=tr("PDF de entrada", "input PDF"))
    parser.add_argument("-o", "--output", type=Path, required=True, help=tr("PDF de salida", "output PDF"))
    parser.add_argument("--config", type=Path, help=tr("config YAML de cliente (bloque metadata)", "client YAML config (metadata block)"))
    args = parser.parse_args(argv)

    policy = DEFAULT_POLICY
    if args.config:
        from sanitizer.rules import ConfigError, load_config  # evita import circular

        try:
            policy = load_config(args.config).metadata
        except ConfigError as exc:
            parser.error(tr("config inválida: {exc}", "invalid config: {exc}", exc=exc))

    try:
        report = clean_metadata(args.pdf, args.output, policy)
    except (ValueError, pikepdf.PdfError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    removed = ", ".join(f"{k} ×{v}" for k, v in report.annotations_removed.items()) or tr("ninguna", "none")
    print(f"{args.pdf} → {args.output}")
    replaced = tr("sí", "yes") if report.document_id_replaced else "no"
    print(tr("  Info: {k} claves · XMP: {x} · /ID sustituido: {r}", "  Info: {k} keys · XMP: {x} · /ID replaced: {r}", k=len(report.info_keys), x=report.xmp_streams, r=replaced))
    print(
        tr(
            "  datos privados (PieceInfo, OPI…): {p} · catálogo: {c}",
            "  private data (PieceInfo, OPI…): {p} · catalog: {c}",
            p=report.private_keys, c=", ".join(report.catalog_keys) or "-",
        )
    )
    print(
        tr(
            "  adjuntos: {e} · JavaScript: {j} · acciones: {a}",
            "  attachments: {e} · JavaScript: {j} · actions: {a}",
            e=report.embedded_files, j=report.javascript, a=report.actions,
        )
    )
    print(tr("  anotaciones eliminadas: {r} · sin autoría: {s}", "  annotations removed: {r} · authorship stripped: {s}", r=removed, s=report.annotations_stripped))
    print(
        tr(
            "  capas renombradas: {l} · campos de formulario: {f} · marcadores: {o}",
            "  layers renamed: {l} · form fields: {f} · bookmarks: {o}",
            l=report.layers_renamed, f=report.fields_renamed, o=report.outline_items,
        )
    )

    problems = verify_metadata(args.output, policy)
    if problems:
        print(tr("VERIFICACIÓN FALLIDA:", "VERIFICATION FAILED:"), file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print(tr("  verificación (relectura con pikepdf): OK", "  verification (re-read with pikepdf): OK"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
