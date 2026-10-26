from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from auditor.config import Settings
from auditor.prompts import TOOL_NAME


def make_pdf(pages: list[str]) -> bytes:
    """Build an in-memory PDF; an empty string produces a page with no text layer."""
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_textbox(pymupdf.Rect(50, 50, 550, 800), text, fontname="helv", fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def settings() -> Settings:
    return Settings(
        api_key="test-key",
        model="claude-test",
        max_output_tokens=4000,
        max_input_chars=200_000,
        max_upload_mb=10,
        request_timeout_s=60,
    )


@pytest.fixture
def sample_pdf_bytes() -> bytes:
    return make_pdf(
        [
            "Folio Real: 00123456\nHIPOTECA en primer lugar a favor de Banco Ficticio, sin cancelación inscrita.",
            "Superficie según escritura: 850.00 m²\nSuperficie según catastro: 792.40 m²",
        ]
    )


@pytest.fixture
def sample_payload() -> dict:
    return {
        "tipo_documento": "Certificado de libertad de gravámenes",
        "inmueble": {"ubicacion": "Texcoco, Estado de México", "folio_real": "00123456"},
        "nivel_riesgo_global": "CRITICO",
        "puntuacion_riesgo": 82,
        "veredicto": "No invertir hasta cancelar la hipoteca.",
        "resumen_ejecutivo": "Párrafo uno.\n\nPárrafo dos.",
        "zonificacion": {"uso_de_suelo": "H 3/40/B", "usos_prohibidos": ["industria"]},
        "hallazgos": [
            {
                "categoria": "SUPERFICIE",
                "severidad": "MEDIA",
                "titulo": "Diferencia de superficie",
                "descripcion": "Escritura y catastro no coinciden (6.8 %).",
                "evidencia": "Superficie según catastro: 792.40 m²",
                "pagina": 2,
                "recomendacion": "Levantamiento topográfico.",
            },
            {
                "categoria": "GRAVAMEN",
                "severidad": "CRITICA",
                "titulo": "Hipoteca vigente",
                "descripcion": "Hipoteca sin cancelar.",
                "evidencia": "HIPOTECA en primer lugar a favor de Banco Ficticio",
                "pagina": 1,
                "recomendacion": "Exigir carta de liberación.",
            },
            {
                "categoria": "GRAVAMEN",
                "severidad": "ALTA",
                "titulo": "Cita inventada",
                "descripcion": "Prueba de verificación.",
                "evidencia": "Embargo trabado por el SAT por cinco millones",
                "pagina": 1,
                "recomendacion": "Verificar.",
            },
            {
                "categoria": "ADMINISTRATIVO_FISCAL",
                "severidad": "BAJA",
                "titulo": "Sin boleta predial",
                "descripcion": "No se adjunta.",
                "evidencia": "AUSENCIA: boleta predial",
                "pagina": None,
                "recomendacion": "Solicitar boleta.",
            },
        ],
        "documentos_faltantes": ["Boleta predial"],
        "siguientes_pasos": ["Solicitar carta de liberación de hipoteca"],
        "limitaciones": ["Análisis asistido por IA."],
    }


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FakeClient:
    def __init__(self, response):
        self.messages = FakeMessages(response)


def fake_response(payload: dict | None, stop_reason: str = "tool_use"):
    content = []
    if payload is not None:
        content.append(SimpleNamespace(type="tool_use", name=TOOL_NAME, input=payload, id="toolu_1"))
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        model="claude-test",
        usage=SimpleNamespace(input_tokens=1234, output_tokens=567),
    )


SAMPLE_FILE = Path(__file__).resolve().parent.parent / "samples" / "expediente_prueba.pdf"
