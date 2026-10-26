import json

import pytest

from auditor.analyzer import LegalAnalyzer, verify_evidence
from auditor.exceptions import AnalysisError
from auditor.extractor import extract_pdf
from auditor.prompts import SYSTEM_PROMPT, TOOL_NAME
from auditor.schemas import tool_input_schema
from tests.conftest import FakeClient, fake_response, make_pdf


def test_text_mode_returns_validated_report(settings, sample_pdf_bytes, sample_payload):
    client = FakeClient(fake_response(sample_payload))
    extraction = extract_pdf(sample_pdf_bytes, "doc.pdf")
    outcome = LegalAnalyzer(settings, client=client).analyze(extraction, pdf_bytes=sample_pdf_bytes, extra_context="Proyecto")

    call = client.messages.calls[0]
    assert call["model"] == "claude-test"
    assert call["tool_choice"] == {"type": "tool", "name": TOOL_NAME}
    assert call["system"] == SYSTEM_PROMPT
    user_text = " ".join(block["text"] for block in call["messages"][0]["content"])
    assert "=== PÁGINA 2 ===" in user_text and "<contexto_usuario>" in user_text

    assert outcome.mode == "texto"
    assert outcome.report.nivel_riesgo_global == "CRITICO"
    assert outcome.report.sorted_findings()[0].severidad == "CRITICA"
    assert outcome.input_tokens == 1234
    # Findings 0 and 1 quote the text, 2 is invented, 3 is an absence.
    assert outcome.evidence_verified == {0: True, 1: True, 2: False, 3: None}
    assert any("no se encontraron textualmente" in w for w in outcome.warnings)


def test_scanned_pdf_uses_native_document_block(settings, sample_payload):
    data = make_pdf(["", "", ""])
    client = FakeClient(fake_response(sample_payload))
    outcome = LegalAnalyzer(settings, client=client).analyze(extract_pdf(data), pdf_bytes=data)

    content = client.messages.calls[0]["messages"][0]["content"]
    assert content[0]["type"] == "document"
    assert content[0]["source"]["media_type"] == "application/pdf"
    assert outcome.mode == "pdf_nativo"
    assert all(v is None for v in outcome.evidence_verified.values())


def test_scanned_without_bytes_and_no_text_fails(settings, sample_payload):
    data = make_pdf(["", ""])
    analyzer = LegalAnalyzer(settings, client=FakeClient(fake_response(sample_payload)))
    with pytest.raises(AnalysisError, match="OCR"):
        analyzer.analyze(extract_pdf(data), pdf_bytes=None)


def test_max_tokens_stop_raises(settings, sample_pdf_bytes, sample_payload):
    client = FakeClient(fake_response(sample_payload, stop_reason="max_tokens"))
    with pytest.raises(AnalysisError, match="LEGALSITE_MAX_OUTPUT_TOKENS"):
        LegalAnalyzer(settings, client=client).analyze(extract_pdf(sample_pdf_bytes), pdf_bytes=sample_pdf_bytes)


def test_missing_tool_call_raises(settings, sample_pdf_bytes):
    client = FakeClient(fake_response(None, stop_reason="end_turn"))
    with pytest.raises(AnalysisError, match="estructurado"):
        LegalAnalyzer(settings, client=client).analyze(extract_pdf(sample_pdf_bytes), pdf_bytes=sample_pdf_bytes)


def test_invalid_payload_raises(settings, sample_pdf_bytes, sample_payload):
    sample_payload["nivel_riesgo_global"] = "MUY_ALTO"
    client = FakeClient(fake_response(sample_payload))
    with pytest.raises(AnalysisError, match="formato"):
        LegalAnalyzer(settings, client=client).analyze(extract_pdf(sample_pdf_bytes), pdf_bytes=sample_pdf_bytes)


def test_missing_api_key_is_reported(settings):
    from dataclasses import replace

    from auditor.exceptions import ConfigurationError

    with pytest.raises(ConfigurationError):
        LegalAnalyzer(replace(settings, api_key=None))


def test_tool_schema_is_self_contained():
    schema = tool_input_schema()
    dumped = json.dumps(schema)
    assert "$ref" not in dumped and "$defs" not in dumped
    assert schema["type"] == "object"
    assert "hallazgos" in schema["required"] or "hallazgos" in schema["properties"]
    finding = schema["properties"]["hallazgos"]["items"]
    assert set(finding["properties"]["severidad"]["enum"]) == {"BAJA", "MEDIA", "ALTA", "CRITICA"}


@pytest.mark.parametrize(
    ("quote", "expected"),
    [
        ("“HIPOTECA en primer lugar”", True),
        ("hipoteca   EN PRIMER lugar ... sin cancelación inscrita", True),
        ("embargo inexistente en el documento", False),
        ("AUSENCIA: avalúo", None),
    ],
)
def test_verify_evidence(quote, expected):
    source = "HIPOTECA en primer lugar a favor de Banco Ficticio, sin cancelación inscrita."
    assert verify_evidence(quote, source) is expected
