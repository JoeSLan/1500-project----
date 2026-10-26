"""Command-line runner: analyze a local PDF without the UI.

Usage:
    python cli.py samples/expediente_prueba.pdf
    python cli.py ruta/al/documento.pdf --context "Proyecto de 20 deptos" --out reports/
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from auditor.analyzer import LegalAnalyzer
from auditor.config import REPORTS_DIR, load_settings
from auditor.exceptions import AuditorError
from auditor.extractor import extract_pdf_file
from auditor.report import to_docx, to_json, to_markdown
from auditor.schemas import RISK_LABELS


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LegalSite Auditor — análisis de riesgo de un PDF local.")
    parser.add_argument("pdf", type=Path, help="Ruta al PDF a analizar")
    parser.add_argument("--context", default=None, help="Contexto opcional del proyecto o la operación")
    parser.add_argument("--out", type=Path, default=REPORTS_DIR, help="Carpeta de salida (default: reports/)")
    parser.add_argument("--verbose", action="store_true", help="Mostrar logs detallados")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    try:
        settings = load_settings()
        extraction = extract_pdf_file(args.pdf, max_bytes=settings.max_upload_bytes)
        print(f"→ {extraction.file_name}: {extraction.page_count} páginas, {extraction.total_chars:,} caracteres")
        print(f"→ Analizando con {settings.model}…")
        outcome = LegalAnalyzer(settings).analyze(
            extraction, pdf_bytes=args.pdf.read_bytes(), extra_context=args.context
        )
    except AuditorError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    stem = args.pdf.stem
    md_path = args.out / f"reporte_{stem}.md"
    docx_path = args.out / f"reporte_{stem}.docx"
    json_path = args.out / f"reporte_{stem}.json"
    md_path.write_text(to_markdown(outcome, extraction.file_name), encoding="utf-8")
    docx_path.write_bytes(to_docx(outcome, extraction.file_name))
    json_path.write_text(to_json(outcome, extraction.file_name), encoding="utf-8")

    report = outcome.report
    print(f"\nRiesgo {RISK_LABELS[report.nivel_riesgo_global]} ({report.puntuacion_riesgo}/100): {report.veredicto}")
    for finding in report.sorted_findings():
        page = f"p. {finding.pagina}" if finding.pagina else "s/p"
        print(f"  [{finding.severidad:<7}] {finding.titulo} ({page})")
    for warning in outcome.warnings:
        print(f"  ⚠ {warning}")
    print(f"\nReportes guardados en: {md_path}, {docx_path}, {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
