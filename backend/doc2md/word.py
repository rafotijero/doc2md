"""DOCX -> Markdown con python-docx.

Solo se extrae el texto (párrafos, títulos, listas y tablas); las imágenes
embebidas se ignoran por completo, no se aplica OCR.
"""

from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph


def _heading_level(style_name: str) -> int | None:
    name = style_name.lower()
    if name == "title":
        return 1
    if name.startswith("heading"):
        digits = "".join(c for c in name if c.isdigit())
        if digits:
            return min(int(digits), 6)
        return 1
    return None


def _paragraph_to_markdown(paragraph: Paragraph) -> str:
    text = "".join(
        f"**{run.text}**" if run.bold and run.text.strip() else
        f"*{run.text}*" if run.italic and run.text.strip() else
        run.text
        for run in paragraph.runs
    ) or paragraph.text

    if not text.strip():
        return ""

    level = _heading_level(paragraph.style.name if paragraph.style else "")
    if level:
        return f"{'#' * level} {text.strip()}"

    style_name = (paragraph.style.name if paragraph.style else "").lower()
    if "list bullet" in style_name:
        return f"- {text.strip()}"
    if "list number" in style_name:
        return f"1. {text.strip()}"

    return text


def _cell_text(cell) -> str:
    return " ".join(p.text.strip() for p in cell.paragraphs if p.text.strip()) or " "


def _table_to_markdown(table: Table) -> str:
    rows = [[_cell_text(cell) for cell in row.cells] for row in table.rows]
    if not rows:
        return ""
    header, *body = rows
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row in body:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def docx_to_markdown(path: Path) -> str:
    document = Document(str(path))
    blocks: list[str] = []
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            md = _paragraph_to_markdown(Paragraph(child, document))
            if md:
                blocks.append(md)
        elif child.tag == qn("w:tbl"):
            md = _table_to_markdown(Table(child, document))
            if md:
                blocks.append(md)
    return "\n\n".join(blocks) + "\n"
