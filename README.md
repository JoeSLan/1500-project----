# ⚖️ LegalSite Auditor (MVP)

Due diligence legal-urbana asistida por IA para inmuebles en el Estado de México y CDMX.
Sube un PDF (escritura, certificado de libertad de gravámenes, CUZUS, constancia de zonificación o
Plan de Desarrollo Urbano) y obtén un reporte de riesgos en español: zonificación, gravámenes ocultos,
anomalías de superficie y banderas rojas para el inversionista.

## Estructura

```
legalsite-auditor/
├── app.py                     # UI Streamlit (punto de entrada)
├── cli.py                     # Análisis por línea de comandos de un PDF local
├── requirements.txt
├── pyproject.toml             # Config de pytest
├── .env.example               # Plantilla de variables (copiar a .env)
├── .streamlit/config.toml     # Tema y límite de carga
├── auditor/
│   ├── config.py              # Settings desde .env
│   ├── exceptions.py          # Errores con mensajes para el usuario
│   ├── extractor.py           # PyMuPDF: texto por página + detección de escaneados
│   ├── prompts.py             # System prompt legal (Edomex/CDMX)
│   ├── schemas.py             # Contrato JSON (Pydantic) del análisis
│   ├── analyzer.py            # Llamada a Claude (tool forzada) + verificación de citas
│   └── report.py              # Exportación a Word, Markdown y JSON
├── samples/
│   ├── generar_pdf_prueba.py  # Genera un expediente ficticio con riesgos sembrados
│   └── expediente_prueba.pdf
├── reports/                   # Salida del CLI
└── tests/                     # pytest (no consume API: cliente simulado)
```

## Cómo funciona

1. **Extracción** — PyMuPDF extrae texto marcado por página (`=== PÁGINA N ===`) para que cada hallazgo cite su página.
2. **Escaneados** — si ≥ 50 % de las páginas no tienen texto, el PDF se envía a Claude como documento nativo
   (lectura visual), hasta 100 páginas / 32 MB.
3. **Análisis** — Claude responde obligatoriamente vía una herramienta con esquema JSON estricto
   (validado con Pydantic), nunca texto libre.
4. **Anti-alucinación** — cada cita de evidencia se compara contra el texto extraído; las que no aparecen
   textualmente se marcan con ⚠️ para revisión manual.
5. **Entrega** — reporte ejecutivo descargable en Word, Markdown y JSON.

## Ejecutar localmente

Requisitos: Python 3.10+ y una clave de la API de Claude (console.anthropic.com).

```bash
cd legalsite-auditor
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # Windows: copy .env.example .env
# edita .env y pega tu ANTHROPIC_API_KEY

streamlit run app.py               # abre http://localhost:8501
```

CLI (sin interfaz):

```bash
python cli.py samples/expediente_prueba.pdf --context "Proyecto de 24 departamentos"
# genera reports/reporte_expediente_prueba.{md,docx,json}
```

Pruebas:

```bash
pytest
```

## Configuración (.env)

| Variable | Default | Uso |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Obligatoria |
| `LEGALSITE_MODEL` | `claude-sonnet-5-5` | Usa `claude-opus-5-5` para máxima precisión |
| `LEGALSITE_MAX_OUTPUT_TOKENS` | `8000` | Súbelo si el reporte se corta |
| `LEGALSITE_MAX_INPUT_CHARS` | `500000` | Texto máximo enviado (PDUs largos se truncan por página) |
| `LEGALSITE_MAX_UPLOAD_MB` | `50` | Límite de archivo (ajusta también `.streamlit/config.toml`) |
| `LEGALSITE_REQUEST_TIMEOUT_S` | `300` | Timeout de la API |

## Aviso

Herramienta de apoyo. No constituye opinión legal ni sustituye la revisión de notario/abogado ni la
consulta directa al Registro Público y a las autoridades de desarrollo urbano.
