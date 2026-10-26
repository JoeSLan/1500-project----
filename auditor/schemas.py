"""Structured output contract between Claude and the app.

The same Pydantic models generate the JSON Schema sent to Claude as a forced
tool, and validate what comes back, so the UI never parses free text.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RiskLevel = Literal["BAJO", "MEDIO", "ALTO", "CRITICO"]
Severity = Literal["BAJA", "MEDIA", "ALTA", "CRITICA"]
Category = Literal[
    "ZONIFICACION",
    "GRAVAMEN",
    "SUPERFICIE",
    "TITULARIDAD",
    "AMBIENTAL_RIESGO",
    "PATRIMONIO",
    "ADMINISTRATIVO_FISCAL",
    "OTRO",
]

SEVERITY_ORDER: dict[str, int] = {"CRITICA": 0, "ALTA": 1, "MEDIA": 2, "BAJA": 3}

SEVERITY_LABELS: dict[str, str] = {"CRITICA": "Crítica", "ALTA": "Alta", "MEDIA": "Media", "BAJA": "Baja"}
RISK_LABELS: dict[str, str] = {"CRITICO": "CRÍTICO", "ALTO": "ALTO", "MEDIO": "MEDIO", "BAJO": "BAJO"}
CATEGORY_LABELS: dict[str, str] = {
    "ZONIFICACION": "Zonificación y uso de suelo",
    "GRAVAMEN": "Gravámenes y cargas",
    "SUPERFICIE": "Superficie y linderos",
    "TITULARIDAD": "Titularidad y régimen de propiedad",
    "AMBIENTAL_RIESGO": "Ambiental y zonas de riesgo",
    "PATRIMONIO": "Patrimonio histórico/artístico",
    "ADMINISTRATIVO_FISCAL": "Administrativo y fiscal",
    "OTRO": "Otro",
}


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class PropertyIdentification(_Model):
    ubicacion: str | None = Field(None, description="Dirección, colonia, municipio/alcaldía y entidad, tal como aparecen.")
    clave_catastral: str | None = Field(None, description="Cuenta o clave catastral, si aparece.")
    folio_real: str | None = Field(None, description="Folio real o electrónico del Registro Público, si aparece.")
    superficie_terreno: str | None = Field(None, description="Superficie de terreno declarada, con unidades (m²).")
    superficie_construccion: str | None = Field(None, description="Superficie de construcción declarada, con unidades (m²).")
    titular: str | None = Field(None, description="Propietario o titular registral según el documento.")


class ZoningSummary(_Model):
    uso_de_suelo: str | None = Field(None, description="Clave y descripción del uso de suelo (p. ej. 'HM 4/20/Z').")
    niveles_maximos: str | None = None
    altura_maxima: str | None = None
    area_libre_minima: str | None = Field(None, description="Porcentaje mínimo de área libre.")
    cos: str | None = Field(None, description="Coeficiente de Ocupación del Suelo.")
    cus: str | None = Field(None, description="Coeficiente de Utilización del Suelo.")
    densidad: str | None = Field(None, description="Densidad o número máximo de viviendas.")
    usos_permitidos: list[str] = Field(default_factory=list)
    usos_prohibidos: list[str] = Field(default_factory=list)
    instrumento_aplicable: str | None = Field(
        None, description="Plan o programa de desarrollo urbano citado en el documento, con fecha si aparece."
    )


class Finding(_Model):
    categoria: Category
    severidad: Severity
    titulo: str = Field(..., description="Título corto y concreto del hallazgo (máx. ~12 palabras).")
    descripcion: str = Field(..., description="Qué se encontró y por qué representa un riesgo para el inversionista.")
    evidencia: str = Field(
        ...,
        description="Cita TEXTUAL breve (máx. ~30 palabras) copiada del documento que sustenta el hallazgo. "
        "Si el hallazgo es una AUSENCIA de información, escribir 'AUSENCIA: <qué falta>'.",
    )
    pagina: int | None = Field(None, description="Número de página (1 = primera página del PDF).")
    recomendacion: str = Field(..., description="Acción concreta de mitigación o verificación.")


class RiskReport(_Model):
    tipo_documento: str = Field(..., description="Tipo de documento identificado (p. ej. escritura, CUZUS, PDU).")
    inmueble: PropertyIdentification
    nivel_riesgo_global: RiskLevel
    puntuacion_riesgo: int = Field(..., ge=0, le=100, description="0 = sin riesgo aparente, 100 = riesgo extremo.")
    veredicto: str = Field(..., description="Una frase de conclusión para el inversionista.")
    resumen_ejecutivo: str = Field(..., description="3 a 6 párrafos cortos, listos para entregar a un cliente.")
    zonificacion: ZoningSummary
    hallazgos: list[Finding] = Field(default_factory=list)
    documentos_faltantes: list[str] = Field(
        default_factory=list, description="Documentos que se deben solicitar para completar la due diligence."
    )
    siguientes_pasos: list[str] = Field(default_factory=list, description="Acciones priorizadas.")
    limitaciones: list[str] = Field(
        default_factory=list, description="Limitaciones del análisis (texto ilegible, páginas faltantes, etc.)."
    )

    def sorted_findings(self) -> list[Finding]:
        return sorted(self.hallazgos, key=lambda f: SEVERITY_ORDER[f.severidad])

    def count_by_severity(self) -> dict[str, int]:
        counts = {key: 0 for key in SEVERITY_ORDER}
        for finding in self.hallazgos:
            counts[finding.severidad] += 1
        return counts


def _inline_refs(node: object, defs: dict) -> object:
    """Replace every {"$ref": "#/$defs/X"} with the definition itself (self-contained schema)."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            resolved = dict(defs[ref.removeprefix("#/$defs/")])
            extras = {k: v for k, v in node.items() if k != "$ref"}
            resolved.update(extras)
            return _inline_refs(resolved, defs)
        return {k: _inline_refs(v, defs) for k, v in node.items() if k not in ("$defs", "title")}
    if isinstance(node, list):
        return [_inline_refs(item, defs) for item in node]
    return node


def tool_input_schema() -> dict:
    """Self-contained JSON Schema for the Claude tool, derived from RiskReport."""
    schema = RiskReport.model_json_schema()
    return _inline_refs(schema, schema.get("$defs", {}))  # type: ignore[return-value]
