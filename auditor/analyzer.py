from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from auditor.config import Settings
from auditor.schemas import RiskReport

@dataclass
class AnalysisOutcome:
    report: RiskReport
    model: str
    mode: str
    input_tokens: int
    output_tokens: int
    elapsed_s: float
    pages_analyzed: int
    truncated: bool
    warnings: list[str] = field(default_factory=list)
    evidence_verified: dict[int, bool | None] = field(default_factory=dict)

class LegalAnalyzer:
    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.settings = settings

    def analyze(
        self,
        extraction: Any,
        pdf_bytes: bytes | None = None,
        extra_context: str | None = None,
    ) -> AnalysisOutcome:
        report_data = {
            "tipo_documento": "Escritura Pública y Antecedentes Registrales (Simulado)",
            "puntuacion_riesgo": 78,
            "nivel_riesgo_global": "ALTO",
            "veredicto": "Riesgo Alto - Se detectaron discrepancias catastrales y cargas financieras pendientes.",
            "resumen_ejecutivo": "El análisis integral del expediente inmobiliario revela inconsistencias críticas entre la superficie reportada en la boleta predial y la escritura pública, además de un gravamen hipotecario inscrito no cancelado formalmente y restricciones de zonificación en Naucalpan.",
            "hallazgos": [
                {
                    "categoria": "SUPERFICIE",
                    "severidad": "ALTA",
                    "titulo": "Discrepancia en superficie de terreno",
                    "descripcion": "La boleta predial reporta 180 m², mientras que la escritura pública señala 210 m².",
                    "evidencia": "...superficie de ciento ochenta metros cuadrados... vs ...doscientos diez metros cuadrados...",
                    "pagina": 1,
                    "recomendacion": "Realizar un levantamiento topográfico georeferenciado para conciliar medidas."
                },
                {
                    "categoria": "GRAVAMEN",
                    "severidad": "CRITICA",
                    "titulo": "Hipoteca activa no cancelada",
                    "descripcion": "Existe una inscripción hipotecaria a favor de institución financiera con fecha de 2018 sin cancelación.",
                    "evidencia": "...gravamen inscrito bajo el número 4520... sin constancia de liberación...",
                    "pagina": 2,
                    "recomendacion": "Exigir al vendedor el testimonio de cancelación de hipoteca debidamente inscrito."
                },
                {
                    "categoria": "ZONIFICACION",
                    "severidad": "MEDIA",
                    "titulo": "Restricciones de densidad habitacional",
                    "descripcion": "El predio se ubica en zona habitacional con restricciones de altura según el Plan de Desarrollo Urbano.",
                    "evidencia": "...densidad H-2/33... sujeto a restricciones de COS y CUS...",
                    "pagina": 3,
                    "recomendacion": "Verificar la constancia de zonificación y uso de suelo específica (CUZUS) vigente."
                }
            ],
            "inmueble": {
                "calle_numero": "Avenida de las Fuentes #14",
                "colonia": "Vista del Valle",
                "municipio_alcaldia": "Naucalpan de Juárez",
                "estado": "Estado de México",
                "cuenta_predial": "098-452-11-000-1",
                "folio_real_antecedente": "FR-9876543",
                "superficie_terreno_m2": 210.0,
                "superficie_construccion_m2": 180.0,
                "propietario_actual": "Titular Registral Vigente"
            },
            "zonificacion": {
                "uso_suelo": "Habitacional Unifamiliar / Mixto",
                "densidad": "H-2 / 33%",
                "superficie_maxima_construccion": "350 m²",
                "niveles_maximos": "3 niveles",
                "restricciones_notables": "Restricción frontal de 3 metros"
            },
            "siguientes_pasos": [
                "Solicitar certificado de libertad de gravamen actualizado ante el IFREM.",
                "Revisar constancias de no adeudo de agua y predial."
            ],
            "documentos_faltantes": [
                "Certificado de Libertad de Gravamen (últimos 10 años)",
                "Constancia de No Adeudo de Agua"
            ],
            "limitaciones": [
                "Análisis basado en el expediente digital proporcionado de forma local."
            ]
        }

        report = RiskReport.model_validate(report_data)

        return AnalysisOutcome(
            report=report,
            model="modo-local-mvp",
            mode="texto",
            input_tokens=1500,
            output_tokens=850,
            elapsed_s=0.4,
            pages_analyzed=extraction.page_count,
            truncated=False,
            warnings=["Ejecutándose en modo local simulado (sin consumo de API externa)."],
            evidence_verified={0: True, 1: True, 2: True}
        )