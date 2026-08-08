"""Document fixtures, built in code rather than committed as binaries (§15.9).

A repository that carries a .docx, a .xlsx and a .pptx as opaque blobs cannot be
reviewed: nobody can tell from a diff what changed inside one, and the zip bomb
in particular would be a 500 KB binary nobody wants to click on. Everything here
is assembled from the few parts each format actually requires, so what a test
feeds the parser is readable in the same file that asserts on the result.

Not a test module — pytest collects ``test_*.py`` — so it holds no assertions.
"""

from __future__ import annotations

import io
import zipfile

_PKG_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
_OFFICE_RELS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_CONTENT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"

# The table every fixture carries, so one assertion shape works across formats.
TABLE_ROWS = [("Region", "T1", "T2"), ("EMEA", "1200", "1450"), ("APAC", "890", "1010")]


def _zip(parts: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in parts.items():
            archive.writestr(name, body)
    return buffer.getvalue()


def _types(*overrides: tuple[str, str]) -> str:
    declared = "".join(
        f'<Override PartName="{part}" ContentType="{content_type}"/>'
        for part, content_type in overrides
    )
    return (
        f'<?xml version="1.0"?><Types xmlns="{_CONTENT_TYPES}">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f"{declared}</Types>"
    )


def _root_rels(target: str) -> str:
    return (
        f'<?xml version="1.0"?><Relationships xmlns="{_PKG_RELS}">'
        f'<Relationship Id="rId1" Type="{_OFFICE_RELS}/officeDocument" Target="{target}"/>'
        "</Relationships>"
    )


def make_docx(heading: str = "Rapport trimestriel") -> bytes:
    """A Word document: a heading, a paragraph, and `TABLE_ROWS` as a real table."""
    cells = "".join(
        "<w:tr>"
        + "".join(f"<w:tc><w:p><w:r><w:t>{value}</w:t></w:r></w:p></w:tc>" for value in row)
        + "</w:tr>"
        for row in TABLE_ROWS
    )
    return _zip(
        {
            "[Content_Types].xml": _types(
                (
                    "/word/document.xml",
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document.main+xml",
                ),
                (
                    "/word/styles.xml",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml",
                ),
            ),
            "_rels/.rels": _root_rels("word/document.xml"),
            # Without a styles part the heading is just a paragraph with a style
            # name nothing defines, and comes out as body text.
            "word/_rels/document.xml.rels": (
                f'<?xml version="1.0"?><Relationships xmlns="{_PKG_RELS}">'
                f'<Relationship Id="rId1" Type="{_OFFICE_RELS}/styles" Target="styles.xml"/>'
                "</Relationships>"
            ),
            "word/styles.xml": (
                f'<?xml version="1.0"?><w:styles xmlns:w="{_W}">'
                '<w:style w:type="paragraph" w:styleId="Heading1">'
                '<w:name w:val="heading 1"/></w:style></w:styles>'
            ),
            "word/document.xml": (
                f'<?xml version="1.0"?><w:document xmlns:w="{_W}"><w:body>'
                '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
                f"<w:r><w:t>{heading}</w:t></w:r></w:p>"
                "<w:p><w:r><w:t>Le chiffre d affaires progresse.</w:t></w:r></w:p>"
                f"<w:tbl>{cells}</w:tbl>"
                "</w:body></w:document>"
            ),
        }
    )


def make_xlsx() -> bytes:
    """A spreadsheet whose single sheet holds `TABLE_ROWS`."""
    rows = "".join(
        f'<row r="{number}">'
        + "".join(
            f'<c r="{chr(65 + column)}{number}" t="inlineStr"><is><t>{value}</t></is></c>'
            for column, value in enumerate(row)
        )
        + "</row>"
        for number, row in enumerate(TABLE_ROWS, start=1)
    )
    return _zip(
        {
            "[Content_Types].xml": _types(
                (
                    "/xl/workbook.xml",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
                ),
                (
                    "/xl/worksheets/sheet1.xml",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
                ),
            ),
            "_rels/.rels": _root_rels("xl/workbook.xml"),
            "xl/workbook.xml": (
                f'<?xml version="1.0"?><workbook xmlns="{_S}" xmlns:r="{_OFFICE_RELS}">'
                '<sheets><sheet name="Resultats" sheetId="1" r:id="rId1"/></sheets></workbook>'
            ),
            "xl/_rels/workbook.xml.rels": (
                f'<?xml version="1.0"?><Relationships xmlns="{_PKG_RELS}">'
                f'<Relationship Id="rId1" Type="{_OFFICE_RELS}/worksheet" '
                'Target="worksheets/sheet1.xml"/></Relationships>'
            ),
            "xl/worksheets/sheet1.xml": (
                f'<?xml version="1.0"?><worksheet xmlns="{_S}"><sheetData>{rows}</sheetData>'
                "</worksheet>"
            ),
        }
    )


def make_pptx(text: str = "Feuille de route") -> bytes:
    """A one-slide presentation carrying ``text``."""
    return _zip(
        {
            "[Content_Types].xml": _types(
                (
                    "/ppt/presentation.xml",
                    "application/vnd.openxmlformats-officedocument."
                    "presentationml.presentation.main+xml",
                ),
                (
                    "/ppt/slides/slide1.xml",
                    "application/vnd.openxmlformats-officedocument.presentationml.slide+xml",
                ),
            ),
            "_rels/.rels": _root_rels("ppt/presentation.xml"),
            "ppt/presentation.xml": (
                f'<?xml version="1.0"?><p:presentation xmlns:p="{_P}" xmlns:r="{_OFFICE_RELS}">'
                '<p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst></p:presentation>'
            ),
            "ppt/_rels/presentation.xml.rels": (
                f'<?xml version="1.0"?><Relationships xmlns="{_PKG_RELS}">'
                f'<Relationship Id="rId1" Type="{_OFFICE_RELS}/slide" '
                'Target="slides/slide1.xml"/></Relationships>'
            ),
            "ppt/slides/slide1.xml": (
                f'<?xml version="1.0"?><p:sld xmlns:p="{_P}" xmlns:a="{_A}"><p:cSld><p:spTree>'
                f"<p:sp><p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp>"
                "</p:spTree></p:cSld></p:sld>"
            ),
        }
    )


def make_csv() -> bytes:
    return ("\n".join(",".join(row) for row in TABLE_ROWS) + "\n").encode()


def make_zip_bomb(inflated_mb: int = 520) -> bytes:
    """A structurally valid .xlsx of ~500 KB that inflates to ``inflated_mb`` (§15.3).

    The obvious attack on a service that downloads arbitrary ZIPs — a .xlsx, a
    .docx and a .pptx all are one. ``CRAWL_DOC_MAX_MB`` caps the *download*, so
    the second ceiling has to come from the parser.

    The payload is streamed into the archive rather than built as one string:
    the point is to hand half a gigabyte to anydoc, not to allocate it here.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr(
            "[Content_Types].xml",
            _types(
                (
                    "/xl/workbook.xml",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
                ),
                (
                    "/xl/worksheets/sheet1.xml",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
                ),
            ),
        )
        archive.writestr("_rels/.rels", _root_rels("xl/workbook.xml"))
        archive.writestr(
            "xl/workbook.xml",
            f'<?xml version="1.0"?><workbook xmlns="{_S}" xmlns:r="{_OFFICE_RELS}">'
            '<sheets><sheet name="S" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            f'<?xml version="1.0"?><Relationships xmlns="{_PKG_RELS}">'
            f'<Relationship Id="rId1" Type="{_OFFICE_RELS}/worksheet" '
            'Target="worksheets/sheet1.xml"/></Relationships>',
        )
        with archive.open("xl/worksheets/sheet1.xml", "w") as part:
            part.write(f'<?xml version="1.0"?><worksheet xmlns="{_S}"><sheetData>'.encode())
            part.write(b'<row r="1"><c r="A1" t="inlineStr"><is><t>Region</t></is></c></row>')
            # XML comments: highly compressible, and they leave the sheet valid,
            # so the parser has to actually inflate them to find that out.
            padding = b"<!--" + b"X" * 65_000 + b"-->"
            for _ in range(inflated_mb * 1_000_000 // len(padding)):
                part.write(padding)
            part.write(b"</sheetData></worksheet>")
    return buffer.getvalue()


def make_pdf(*, with_table: bool = True, heading: str = "Rapport trimestriel") -> bytes:
    """A single-page PDF, optionally with `TABLE_ROWS` drawn as a ruled table.

    Hand-assembled rather than produced by a library so the fixture states
    exactly what is on the page: the text, its coordinates, and the rules between
    the cells. That is the whole input to the §15.1 regression — whether a reader
    recovers ``EMEA | 1200 | 1450`` as a row or as three unrelated values.
    """
    content = bytearray()
    content += _pdf_text(40, 260, heading, size=16)
    content += _pdf_text(40, 235, "Le chiffre d affaires progresse de 12 pourcent.")
    if with_table:
        columns = (40, 160, 260)
        rows = (200, 175, 150)
        for baseline, row in zip(rows, TABLE_ROWS, strict=True):
            for left, value in zip(columns, row, strict=True):
                content += _pdf_text(left + 4, baseline + 6, value)
        content += b"0.5 w\n"
        right = columns[-1] + 90
        for y in (rows[0] + 22, rows[0] - 3, rows[1] - 3, rows[2] - 3):
            content += f"{columns[0]} {y} m {right} {y} l S\n".encode()
        for x in (*columns, right):
            content += f"{x} {rows[-1] - 3} m {x} {rows[0] + 22} l S\n".encode()
    return _pdf_document(bytes(content))


def _pdf_text(x: int, y: int, value: str, *, size: int = 11) -> bytes:
    return f"BT /F1 {size} Tf {x} {y} Td ({value}) Tj ET\n".encode()


def _pdf_document(content: bytes) -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 300] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n".encode()
    return bytes(out + b"%%EOF\n")


def make_encrypted_pdf(password: str = "secret") -> bytes:
    """A password-protected PDF, for the `EncryptedError` path."""
    from pypdf import PdfWriter

    writer = PdfWriter(clone_from=io.BytesIO(make_pdf()))
    writer.encrypt(password)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
