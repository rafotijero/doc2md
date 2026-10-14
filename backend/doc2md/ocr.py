"""OCR de páginas escaneadas de un PDF a Markdown (Tesseract + OpenCV).

pdf_inspector solo extrae texto embebido; las páginas escaneadas salen vacías.
Este módulo las renderiza, corrige su orientación y reconoce el texto:

- Páginas con una tabla que ocupa casi toda la hoja (p. ej. los consolidados
  de posiciones): se detecta la cuadrícula y se reconoce cada celda por
  separado -> tabla Markdown.
- Resto de páginas (formularios de perfil): texto por líneas visuales.

Requisitos: Tesseract (ruta en TESSERACT_CMD, en el PATH o en
"C:\\Program Files\\Tesseract-OCR") con los idiomas "spa" y "osd". Si no
están en el tessdata de la instalación, apunta TESSDATA_DIR a una carpeta
que contenga spa.traineddata y osd.traineddata.
"""

import csv
import io
import os
import re
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
import pypdfium2 as pdfium

DPI = 300
LANG = "spa"
MIN_CONF = 30  # confianza mínima (0-100) para aceptar una palabra
MIN_OSD_CONF = 2.0  # confianza mínima para aceptar una rotación de OSD
# Dos líneas son del mismo párrafo si su separación es <= esto x su altura de texto.
_PARAGRAPH_PITCH = 2.0
# Carpeta con spa/osd.traineddata; sin definir, Tesseract usa la suya.
TESSDATA_DIR = os.environ.get("TESSDATA_DIR")

# Tablas: una banda horizontal más gruesa que esto es un encabezado relleno.
_FILL_MIN_PX = int(DPI * 0.07)
_TABLE_MIN_PAGE_FRACTION = 0.4
_TABLE_MIN_ROWS = 5
_TABLE_MIN_COLS = 4
_TABLE_FILLED_MIN_COLS = 6

# Limita los Tesseract simultáneos entre todos los hilos del proceso.
_MAX_TESSERACT = max(1, (os.cpu_count() or 4) - 2)
_tesseract_slots = threading.BoundedSemaphore(_MAX_TESSERACT)

_ROTATIONS = {
    90: cv2.ROTATE_90_CLOCKWISE,
    180: cv2.ROTATE_180,
    270: cv2.ROTATE_90_COUNTERCLOCKWISE,
}


def _find_tesseract() -> str:
    candidates = [
        os.environ.get("TESSERACT_CMD"),
        shutil.which("tesseract"),
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    ]
    for cand in candidates:
        if cand and Path(cand).exists():
            return cand
    raise RuntimeError(
        "No se encontró Tesseract. Instálalo o define la variable TESSERACT_CMD."
    )


TESSERACT = _find_tesseract()
_ENV = {**os.environ, "OMP_THREAD_LIMIT": "1"}
if TESSDATA_DIR:
    _ENV["TESSDATA_PREFIX"] = TESSDATA_DIR


def _tesseract(
    gray: np.ndarray,
    psm: int,
    output: str | None,
    lang: str = LANG,
    extra: list[str] | None = None,
) -> str:
    """Ejecuta Tesseract sobre una imagen en gris y devuelve su salida."""
    with tempfile.TemporaryDirectory() as tmp:
        img_path = Path(tmp) / "page.png"
        cv2.imwrite(str(img_path), gray)
        cmd = [
            TESSERACT, str(img_path), "stdout",
            "-l", lang, "--psm", str(psm), "--dpi", str(DPI), *(extra or []),
        ]
        if output:
            cmd.append(output)
        with _tesseract_slots:
            proc = subprocess.run(cmd, capture_output=True, env=_ENV)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip())
    return proc.stdout.decode("utf-8", "replace")


class _Renderer:
    """Renderiza páginas de un PDF (pdfium no es seguro entre hilos)."""

    def __init__(self, pdf_path: Path):
        self._doc = pdfium.PdfDocument(str(pdf_path))
        self._lock = threading.Lock()

    def render(self, page_index: int) -> np.ndarray:
        with self._lock:
            page = self._doc[page_index]
            image = page.render(scale=DPI / 72).to_pil().convert("L")
        return np.array(image)

    def close(self) -> None:
        """Libera el PDF (en Windows, abierto impide borrar el archivo)."""
        self._doc.close()


def _fix_orientation(gray: np.ndarray) -> np.ndarray:
    """Gira la página según la orientación que detecta Tesseract (OSD)."""
    try:
        osd = _tesseract(gray, 0, None, lang="osd")
    except RuntimeError:
        return gray  # muy poco texto para decidir
    rotate = re.search(r"Rotate:\s*(\d+)", osd)
    conf = re.search(r"Orientation confidence:\s*([\d.]+)", osd)
    if not rotate or not conf or float(conf.group(1)) < MIN_OSD_CONF:
        return gray
    code = _ROTATIONS.get(int(rotate.group(1)))
    return cv2.rotate(gray, code) if code is not None else gray


