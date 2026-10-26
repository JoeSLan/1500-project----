"""PDF text extraction with PyMuPDF.

Produces page-tagged text so Claude can cite page numbers, and flags pages with
little or no text layer (scanned documents) so the analyzer can switch to
Claude's native PDF (vision) input instead.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from auditor.exceptions import PDFExtractionError

# A page with fewer meaningful characters than this is treated as "no text layer".
LOW_TEXT_THRESHOLD = 40
# If at least this share of pages lack text, the document is treated as scanned.
SCANNED_RATIO_THRESHOLD = 0.5

PAGE_HEADER = "=== PÁGINA {n} ==="

_MULTI_BLANK_LINES = re.compile(r"\n{3,}")
_TRAILING_SPACES = re.compile(r"[ \t]+\n")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


@dataclass(frozen=True)
class PageText:
    number: int  # 1-based, as shown in a PDF viewer
    text: str

    @property
    def char_count(self) -> int:
        return len(self.text.strip())


@dataclass(frozen=True)
class PromptText:
    text: str
    pages_included: int
    truncated: bool


@dataclass
class ExtractionResult:
    file_name: str
    sha256: str
    size_bytes: int
    page_count: int
    pages: list[PageText] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def low_text_pages(self) -> list[int]:
        return [p.number for p in self.pages if p.char_count < LOW_TEXT_THRESHOLD]

    @property
    def total_chars(self) -> int:
        return sum(p.char_count for p in self.pages)

    @property
    def is_likely_scanned(self) -> bool:
        if self.page_count == 0:
            return False
        return len(self.low_text_pages) / self.page_count >= SCANNED_RATIO_THRESHOLD

    @property
    def full_text(self) -> str:
        return "\n\n".join(f"{PAGE_HEADER.format(n=p.number)}\n{p.text}" for p in self.pages)

    def to_prompt_text(self, max_chars: int) -> PromptText:
        """Join page texts with page markers, stopping cleanly at a page boundary."""
        chunks: list[str] = []
        used = 0
        for page in self.pages:
            chunk = f"{PAGE_HEADER.format(n=page.number)}\n{page.text}"
            if used + len(chunk) > max_chars:
                if not chunks:
                    # Even the first page is too long: hard-cut it rather than send nothing.
                    chunks.append(chunk[:max_chars])
                return PromptText(text="\n\n".join(chunks), pages_included=len(chunks), truncated=True)
            chunks.append(chunk)
            used += len(chunk) + 2
        return PromptText(text="\n\n".join(chunks), pages_included=len(chunks), truncated=False)


def _clean(text: str) -> str:
    text = _CONTROL_CHARS.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _TRAILING_SPACES.sub("\n", text)
    text = _MULTI_BLANK_LINES.sub("\n\n", text)
    return text.strip()


def extract_pdf(data: bytes, file_name: str = "documento.pdf", max_bytes: int | None = None) -> ExtractionResult:
    """Extract page-tagged text from PDF bytes."""
    if not data:
        raise PDFExtractionError("El archivo está vacío.")
    if max_bytes is not None and len(data) > max_bytes:
        raise PDFExtractionError(
            f"El archivo pesa {len(data) / 1_048_576:.1f} MB y excede el límite de {max_bytes / 1_048_576:.0f} MB."
        )
    if not data.lstrip()[:5].startswith(b"%PDF"):
        raise PDFExtractionError("El archivo no parece ser un PDF válido.")

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # PyMuPDF raises several internal exception types
        raise PDFExtractionError(f"No se pudo abrir el PDF: {exc}") from exc

    with doc:
        if doc.needs_pass and not doc.authenticate(""):
            raise PDFExtractionError("El PDF está protegido con contraseña. Quita la protección y vuelve a subirlo.")

        pages: list[PageText] = []
        for index, page in enumerate(doc):
            try:
                raw = page.get_text("text", sort=True)
            except Exception as exc:
                raise PDFExtractionError(f"Error al leer la página {index + 1}: {exc}") from exc
            pages.append(PageText(number=index + 1, text=_clean(raw)))

        metadata = {k: str(v) for k, v in (doc.metadata or {}).items() if v}

    return ExtractionResult(
        file_name=file_name,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        page_count=len(pages),
        pages=pages,
        metadata=metadata,
    )


def extract_pdf_file(path: str | Path, max_bytes: int | None = None) -> ExtractionResult:
    """Convenience wrapper for local files."""
    path = Path(path)
    if not path.is_file():
        raise PDFExtractionError(f"No existe el archivo: {path}")
    return extract_pdf(path.read_bytes(), file_name=path.name, max_bytes=max_bytes)
