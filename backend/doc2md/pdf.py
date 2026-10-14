"""PDF -> Markdown con pdf_inspector, OCR opcional y unión de tablas."""

import re
from dataclasses import dataclass
from pathlib import Path

import pdf_inspector

# Pie de página: línea de texto (no fila de tabla) que termina en "Página N de M".
_PAGE_FOOTER = re.compile(r"^(?!\|).*\bP[áa]g(?:ina|\.)?\s*\d+\s+de\s+\d+\W*$", re.IGNORECASE)
_TABLE_SEPARATOR = re.compile(r"^\|(\s*:?-+:?\s*\|)+$")


@dataclass
class PdfConversion:
    markdown: str
    pdf_type: str
    page_count: int
    ocr_pages: int  # páginas reconocidas con OCR
    ocr_skipped: int  # páginas escaneadas que superaban el límite de OCR


def merge_tables(markdown: str) -> str:
    """Quita los pies de página y une las tablas partidas por salto de página.

    Una tabla continúa la anterior si repite exactamente su cabecera y entre
    ambas solo hay líneas en blanco (una vez quitados los pies de página).
    """
    lines = [line for line in markdown.splitlines() if not _PAGE_FOOTER.match(line)]
    out: list[str] = []
    blanks: list[str] = []
    header = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            blanks.append(line)
            i += 1
            continue
        is_header = (
            line.startswith("|")
            and i + 1 < len(lines)
            and _TABLE_SEPARATOR.match(lines[i + 1]) is not None
        )
        if is_header and line == header and out and out[-1].startswith("|"):
            blanks = []
            i += 2  # se descartan la cabecera repetida y su separador
            continue
        out += blanks
        blanks = []
        if is_header:
            header = line
        elif not line.startswith("|"):
            header = None
        out.append(line)
        i += 1
    return "\n".join(out) + "\n"


def pdf_to_markdown(
    pdf_path: Path,
    ocr: bool = False,
    merge: bool = False,
    max_ocr_pages: int | None = None,
) -> PdfConversion:
    """Convierte un PDF a Markdown.

    Con ocr=True las páginas escaneadas se reconocen con Tesseract (solo las
    primeras max_ocr_pages, si se indica); sin él quedan vacías.
    """
    result = pdf_inspector.process_pdf(str(pdf_path))
    markdown = result.markdown or ""
    ocr_pages: list[int] = []
    skipped = 0
    if ocr:
        ocr_pages = list(pdf_inspector.classify_pdf(str(pdf_path)).pages_needing_ocr)
        if max_ocr_pages is not None and len(ocr_pages) > max_ocr_pages:
            skipped = len(ocr_pages) - max_ocr_pages
            ocr_pages = ocr_pages[:max_ocr_pages]
        if ocr_pages:
            from . import ocr as ocr_module  # solo se carga si hace falta (requiere Tesseract)

            markdown = ocr_module.convert_with_ocr(pdf_path, ocr_pages)
    if merge:
        markdown = merge_tables(markdown)
    return PdfConversion(markdown, result.pdf_type, result.page_count, len(ocr_pages), skipped)