def _deskew(gray: np.ndarray) -> np.ndarray:
    """Endereza inclinaciones pequeñas del escaneo (hasta ±1.5°).

    Elige el ángulo que hace más "picuda" la proyección de tinta por filas
    (líneas de texto y bordes de tabla quedan horizontales).
    """
    small = cv2.resize(gray, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA)
    _, ink = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    h, w = ink.shape

    def score(angle: float) -> float:
        matrix = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1)
        rotated = cv2.warpAffine(ink, matrix, (w, h))
        profile = rotated.sum(axis=1, dtype=np.float64)
        return float(np.square(np.diff(profile)).sum())

    coarse = max(np.arange(-1.5, 1.51, 0.1), key=score)
    angle = max(np.arange(coarse - 0.08, coarse + 0.081, 0.02), key=score)
    if abs(angle) < 0.05:
        return gray
    height, width = gray.shape
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1)
    return cv2.warpAffine(
        gray, matrix, (width, height), flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT, borderValue=255,
    )


# --------------------------------------------------------------------------
# Palabras y texto corrido
# --------------------------------------------------------------------------

def _words(gray: np.ndarray, psm: int, min_conf: float = MIN_CONF) -> list[dict]:
    tsv = _tesseract(gray, psm, "tsv")
    reader = csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE)
    words = []
    for row in reader:
        if row["level"] != "5":
            continue
        text = (row["text"] or "").strip()
        if not text or float(row["conf"]) < min_conf:
            continue
        left, top = int(row["left"]), int(row["top"])
        width, height = int(row["width"]), int(row["height"])
        words.append({
            "text": text, "left": left, "top": top, "width": width, "height": height,
            "cx": left + width / 2, "cy": top + height / 2,
        })
    return words


def _group_lines(words: list[dict]) -> list[list[dict]]:
    """Agrupa palabras en líneas visuales (por altura) y las ordena de izq. a der."""
    lines: list[list[dict]] = []
    for word in sorted(words, key=lambda w: (w["cy"], w["left"])):
        if lines:
            last = lines[-1]
            ref_cy = sum(w["cy"] for w in last) / len(last)
            ref_h = sum(w["height"] for w in last) / len(last)
            if abs(word["cy"] - ref_cy) <= 0.6 * ref_h:
                last.append(word)
                continue
        lines.append([word])
    return [sorted(line, key=lambda w: w["left"]) for line in lines]


def _text_markdown(words: list[dict]) -> str:
    """Texto por líneas visuales; se unen solo las líneas de un mismo párrafo.

    Se agrupa por posición (no por el orden de bloques de Tesseract) para que
    cada etiqueta quede en la misma línea que su valor ("ÓRGANO: GERENCIA...").
    """
    paragraphs: list[str] = []
    prev_cy = prev_h = None
    for line in _group_lines(words):
        cy = sum(w["cy"] for w in line) / len(line)
        height = sum(w["height"] for w in line) / len(line)
        text = " ".join(w["text"] for w in line)
        if paragraphs and cy - prev_cy <= _PARAGRAPH_PITCH * (height + prev_h) / 2:
            paragraphs[-1] += " " + text
        else:
            paragraphs.append(text)
        prev_cy, prev_h = cy, height
    return "\n\n".join(paragraphs)


# --------------------------------------------------------------------------
# Tablas con cuadrícula
# --------------------------------------------------------------------------

