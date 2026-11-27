"""Tests de la fase 3: limpieza de metadatos con un PDF sintético "sucio".

El PDF de prueba reúne lo encontrado en P&ID reales (docs/corpus-pid.md) y
algún caso más: todo valor sensible lleva la marca SECRET para poder buscarlo
después en cualquier objeto del PDF limpio, streams descomprimidos incluidos.
"""

from pathlib import Path

import pikepdf
import pymupdf
import pytest
from pikepdf import Array, Dictionary, Name, String
from PIL import Image
from reportlab.lib.pagesizes import A3, landscape
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from sanitizer.metadata import (
    STATIC_ID,
    MetadataPolicy,
    clean_metadata,
    main,
    verify_metadata,
)
from sanitizer.rules import ConfigError, load_config

PAGE_TEXT = ["P-101A FIC-1203 6\"-PL-1001-A1A", "P-102 HH ALARM"]
KEPT_CONTENTS = ["Check valve FC", "Agreed, close it", "OPTIONAL"]


def _base(path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=landscape(A3))
    c.drawString(100, 700, PAGE_TEXT[0])
    c.drawImage(ImageReader(Image.new("RGB", (20, 10), (0, 90, 200))), 1000, 50, 80, 40)
    c.showPage()
    c.drawString(100, 700, PAGE_TEXT[1])
    c.showPage()
    c.save()


def _annot(pdf, **kw):
    return pdf.make_indirect(Dictionary(Type=Name.Annot, Rect=Array([40, 40, 60, 60]), **kw))


