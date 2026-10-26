"""LegalSite Auditor — Streamlit UI.

Run with:  streamlit run app.py
"""

from __future__ import annotations

import logging
from pathlib import Path

import streamlit as st

from auditor.analyzer import AnalysisOutcome, LegalAnalyzer
from auditor.config import SAMPLES_DIR, Settings, load_settings
from auditor.exceptions import AuditorError
from auditor.extractor import ExtractionResult, extract_pdf
from auditor.report import property_rows, to_docx, to_json, to_markdown, zoning_rows
from auditor.schemas import CATEGORY_LABELS, RISK_LABELS, SEVERITY_LABELS, SEVERITY_ORDER

# 1. IMPORTANTE: Importamos el sistema de autenticación SaaS
from auth_ui import render_login_screen, render_user_sidebar

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

st.set_page_config(page_title="LegalSite Auditor", page_icon="⚖️", layout="wide")

# ------------------------------------------------------------------
# PROTECCIÓN SAAS: VALIDAR LOGIN Y MEMBRESÍA ANTES DE MOSTRAR NADA
# ------------------------------------------------------------------
user = render_login_screen()

if not user:
    # Si el usuario no ha iniciado sesión, se detiene la ejecución aquí
    st.stop()

# Si ya inició sesión, mostramos sus datos de membresía en la barra lateral
render_user_sidebar(user)
# ------------------------------------------------------------------

SEVERITY_ICONS = {"CRITICA": "🔴", "ALTA": "🟠", "MEDIA": "🟡", "BAJA": "🔵"}
RISK_ICONS = {"CRITICO": "🔴", "ALTO": "🟠", "MEDIO": "🟡", "BAJO": "🟢"}


@st.cache_resource
def get_settings() -> Settings:
    return load_settings()


@st.cache_data(show_spinner=False, max_entries=20)
def cached_extract(data: bytes, file_name: str, max_bytes: int) -> ExtractionResult:
    return extract_pdf(data, file_name=file_name, max_bytes=max_bytes)


def local_samples() -> list[Path]:
    return sorted(SAMPLES_DIR.glob("*.pdf")) if SAMPLES_DIR.is_dir() else []


# ------------------------------------------------------------------ sidebar
def render_sidebar(settings: Settings) -> None:
    with st.sidebar:
        st.header("⚙️ Configuración")
        st.success("Modo Local / Offline Activo")
        st.caption(f"Modelo: `modo-local`")
        st.caption(f"Límite de archivo: {settings.max_upload_mb} MB")
        st.divider()
        st.markdown(
            "**Qué revisa**\n"
            "- Zonificación y uso de suelo\n"
            "- Gravámenes y cargas ocultas\n"
            "- Anomalías de superficie\n"
            "- Banderas rojas para inversión"
        )
        st.divider()
        st.caption(
            "Herramienta de apoyo. No sustituye la revisión de notario/abogado ni la consulta "
            "al Registro Público."
        )


# ------------------------------------------------------------------- input
def select_document() -> tuple[bytes, str] | None:
    source = st.radio("Origen del documento", ["Subir PDF", "PDF de prueba local"], horizontal=True)
    if source == "Subir PDF":
        uploaded = st.file_uploader(
            "Escritura, certificado de libertad de gravámenes, CUZUS, constancia de zonificación o Plan de Desarrollo Urbano",
            type=["pdf"],
        )
        return (uploaded.getvalue(), uploaded.name) if uploaded else None

    samples = local_samples()
    if not samples:
        st.info(f"No hay PDFs en `{SAMPLES_DIR.name}/`. Copia ahí tu PDF de prueba o súbelo directamente.")
        return None
    chosen = st.selectbox("PDF de prueba", samples, format_func=lambda p: p.name)
    return chosen.read_bytes(), chosen.name


def render_extraction_summary(extraction: ExtractionResult) -> None:
    c1, c2, c3 = st.columns(3)
    c1.metric("Páginas", extraction.page_count)
    c2.metric("Caracteres extraídos", f"{extraction.total_chars:,}")
    c3.metric("Páginas sin texto", len(extraction.low_text_pages))
    if extraction.is_likely_scanned:
        st.warning("El documento parece escaneado. Se analizará en modo visual.")
    with st.expander("Ver texto extraído"):
        st.text_area("Texto", extraction.full_text[:60_000], height=300, label_visibility="collapsed")