def _runs(flags: np.ndarray) -> list[tuple[int, int]]:
    """Tramos [inicio, fin) de valores True consecutivos."""
    padded = np.concatenate(([False], flags, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return list(zip(edges[::2], edges[1::2]))


def _detect_grid(gray: np.ndarray):
    """Detecta una cuadrícula que ocupa buena parte de la página.

    Devuelve (bbox, filas, columnas, bandas_rellenas) o None. Las filas son
    (y0, y1, es_relleno); las columnas, (x0, x1); bbox = (x, y, w, h).
    """
    height, width = gray.shape
    _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(width // 40, 40), 1))
    v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(height // 40, 40)))
    hmask = cv2.morphologyEx(ink, cv2.MORPH_OPEN, h_kernel)
    vmask = cv2.morphologyEx(ink, cv2.MORPH_OPEN, v_kernel)

    lines = cv2.dilate(hmask | vmask, np.ones((5, 5), np.uint8))
    count, _, stats, _ = cv2.connectedComponentsWithStats(lines)
    if count < 2:
        return None
    biggest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    x, y, w, h = (int(stats[biggest, k]) for k in (
        cv2.CC_STAT_LEFT, cv2.CC_STAT_TOP, cv2.CC_STAT_WIDTH, cv2.CC_STAT_HEIGHT))
    covers_page = w * h >= _TABLE_MIN_PAGE_FRACTION * width * height

    row_strength = hmask[y:y + h, x:x + w].sum(axis=1) / 255 / w
    col_strength = vmask[y:y + h, x:x + w].sum(axis=0) / 255 / h

    # Filas: entre líneas horizontales; una banda gruesa es un encabezado relleno.
    bounds: list[tuple[int, bool]] = []  # (posición, inicia_relleno)
    fills: list[tuple[int, int]] = []
    for start, end in _runs(row_strength > 0.5):
        if end - start >= _FILL_MIN_PX:
            fills.append((y + start, y + end))
            bounds += [(y + start, False), (y + end, False)]
        else:
            bounds.append((y + (start + end) // 2, False))
    ys = sorted({b for b, _ in bounds})
    rows = []
    for y0, y1 in zip(ys, ys[1:]):
        if y1 - y0 < 15:
            continue
        rows.append((y0, y1, (y0, y1) in fills))

    xs = [x + (s + e) // 2 for s, e in _runs(col_strength > 0.5)]
    cols = [(x0, x1) for x0, x1 in zip(xs, xs[1:]) if x1 - x0 >= 15]

    if len(rows) < _TABLE_MIN_ROWS or len(cols) < _TABLE_MIN_COLS:
        return None
    # Una tabla corta (última página de un listado) no llena la hoja, pero
    # conserva el encabezado relleno y muchas columnas; un formulario no.
    if not covers_page and not (fills and len(cols) >= _TABLE_FILLED_MIN_COLS):
        return None
    return (x, y, w, h), rows, cols, fills, hmask | vmask


def _prepare_table_image(gray, fills, line_mask):
    """Quita las líneas de la cuadrícula y invierte los encabezados rellenos."""
    clean = gray.copy()
    clean[cv2.dilate(line_mask, np.ones((3, 3), np.uint8)) > 0] = 255
    for y0, y1 in fills:
        band = gray[y0:y1]
        # Texto claro sobre fondo gris -> texto negro sobre fondo blanco, sin
        # los separadores claros (verticales y horizontales) entre celdas.
        threshold = (int(np.median(band)) + 255) // 2
        text = np.where(band > threshold, 255, 0).astype(np.uint8)
        for size in ((1, max(int(0.85 * (y1 - y0)), 3)), (60, 1)):
            seps = cv2.morphologyEx(
                text, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, size))
            text = cv2.subtract(text, seps)
        clean[y0:y1] = 255 - text
    return clean


def _ocr_cell(clean: np.ndarray, rows, cols, r: int, c: int, digits: bool = False) -> str:
    """OCR de una celda: recorte ampliado; vacío si la celda no tiene tinta.

    Con digits=True solo reconoce cifras (para columnas numéricas, donde un
    dígito suelto suele ignorarse).
    """
    margin = 4
    y0, y1, _ = rows[r]
    x0, x1 = cols[c]
    crop = clean[y0 + margin:y1 - margin, x0 + margin:x1 - margin]
    if crop.size == 0 or (crop < 128).mean() < 0.002:
        return ""
    if digits:
        return _ocr_digits(crop)
    crop = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    crop = cv2.copyMakeBorder(crop, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=255)
    for psm in (6, 7, 8, 10):
        text = " ".join(_tesseract(crop, psm, None).split())
        if text:
            return text.replace("|", "\\|")
    return ""


def _ocr_digits(crop: np.ndarray) -> str:
    """Cifras sueltas: miden ~13 px, así que se prueban varias preparaciones
    (a veces se leen a escala nativa y a veces solo engrosadas o ampliadas)."""
    _, otsu = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    ys, xs = np.nonzero(otsu < 128)
    if len(ys) == 0:
        return ""
    tight = otsu[max(ys.min() - 6, 0):ys.max() + 7, max(xs.min() - 6, 0):xs.max() + 7]
    variants = (
        crop,
        tight,
        cv2.erode(tight, np.ones((2, 2), np.uint8)),
        cv2.resize(tight, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC),
    )
    extra = ["-c", "tessedit_char_whitelist=0123456789"]
    for image in variants:
        image = cv2.copyMakeBorder(image, 30, 30, 30, 30, cv2.BORDER_CONSTANT, value=255)
        for psm in (7, 10):
            text = "".join(_tesseract(image, psm, None, extra=extra).split())
            if text:
                return text
    return ""


_MONEY = re.compile(r"^[S5]/\s*(\d{1,3}(?:[.,\s]{1,2}\d{3})*)[.,\s](\d{2})$")


def _normalize_money(text: str) -> str:
    """'S/ 8,500,00' / '5/ 12.500,00' -> 'S/ 8,500.00' / 'S/ 12,500.00'."""
    match = _MONEY.match(text)
    if not match:
        return text
    whole = int(re.sub(r"\D", "", match.group(1)))
    return f"S/ {whole:,}.{match.group(2)}"


def _strip_text(clean: np.ndarray, y0: int, y1: int) -> str:
    """Texto suelto por encima/debajo de la tabla (títulos, notas)."""
    if y1 - y0 < 30:
        return ""
    words = _words(clean[y0:y1], psm=6, min_conf=60)
    return "\n\n".join(" ".join(w["text"] for w in line) for line in _group_lines(words))


def _table_markdown(gray: np.ndarray, grid) -> str:
    (bx, by, bw, bh), rows, cols, fills, line_mask = grid
    clean = _prepare_table_image(gray, fills, line_mask)

    coords = [(r, c) for r in range(len(rows)) for c in range(len(cols))]
    with ThreadPoolExecutor(max_workers=_MAX_TESSERACT) as pool:
        texts = list(pool.map(lambda rc: _ocr_cell(clean, rows, cols, *rc), coords))
    table = [texts[r * len(cols):(r + 1) * len(cols)] for r in range(len(rows))]

    # Columnas numéricas: se repite el OCR restringido a dígitos.
    # Solo se rehacen las celdas vacías o que no salieron como número.
    body = [r for r, (_, _, is_fill) in enumerate(rows) if not is_fill]
    for c in range(len(cols)):
        values = [table[r][c] for r in body if table[r][c]]
        if len(values) < 3 or sum(v.isdigit() for v in values) < 0.6 * len(values):
            continue
        todo = [r for r in body if not table[r][c].isdigit()]
        with ThreadPoolExecutor(max_workers=_MAX_TESSERACT) as pool:
            redone = list(pool.map(
                lambda r: _ocr_cell(clean, rows, cols, r, c, digits=True), todo))
        for r, text in zip(todo, redone):
            table[r][c] = text
    table = [[_normalize_money(cell) for cell in row] for row in table]
    table = [row for row in table if any(row)]
    if not table:
        return ""

    lines = ["| " + " | ".join(table[0]) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(row) + " |" for row in table[1:]]
    parts = [
        _strip_text(clean, 0, rows[0][0]),
        "\n".join(lines),
        _strip_text(clean, rows[-1][1], gray.shape[0]),
    ]
    return "\n\n".join(p for p in parts if p)


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

def ocr_page(renderer: _Renderer, page_index: int) -> str:
    """Devuelve el Markdown de una página (índice 0-based) mediante OCR."""
    gray = _deskew(_fix_orientation(renderer.render(page_index)))
    grid = _detect_grid(gray)
    if grid is not None:
        return _table_markdown(gray, grid)
    return _text_markdown(_words(gray, psm=3))


def ocr_pages(pdf_path: Path, page_indices: list[int]) -> dict[int, str]:
    """OCR de varias páginas (0-based) en paralelo -> {índice: markdown}."""
    renderer = _Renderer(Path(pdf_path))
    try:
        with ThreadPoolExecutor(max_workers=_MAX_TESSERACT) as pool:
            results = pool.map(lambda i: ocr_page(renderer, i), page_indices)
            return dict(zip(page_indices, results))
    finally:
        renderer.close()


def convert_with_ocr(pdf_path: Path, ocr_page_indices: list[int]) -> str:
    """Markdown del PDF con las páginas indicadas (0-based) reconocidas por OCR.

    El resto de las páginas usa el texto embebido. Cada página con OCR va
    precedida de un comentario HTML para poder auditar lo reconocido.
    """
    import pdf_inspector  # import local: este módulo también se usa sin él

    to_ocr = set(ocr_page_indices)
    recognized = ocr_pages(pdf_path, sorted(to_ocr))
    parts = []
    for page in pdf_inspector.extract_pages_markdown(str(pdf_path)).pages:
        if page.page in to_ocr:
            text = recognized[page.page].strip()
            if text:
                parts.append(f"<!-- OCR: página {page.page + 1} -->\n\n{text}")
        elif page.markdown.strip():
            parts.append(page.markdown.strip())
    return re.sub(r"\n{3,}", "\n\n", "\n\n".join(parts)) + "\n"