def _make_dirty(tmp_path: Path) -> Path:
    base = tmp_path / "base.pdf"
    _base(base)
    full = tmp_path / "dirty_full.pdf"

    with pikepdf.open(base) as pdf:
        p0, p1 = pdf.pages[0].obj, pdf.pages[1].obj
        js = lambda code: Dictionary(S=Name.JavaScript, JS=String(code))  # noqa: E731

        # Metadatos del documento, incluido un /Info duplicado en el catálogo
        pdf.trailer.Info = pdf.make_indirect(
            Dictionary(
                Author=String("Jane Engineer SECRET"),
                Title=String("ACME plant SECRET"),
                Client=String("Globex SECRET"),
            )
        )
        pdf.Root.Info = Dictionary(Producer=String("SecretCAD SECRET"))
        pdf.Root.Metadata = pdf.make_stream(
            b"<x:xmpmeta>ACME-XMP-SECRET</x:xmpmeta>", Type=Name.Metadata, Subtype=Name.XML
        )

        # Imagen con XMP propio y referencia OPI a una ruta de red
        img = next(iter(p0.Resources.XObject.values()))
        img.Metadata = pdf.make_stream(b"IMG-XMP-SECRET")
        img.OPI = Dictionary(F=String("C:\\cad\\OPI-SECRET.tif"))

        # Datos privados de aplicación, miniatura y acciones de página
        p0.PieceInfo = Dictionary(AutoCAD=Dictionary(Private=String("CAD-PIECE-SECRET")))
        p0.LastModified = String("D:20200101")
        p0.Thumb = pdf.make_stream(b"\x00" * 16, Width=4, Height=4, BitsPerComponent=8)
        p0.AA = Dictionary(O=js("PAGE-JS-SECRET"))
        pdf.Root.OpenAction = pdf.make_indirect(js("OPEN-JS-SECRET"))
        pdf.Root.AA = Dictionary(WC=js("CLOSE-JS-SECRET"))

        # Árbol /Names: JavaScript, adjunto y destino con nombre
        attachment = Dictionary(
            Type=Name.Filespec,
            F=String("secret.txt"),
            EF=Dictionary(F=pdf.make_stream(b"EMBEDDED-SECRET", Type=Name.EmbeddedFile)),
        )
        pdf.Root.Names = Dictionary(
            JavaScript=Dictionary(Names=Array([String("doc-js"), js("NAMES-JS-SECRET")])),
            EmbeddedFiles=Dictionary(Names=Array([String("secret.txt"), attachment])),
            Dests=Dictionary(Names=Array([String("DEST-NAME-SECRET"), Array([p1, Name.Fit])])),
        )
        pdf.Root.PageLabels = Dictionary(Nums=Array([0, Dictionary(P=String("LABEL-SECRET-"))]))
        pdf.Root.StructTreeRoot = pdf.make_indirect(
            Dictionary(Type=Name.StructTreeRoot, K=Dictionary(S=Name.Figure, Alt=String("ALT-SECRET")))
        )
        pdf.Root.MarkInfo = Dictionary(Marked=True)

        # Anotaciones de revisión
        text = _annot(
            pdf,
            Subtype=Name.Text,
            Contents=String(KEPT_CONTENTS[0]),
            T=String("J. Reviewer SECRET"),
            M=String("D:20200616"),
            NM=String("uuid-SECRET"),
        )
        popup = _annot(pdf, Subtype=Name.Popup, Parent=text)
        text.Popup = popup
        reply = _annot(
            pdf, Subtype=Name.Text, IRT=text, Contents=String(KEPT_CONTENTS[1]), T=String("REPLY-SECRET")
        )
        state = _annot(
            pdf,
            Subtype=Name.Text,
            IRT=text,
            State=String("Accepted"),
            StateModel=String("Review"),
            T=String("STATE-AUTHOR-SECRET"),
            Contents=String("Accepted set by STATE-AUTHOR-SECRET"),
        )
        freetext = _annot(
            pdf,
            Subtype=Name.FreeText,
            Contents=String(KEPT_CONTENTS[2]),
            DA=String("/Helv 10 Tf 0 g"),
            RC=String("<body>OPTIONAL RC-SECRET</body>"),
            T=String("FT-AUTHOR-SECRET"),
        )
        file_annot = _annot(
            pdf,
            Subtype=Name.FileAttachment,
            FS=Dictionary(
                Type=Name.Filespec,
                F=String("a.txt"),
                EF=Dictionary(F=pdf.make_stream(b"ATTACH-SECRET", Type=Name.EmbeddedFile)),
            ),
        )
        link_uri = _annot(
            pdf, Subtype=Name.Link, A=Dictionary(S=Name.URI, URI=String("http://intranet/URI-SECRET"))
        )
        link_internal = _annot(pdf, Subtype=Name.Link, Dest=String("DEST-NAME-SECRET"))
        widget = _annot(
            pdf,
            Subtype=Name.Widget,
            FT=Name.Tx,
            T=String("FIELD-NAME-SECRET"),
            TU=String("TOOLTIP-SECRET"),
            V=String("4711"),
            DA=String("/Helv 10 Tf 0 g"),
            P=p0,
        )
        p0.Annots = Array(
            [text, popup, reply, state, freetext, file_annot, link_uri, link_internal, widget]
        )
        pdf.Root.AcroForm = Dictionary(
            Fields=Array([widget]),
            XFA=pdf.make_stream(b"<xdp>XFA-SECRET</xdp>"),
            DA=String("/Helv 0 Tf 0 g"),
        )

        # Capas: dos registradas (una oculta), una sin registrar y un grupo con nombre
        ocg1 = pdf.make_indirect(
            Dictionary(
                Type=Name.OCG,
                Name=String("ACME-TITLEBLOCK-SECRET"),
                Usage=Dictionary(CreatorInfo=Dictionary(Creator=String("CREATORINFO-SECRET"))),
            )
        )
        ocg2 = pdf.make_indirect(Dictionary(Type=Name.OCG, Name=String("99999|LAYER-SECRET")))
        orphan = pdf.make_indirect(Dictionary(Type=Name.OCG, Name=String("ORPHAN-LAYER-SECRET")))
        p0.Resources.Properties = Dictionary(MC9=orphan)
        pdf.Root.OCProperties = Dictionary(
            OCGs=Array([ocg1, ocg2]),
            D=Dictionary(
                Name=String("CONFIG-SECRET"),
                Creator=String("CONFIG-CREATOR-SECRET"),
                OFF=Array([ocg2]),
                Order=Array([ocg1, Array([String("GROUP-SECRET"), ocg2])]),
            ),
        )

        # Marcadores: destino explícito, destino con nombre y acción JavaScript
        root = pdf.make_indirect(Dictionary(Type=Name.Outlines))
        it1 = pdf.make_indirect(
            Dictionary(Title=String("ACME folder 2017 SECRET"), Parent=root, Dest=Array([p0, Name.Fit]))
        )
        it2 = pdf.make_indirect(
            Dictionary(
                Title=String("OUTLINE-SECRET rev 05"),
                Parent=it1,
                A=Dictionary(S=Name.GoTo, D=String("DEST-NAME-SECRET")),
            )
        )
        it3 = pdf.make_indirect(
            Dictionary(Title=String("JS-OUTLINE-SECRET"), Parent=root, A=js("OUTLINE-JS-SECRET"))
        )
        # Página "fantasma": fuera del árbol de páginas, solo enlazada desde un marcador
        ghost = pdf.make_indirect(
            Dictionary(
                Type=Name.Page,
                Parent=pdf.Root.Pages,
                MediaBox=Array([0, 0, 200, 200]),
                Contents=pdf.make_stream(b"BT /F1 12 Tf 10 10 Td (GHOST-PAGE-SECRET) Tj ET"),
            )
        )
        it4 = pdf.make_indirect(
            Dictionary(Title=String("GHOST-OUTLINE-SECRET"), Parent=root, Dest=Array([ghost, Name.Fit]))
        )
        it1.First = it1.Last = it2
        it1.Count = 1
        it1.Next, it3.Prev = it3, it1
        it3.Next, it4.Prev = it4, it3
        root.First, root.Last, root.Count = it1, it4, 4
        pdf.Root.Outlines = root
        pdf.save(full, fix_metadata_version=False)

    # Revisión incremental: una anotación añadida y luego borrada sigue en el fichero
    doc = pymupdf.open(full)
    doc[1].add_text_annot((80, 80), "OLD-REVISION-SECRET")
    doc.saveIncr()
    doc.close()
    doc = pymupdf.open(full)
    doc[1].delete_annot(doc[1].first_annot)
    doc.saveIncr()
    doc.close()
    assert b"OLD-REVISION-SECRET" in full.read_bytes()
    return full


