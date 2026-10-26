"""Generate a FICTITIOUS 3-page test file with planted red flags.

Usage:  python samples/generar_pdf_prueba.py
Output: samples/expediente_prueba.pdf

Planted issues (to check the analyzer catches them):
  - Hipoteca vigente + embargo inscrito (gravámenes)
  - Superficie: escritura 850.00 m² vs. catastro 792.40 m² vs. linderos que suman otra cifra
  - Zonificación H 3/40/B incompatible con proyecto de 6 niveles
  - Afectación vial y antecedente ejidal
"""

from __future__ import annotations

from pathlib import Path

import pymupdf

OUTPUT = Path(__file__).resolve().parent / "expediente_prueba.pdf"

PAGES = [
    (
        "CERTIFICADO DE LIBERTAD O EXISTENCIA DE GRAVÁMENES",
        """DOCUMENTO FICTICIO PARA PRUEBAS - SIN VALIDEZ LEGAL

Instituto de la Función Registral del Estado de México (IFREM)
Oficina Registral: Texcoco
Folio Real Electrónico: 00123456

Inmueble: Lote 14, Manzana 3, Fraccionamiento Los Álamos, Municipio de Texcoco, Estado de México.
Titular registral: Inmobiliaria Horizonte Verde, S.A. de C.V.
Superficie según antecedente registral: 850.00 m².

Antecedente: Inmueble proveniente de la parcela 87 del Ejido de San Miguel Tlaixpan, \
según título de propiedad cuyo dominio pleno se encuentra en trámite ante el Registro Agrario Nacional.

GRAVÁMENES Y ANOTACIONES VIGENTES:
1. HIPOTECA en primer lugar a favor de Banco Ficticio del Centro, S.A., por la cantidad de \
$4,500,000.00 M.N., inscrita el 12 de marzo de 2021. Sin cancelación inscrita.
2. EMBARGO ordenado por el Juzgado Tercero Civil de Texcoco, expediente 455/2025, a favor de \
Constructora Delta, S.A. de C.V., por $1,280,000.00 M.N., inscrito el 3 de febrero de 2026.
3. Anotación preventiva de demanda de nulidad de compraventa, expediente 102/2026.

Se expide a solicitud de parte interesada el 15 de septiembre de 2026.""",
    ),
    (
        "CONSTANCIA DE ZONIFICACIÓN / CÉDULA INFORMATIVA",
        """DOCUMENTO FICTICIO PARA PRUEBAS - SIN VALIDEZ LEGAL

Dirección de Desarrollo Urbano, H. Ayuntamiento de Texcoco.
Plan Municipal de Desarrollo Urbano de Texcoco, publicado en Gaceta del Gobierno el 8 de mayo de 2019.

Clave de uso de suelo: H 3/40/B - Habitacional densidad baja.
Niveles máximos de construcción: 2 niveles o 7.50 metros sobre nivel de banqueta.
Área libre mínima: 40% de la superficie del predio.
Coeficiente de Ocupación del Suelo (COS): 0.60
Coeficiente de Utilización del Suelo (CUS): 1.2
Número máximo de viviendas: 3 viviendas por predio.
Usos prohibidos: comercio mayor a 50 m², industria, bodegas.

RESTRICCIONES:
- Afectación por derecho de vía del proyecto de ampliación de la Avenida Juárez: franja de 6.00 metros \
en el frente del predio.
- Restricción de construcción de 5.00 metros a partir del alineamiento.

Vigencia de esta constancia: 12 meses a partir del 10 de enero de 2025.""",
    ),
    (
        "EXTRACTO DE BOLETA PREDIAL Y PLANO",
        """DOCUMENTO FICTICIO PARA PRUEBAS - SIN VALIDEZ LEGAL

Clave catastral: 099-05-014-03-00-0000
Superficie de terreno según catastro: 792.40 m²
Superficie de construcción según catastro: 120.00 m²
Adeudo de impuesto predial: ejercicios 2023, 2024 y 2025.

Medidas y colindancias según plano del vendedor:
Al norte: 25.00 m con lote 13.
Al sur: 25.00 m con calle Fresno.
Al oriente: 30.00 m con lote 15.
Al poniente: 30.00 m con Avenida Juárez.

Nota del vendedor: el predio es apto para desarrollo vertical de 6 niveles con 24 departamentos.""",
    ),
]


def build(path: Path = OUTPUT) -> Path:
    doc = pymupdf.open()
    for title, body in PAGES:
        page = doc.new_page(width=612, height=792)  # US Letter
        page.insert_textbox(pymupdf.Rect(54, 54, 558, 100), title, fontname="hebo", fontsize=14, align=1)
        page.insert_textbox(pymupdf.Rect(54, 110, 558, 750), body, fontname="helv", fontsize=10.5)
    doc.save(path)
    doc.close()
    return path


if __name__ == "__main__":
    print(f"PDF de prueba generado: {build()}")