# ------------------------------------------------------------------ output
def render_results(outcome: AnalysisOutcome, file_name: str) -> None:
    report = outcome.report
    level = report.nivel_riesgo_global

    st.divider()
    st.subheader(f"{RISK_ICONS[level]} Riesgo {RISK_LABELS[level]} — {report.puntuacion_riesgo}/100")
    st.markdown(f"**{report.veredicto}**")
    st.caption(f"Tipo de documento: {report.tipo_documento}")

    counts = report.count_by_severity()
    cols = st.columns(4)
    for col, key in zip(cols, ["CRITICA", "ALTA", "MEDIA", "BAJA"]):
        col.metric(f"{SEVERITY_ICONS[key]} {SEVERITY_LABELS[key]}", counts[key])

    for warning in outcome.warnings:
        st.warning(warning)

    tab_summary, tab_findings, tab_property, tab_next = st.tabs(
        ["Resumen ejecutivo", f"Hallazgos ({len(report.hallazgos)})", "Inmueble y zonificación", "Siguientes pasos"]
    )

    with tab_summary:
        st.markdown(report.resumen_ejecutivo)

    with tab_findings:
        if not report.hallazgos:
            st.success("No se identificaron hallazgos de riesgo.")
        order = sorted(range(len(report.hallazgos)), key=lambda i: SEVERITY_ORDER[report.hallazgos[i].severidad])
        for idx in order:
            f = report.hallazgos[idx]
            page = f"p. {f.pagina}" if f.pagina else "s/p"
            title = f"{SEVERITY_ICONS[f.severidad]} {f.titulo}  ·  {page}"
            with st.expander(title, expanded=f.severidad in ("CRITICA", "ALTA")):
                st.caption(f"{CATEGORY_LABELS[f.categoria]} · Severidad {SEVERITY_LABELS[f.severidad]}")
                st.markdown(f.descripcion)
                verified = outcome.evidence_verified.get(idx)
                badge = {True: "✅ cita verificada", False: "⚠️ cita no encontrada textualmente", None: ""}[verified]
                st.markdown(f"> {f.evidencia}")
                if badge:
                    st.caption(badge)
                st.markdown(f"**Recomendación:** {f.recomendacion}")

    with tab_property:
        left, right = st.columns(2)
        with left:
            st.markdown("**Identificación del inmueble**")
            st.table({"Campo": [k for k, _ in property_rows(report)], "Valor": [v for _, v in property_rows(report)]})
        with right:
            st.markdown("**Zonificación**")
            st.table({"Parámetro": [k for k, _ in zoning_rows(report)], "Valor": [v for _, v in zoning_rows(report)]})

    with tab_next:
        if report.siguientes_pasos:
            st.markdown("**Siguientes pasos**")
            st.markdown("\n".join(f"{i}. {s}" for i, s in enumerate(report.siguientes_pasos, start=1)))
        if report.documentos_faltantes:
            st.markdown("**Documentos a solicitar**")
            st.markdown("\n".join(f"- {d}" for d in report.documentos_faltantes))
        if report.limitaciones:
            st.markdown("**Limitaciones del análisis**")
            st.markdown("\n".join(f"- {d}" for d in report.limitaciones))

    st.divider()
    stem = Path(file_name).stem
    d1, d2, d3 = st.columns(3)
    d1.download_button(
        "📄 Descargar Word",
        data=to_docx(outcome, file_name),
        file_name=f"reporte_{stem}.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    d2.download_button(
        "📝 Descargar Markdown",
        data=to_markdown(outcome, file_name),
        file_name=f"reporte_{stem}.md",
        mime="text/markdown",
    )
    d3.download_button(
        "🧾 Descargar JSON",
        data=to_json(outcome, file_name),
        file_name=f"reporte_{stem}.json",
        mime="application/json",
    )
    st.caption(
        f"Modelo {outcome.model} · modo {outcome.mode} · {outcome.pages_analyzed} páginas · "
        f"{outcome.input_tokens:,} tokens de entrada / {outcome.output_tokens:,} de salida · {outcome.elapsed_s:.0f} s"
    )


# --------------------------------------------------------------------- main
def main() -> None:
    try:
        settings = get_settings()
    except AuditorError as exc:
        st.error(str(exc))
        st.stop()

    render_sidebar(settings)
    st.title("⚖️ LegalSite Auditor")
    st.caption("Due diligence legal-urbana asistida por IA para inmuebles en el Estado de México y CDMX.")

    selected = select_document()
    if selected is None:
        st.session_state.pop("result", None)
        return
    data, file_name = selected

    try:
        extraction = cached_extract(data, file_name, settings.max_upload_bytes)
    except AuditorError as exc:
        st.error(str(exc))
        return

    render_extraction_summary(extraction)

    context = st.text_area(
        "Contexto opcional para el análisis",
        placeholder="Ej.: Queremos construir 24 departamentos de 4 niveles; el vendedor dice que el predio mide 850 m².",
        height=80,
    )

    if st.button("🔍 Analizar riesgos", type="primary"):
        try:
            with st.spinner("Generando auditoría legal-urbana en modo local..."):
                analyzer = LegalAnalyzer(settings)
                outcome = analyzer.analyze(extraction, pdf_bytes=data, extra_context=context)
            st.session_state["result"] = {"sha": extraction.sha256, "outcome": outcome, "file_name": file_name}
        except AuditorError as exc:
            st.error(str(exc))
            return

    result = st.session_state.get("result")
    if result and result["sha"] == extraction.sha256:
        render_results(result["outcome"], result["file_name"])


if __name__ == "__main__":
    main()