@pytest.fixture
def dirty(tmp_path: Path) -> Path:
    return _make_dirty(tmp_path)


def _objects_blob(path: Path) -> bytes:
    """Todos los objetos del PDF, con los streams descomprimidos."""
    chunks = []
    with pikepdf.open(path) as pdf:
        chunks.append(pdf.trailer.unparse())
        for obj in pdf.objects:
            if isinstance(obj, pikepdf.Stream):
                chunks.append(obj.stream_dict.unparse())
                try:
                    chunks.append(obj.read_bytes())
                except pikepdf.PdfError:
                    chunks.append(obj.read_raw_bytes())
            else:
                try:
                    chunks.append(obj.unparse())
                except Exception:  # objetos nulos o huérfanos
                    pass
    return b"\n".join(chunks)


def _clean(dirty: Path, tmp_path: Path, **policy) -> Path:
    out = tmp_path / "clean.pdf"
    clean_metadata(dirty, out, MetadataPolicy(**policy))
    return out


# ---------------------------------------------------------------------------
# Política por defecto
# ---------------------------------------------------------------------------


def test_no_secret_survives_anywhere(dirty, tmp_path):
    out = _clean(dirty, tmp_path)
    assert b"SECRET" not in _objects_blob(out)
    assert b"SECRET" not in out.read_bytes()
    assert b"Jane Engineer" not in _objects_blob(out)


