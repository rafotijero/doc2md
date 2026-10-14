"""Conversión masiva de carpetas de PDF o DOCX a Markdown (uso local).

Uso:
    python -m doc2md.cli pdf  <carpeta_entrada> [carpeta_salida] [--recursive]
                              [--workers N] [--overwrite] [--ocr] [--merge-tables]
    python -m doc2md.cli docx <carpeta_entrada> [carpeta_salida]
                              [--workers N] [--overwrite]

Por cada archivo encontrado en la carpeta de entrada, genera un "<nombre>.md"
en la carpeta de salida (por defecto "<carpeta_entrada>_md", creada junto a la
carpeta de entrada), conservando la estructura de subcarpetas.

Los archivos cuyo .md ya exista en la salida se omiten (permite reanudar una
corrida interrumpida); usa --overwrite para forzar su reconversión. Los
fallos se registran en "_fallidos.log" dentro de la carpeta de salida.

PDF: con --ocr, las páginas escaneadas (sin texto embebido) se reconocen con
Tesseract; sin él quedan vacías en el .md. Con --merge-tables se quitan los
pies de página ("... Página N de M") y las cabeceras que se repiten en cada
hoja, de modo que una tabla que ocupa varias páginas queda como una sola.

DOCX: solo se extrae el texto (párrafos, títulos, listas y tablas); las
imágenes embebidas se ignoran.
"""

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable


def _convert_pdf(ocr: bool, merge: bool) -> Callable[[Path, Path], str]:
    from .pdf import pdf_to_markdown

    def convert(src: Path, dst: Path) -> str:
        result = pdf_to_markdown(src, ocr=ocr, merge=merge)
        dst.write_text(result.markdown, encoding="utf-8")
        ocr_note = f", OCR {result.ocr_pages} pag." if result.ocr_pages else ""
        return f"{result.pdf_type}, {result.page_count} pag.{ocr_note}"

    return convert


def _convert_docx(src: Path, dst: Path) -> str:
    from .word import docx_to_markdown

    dst.write_text(docx_to_markdown(src), encoding="utf-8")
    return "docx"


def convert_folder(
    input_dir: Path,
    output_dir: Path,
    pattern: str,
    convert: Callable[[Path, Path], str],
    workers: int,
    overwrite: bool,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(p for p in input_dir.glob(pattern) if not p.name.startswith("~$"))
    if not files:
        print(f"No se encontraron archivos ({pattern}) en {input_dir}")
        return

    jobs: list[tuple[Path, Path, Path]] = []
    skipped = 0
    for src in files:
        rel = src.relative_to(input_dir)
        dst = (output_dir / rel).with_suffix(".md")
        if dst.exists() and not overwrite:
            skipped += 1
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        jobs.append((src, dst, rel))

    print(f"Encontrados {len(files)} archivo(s) en {input_dir}")
    print(f"Salida: {output_dir}")
    if skipped:
        print(f"Omitidos (ya convertidos): {skipped}")
    print(f"Pendientes: {len(jobs)}  |  workers: {workers}\n")

    if not jobs:
        print("Nada por hacer.")
        return

    ok = 0
    failed: list[tuple[Path, str]] = []
    total = len(jobs)
    done = 0
    start_all = time.time()

    def run_safe(job: tuple[Path, Path, Path]):
        src, dst, rel = job
        start = time.time()
        try:
            return rel, True, convert(src, dst), time.time() - start
        except Exception as e:
            return rel, False, str(e), time.time() - start

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(run_safe, job) for job in jobs]
        for future in as_completed(futures):
            rel, success, info, elapsed = future.result()
            done += 1
            if success:
                print(f"[{done}/{total}] OK  ({info}, {elapsed:.1f}s) {rel}")
                ok += 1
            else:
                print(f"[{done}/{total}] ERROR ({elapsed:.1f}s) {rel}: {info}")
                failed.append((rel, info))

    elapsed_all = time.time() - start_all
    print(f"\nCompletado: {ok}/{total} convertidos en {elapsed_all:.1f}s.")
    if skipped:
        print(f"Omitidos por ya existir: {skipped}")

    if failed:
        log_path = output_dir / "_fallidos.log"
        with log_path.open("w", encoding="utf-8") as f:
            for rel, err in failed:
                f.write(f"{rel}: {err}\n")
        print(f"\nFallidos ({len(failed)}), detalle en {log_path}:")
        for rel, err in failed:
            print(f"  - {rel}: {err}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m doc2md.cli",
        description="Convierte de forma masiva los PDF o DOCX de una carpeta a Markdown.",
    )
    sub = parser.add_subparsers(dest="kind", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("input_dir", help="Carpeta con los archivos a convertir")
        p.add_argument(
            "output_dir",
            nargs="?",
            default=None,
            help=(
                "Carpeta de salida para los .md "
                "(por defecto: '<carpeta_entrada>_md' junto a la carpeta de entrada)"
            ),
        )
        p.add_argument(
            "--workers", "-w", type=int, default=4,
            help="Número de conversiones en paralelo (por defecto: 4)",
        )
        p.add_argument(
            "--overwrite", action="store_true",
            help="Reconvertir aunque el .md de salida ya exista (por defecto se omite)",
        )

    pdf = sub.add_parser("pdf", help="Convertir PDF")
    common(pdf)
    pdf.add_argument(
        "--recursive", "-r", action="store_true",
        help="Buscar también PDFs en subcarpetas (conserva la estructura en la salida)",
    )
    pdf.add_argument(
        "--ocr", action="store_true",
        help="Reconocer con OCR (Tesseract) las páginas escaneadas sin texto embebido",
    )
    pdf.add_argument(
        "--merge-tables", action="store_true",
        help=(
            "Quitar pies de página ('Página N de M') y cabeceras repetidas para "
            "unir en una sola las tablas que ocupan varias páginas"
        ),
    )

    docx = sub.add_parser("docx", help="Convertir DOCX (siempre recursivo)")
    common(docx)

    args = parser.parse_args()

    input_dir = Path(args.input_dir).resolve()
    if not input_dir.is_dir():
        print(f"Error: '{input_dir}' no es una carpeta valida.")
        sys.exit(1)

    output_dir = (
        Path(args.output_dir).resolve()
        if args.output_dir
        else input_dir.parent / f"{input_dir.name}_md"
    )

    if args.kind == "pdf":
        pattern = "**/*.pdf" if args.recursive else "*.pdf"
        convert = _convert_pdf(args.ocr, args.merge_tables)
    else:
        pattern = "**/*.docx"
        convert = _convert_docx

    convert_folder(input_dir, output_dir, pattern, convert, args.workers, args.overwrite)


if __name__ == "__main__":
    main()
