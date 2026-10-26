import pytest

from auditor.exceptions import PDFExtractionError
from auditor.extractor import extract_pdf, extract_pdf_file
from tests.conftest import SAMPLE_FILE, make_pdf


def test_extracts_page_tagged_text(sample_pdf_bytes):
    result = extract_pdf(sample_pdf_bytes, "doc.pdf")
    assert result.page_count == 2
    assert "=== PÁGINA 1 ===" in result.full_text
    assert "=== PÁGINA 2 ===" in result.full_text
    assert "792.40 m²" in result.pages[1].text
    assert not result.is_likely_scanned
    assert len(result.sha256) == 64


def test_truncates_at_page_boundary():
    data = make_pdf(["A" * 3000, "B" * 3000, "C" * 3000])
    result = extract_pdf(data, "long.pdf")
    prompt = result.to_prompt_text(max_chars=7000)
    assert prompt.truncated
    assert prompt.pages_included == 2
    assert "C" * 100 not in prompt.text


def test_no_truncation_when_it_fits(sample_pdf_bytes):
    prompt = extract_pdf(sample_pdf_bytes).to_prompt_text(max_chars=100_000)
    assert not prompt.truncated
    assert prompt.pages_included == 2


def test_detects_scanned_document():
    result = extract_pdf(make_pdf(["", "", "Texto suficiente en una sola página para no ser vacía."]))
    assert result.low_text_pages == [1, 2]
    assert result.is_likely_scanned


@pytest.mark.parametrize("data", [b"", b"not a pdf at all"])
def test_rejects_invalid_input(data):
    with pytest.raises(PDFExtractionError):
        extract_pdf(data)


def test_rejects_oversized_file(sample_pdf_bytes):
    with pytest.raises(PDFExtractionError, match="excede"):
        extract_pdf(sample_pdf_bytes, max_bytes=100)


def test_rejects_password_protected():
    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_text((50, 50), "secreto")
    data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u")
    with pytest.raises(PDFExtractionError, match="contraseña"):
        extract_pdf(data)


def test_bundled_sample_file():
    result = extract_pdf_file(SAMPLE_FILE)
    assert result.page_count == 3
    assert "HIPOTECA" in result.full_text