def test_dirty_file_fails_verification(dirty):
    problems = verify_metadata(dirty)
    for fragment in ("/Info", "/ID", "/PieceInfo", "JavaScript", "/EmbeddedFiles", "capa", "marcador"):
        assert any(fragment in p for p in problems), fragment


def test_clean_file_passes_verification(dirty, tmp_path):
    assert verify_metadata(_clean(dirty, tmp_path)) == []


def test_document_id_is_static(dirty, tmp_path):
    with pikepdf.open(_clean(dirty, tmp_path)) as pdf:
        assert [bytes(x) for x in pdf.trailer.ID] == [STATIC_ID, STATIC_ID]
        assert "/Info" not in pdf.trailer


def test_output_is_deterministic(dirty, tmp_path):
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    clean_metadata(dirty, a)
    clean_metadata(dirty, b)
    assert a.read_bytes() == b.read_bytes()


def test_page_content_is_untouched(dirty, tmp_path):
    out = _clean(dirty, tmp_path)
    with pymupdf.open(dirty) as before, pymupdf.open(out) as after:
        assert after.page_count == before.page_count
        for i in range(before.page_count):
            # La primera línea es el contenido de página; el resto, apariencias
            # de anotaciones que MuPDF genera al vuelo
            assert after[i].get_text().splitlines()[0] == PAGE_TEXT[i]
            assert before[i].get_text().splitlines()[0] == PAGE_TEXT[i]
            # Solo el contenido de página (sin anotaciones): idéntico píxel a píxel
            a = before[i].get_pixmap(dpi=30, annots=False)
            b = after[i].get_pixmap(dpi=30, annots=False)
            assert a.samples == b.samples


def test_annotations_keep_content_without_authorship(dirty, tmp_path):
    with pikepdf.open(_clean(dirty, tmp_path)) as pdf:
        annots = list(pdf.pages[0].obj.Annots)
        subtypes = [str(a.Subtype) for a in annots]
        contents = [str(a.get("/Contents", "")) for a in annots]
    assert subtypes == ["/Text", "/Text", "/FreeText", "/Widget"]  # sin popup, estado, adjunto ni enlaces
    for kept in KEPT_CONTENTS:
        assert kept in contents


def test_forms_are_kept_with_generic_names(dirty, tmp_path):
    with pikepdf.open(_clean(dirty, tmp_path)) as pdf:
        widget = pdf.pages[0].obj.Annots[-1]
        assert str(widget.T) == "Campo-001"
        assert str(widget.V) == "4711"  # el valor visible se conserva (fase 4)
        assert "/TU" not in widget
        assert "/XFA" not in pdf.Root.AcroForm


def test_layers_renamed_and_visibility_kept(dirty, tmp_path):
    with pikepdf.open(_clean(dirty, tmp_path)) as pdf:
        props = pdf.Root.OCProperties
        names = [str(o.Name) for o in props.OCGs]
        assert names == ["Capa-001", "Capa-002"]
        assert [str(o.Name) for o in props.D.OFF] == ["Capa-002"]
        assert str(props.D.Order[1][0]) == "Grupo-001"
        orphan = pdf.pages[0].obj.Resources.Properties.MC9
        assert str(orphan.Name) == "Capa-003"
        assert "/CreatorInfo" not in props.OCGs[0].Usage


def test_outline_structure_kept_with_generic_titles(dirty, tmp_path):
    with pikepdf.open(_clean(dirty, tmp_path)) as pdf:
        first = pdf.Root.Outlines.First
        assert str(first.Title) == "Página 1"
        assert str(first.First.Title) == "Página 2"  # destino con nombre resuelto
        assert isinstance(first.First.Dest, Array)
        assert str(first.Next.Title) == "Marcador 3"  # era una acción JavaScript
        assert "/A" not in first.Next
        ghost_item = first.Next.Next
        assert str(ghost_item.Title) == "Marcador 4"
        assert "/Dest" not in ghost_item  # apuntaba a una página fantasma


def test_ghost_pages_are_not_written(dirty, tmp_path):
    assert any("página fantasma" in p for p in verify_metadata(dirty))
    with pikepdf.open(_clean(dirty, tmp_path)) as pdf:
        tree = {p.obj.objgen for p in pdf.pages}
        pages = [o for o in pdf.objects if isinstance(o, Dictionary) and o.get("/Type") == Name.Page]
        assert {p.objgen for p in pages} == tree


# ---------------------------------------------------------------------------
# Otras políticas
# ---------------------------------------------------------------------------


def test_remove_all_annotations(dirty, tmp_path):
    out = _clean(dirty, tmp_path, annotations="remove")
    with pikepdf.open(out) as pdf:
        assert "/Annots" not in pdf.pages[0].obj
        assert "/AcroForm" not in pdf.Root
    assert verify_metadata(out, MetadataPolicy(annotations="remove")) == []


def test_keep_internal_links(dirty, tmp_path):
    out = _clean(dirty, tmp_path, links="keep_internal")
    with pikepdf.open(out) as pdf:
        links = [a for a in pdf.pages[0].obj.Annots if a.Subtype == Name.Link]
        assert len(links) == 1  # el URI se va, el interno se queda
        assert isinstance(links[0].Dest, Array)
    assert b"SECRET" not in _objects_blob(out)


def test_remove_outline(dirty, tmp_path):
    out = _clean(dirty, tmp_path, outline="remove")
    with pikepdf.open(out) as pdf:
        assert "/Outlines" not in pdf.Root


def test_keep_layers_is_reported_by_nothing_else(dirty, tmp_path):
    policy = MetadataPolicy(layers="keep")
    out = tmp_path / "keep.pdf"
    clean_metadata(dirty, out, policy)
    with pikepdf.open(out) as pdf:
        assert str(pdf.Root.OCProperties.OCGs[0].Name) == "ACME-TITLEBLOCK-SECRET"
    assert verify_metadata(out, policy) == []


def test_verification_catches_leftovers(dirty, tmp_path):
    out = _clean(dirty, tmp_path)
    with pikepdf.open(out, allow_overwriting_input=True) as pdf:
        pdf.pages[0].obj.PieceInfo = Dictionary(X=String("y"))
        pdf.Root.OCProperties.OCGs[0].Name = String("Real layer name")
        pdf.save(out)
    problems = verify_metadata(out)
    assert any("/PieceInfo" in p for p in problems)
    assert any("Real layer name" in p for p in problems)


def test_refuses_to_overwrite_source(dirty):
    with pytest.raises(ValueError, match="sobrescribir"):
        clean_metadata(dirty, dirty)


@pytest.mark.parametrize("bad", [{"layer_name": "Capa-{x}"}, {"outline_title": "P {page"}])
def test_invalid_templates(bad):
    with pytest.raises(ValueError, match="mal formado"):
        MetadataPolicy(**bad)


# ---------------------------------------------------------------------------
# Config y CLI
# ---------------------------------------------------------------------------


def test_policy_from_config(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("version: 1\nname: X\nmetadata:\n  annotations: remove\n  outline: keep\n")
    policy = load_config(cfg).metadata
    assert policy.annotations == "remove" and policy.outline == "keep"
    assert policy.layers == "rename"  # valor por defecto

    cfg.write_text("version: 1\nname: X\nmetadata:\n  annotations: delete\n")
    with pytest.raises(ConfigError, match="annotations"):
        load_config(cfg)


def test_cli(dirty, tmp_path, capsys):
    out = tmp_path / "cli.pdf"
    assert main([str(dirty), "-o", str(out)]) == 0
    assert "verificación (relectura con pikepdf): OK" in capsys.readouterr().out
    assert main([str(dirty), "-o", str(dirty)]) == 1
