"""
generar_reporte.py --- Reportes PDF
======================================
Genera reportes profesionales en PDF con todos los datos del sistema:
portada con KPIs, estadisticas, tablas de lecturas, graficas y mapa de sismos.

Contenido del reporte:
- Portada con 4 KPI cards (lecturas, anomalias, sismos, sensores)
- Resumen estadistico por sensor (promedio, minimo, maximo, desviacion)
- Tabla de las ultimas 50 lecturas
- Grafica de barras: anomalias por sensor
- Grafica de linea: serie temporal de lecturas
- Grafica de dona: distribucion de severidad
- Mapa mundial de sismos embebido como imagen (opcional, requiere red)
- Tabla de los ultimos 40 eventos criticos
- Pie de pagina con numero de pagina y fecha de generacion

Modos de generacion:
- Terminal:       python generar_reporte.py
- Dashboard HTML: boton "Exportar PDF" -> GET /reporte (Flask/FastAPI)
- Bot Telegram:   comando /reporte
- Flask API:      GET /reporte
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import statistics
import tempfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

logger = logging.getLogger("sipia.reporte")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "sipia_readings.db"
OUTPUT_DIR = BASE_DIR / "reportes"

COLOR_PRIMARIO = colors.HexColor("#1E3A5F")
COLOR_CRITICO = colors.HexColor("#D32F2F")
COLOR_WARNING = colors.HexColor("#F9A825")
COLOR_OK = colors.HexColor("#2E7D32")

TABLAS_REQUERIDAS = ("readings", "events")


# ---------------------------------------------------------------------------
# Utilidades de datos
# ---------------------------------------------------------------------------

def _num(valor: Any, default: float = 0.0) -> float:
    """Convierte a float de forma segura; devuelve `default` si es None/invalido."""
    if valor is None:
        return default
    try:
        return float(valor)
    except (TypeError, ValueError):
        return default


def _texto(valor: Any, default: str = "-") -> str:
    return str(valor) if valor not in (None, "") else default


def _tablas_existentes(conn: sqlite3.Connection) -> set[str]:
    filas = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {f[0] for f in filas}


# ---------------------------------------------------------------------------
# Obtencion de datos desde SQLite
# ---------------------------------------------------------------------------

def obtener_datos() -> dict:
    """Lee SQLite (readings + events) y arma el diccionario de datos para el reporte.

    Nunca lanza excepcion: ante base de datos ausente, tablas faltantes o
    errores de lectura, devuelve un diccionario vacio y registra el problema
    para que el reporte se genere igual (con las secciones correspondientes
    mostrando "sin datos").
    """
    vacio = {"lecturas": [], "eventos": [], "sismos": []}

    if not DB_PATH.exists():
        logger.warning("No se encontro la base de datos en %s", DB_PATH)
        return vacio

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        logger.error("No se pudo abrir la base de datos: %s", exc)
        return vacio

    try:
        tablas = _tablas_existentes(conn)
        lecturas = []
        eventos = []

        if "readings" in tablas:
            lecturas = [dict(r) for r in conn.execute(
                "SELECT * FROM readings ORDER BY reading_id DESC LIMIT 50"
            ).fetchall()]
        else:
            logger.warning("La tabla 'readings' no existe en la base de datos")

        if "events" in tablas:
            eventos = [dict(r) for r in conn.execute(
                "SELECT * FROM events ORDER BY event_id DESC LIMIT 40"
            ).fetchall()]
        else:
            logger.warning("La tabla 'events' no existe en la base de datos")

    except sqlite3.Error as exc:
        logger.error("Error leyendo datos de la base: %s", exc)
        return vacio
    finally:
        conn.close()

    sismos = [l for l in lecturas if l.get("sensor_id") == "VIB-01"]
    return {"lecturas": lecturas, "eventos": eventos, "sismos": sismos}


# ---------------------------------------------------------------------------
# Bloques del reporte
# ---------------------------------------------------------------------------

def _estilos():
    hoja = getSampleStyleSheet()
    hoja.add(ParagraphStyle(
        "SIPIATitulo", parent=hoja["Title"], textColor=COLOR_PRIMARIO, fontSize=24,
    ))
    hoja.add(ParagraphStyle(
        "SIPIASubtitulo", parent=hoja["Heading2"], textColor=COLOR_PRIMARIO, spaceBefore=16,
    ))
    return hoja


def bloque_portada(datos: dict, estilos) -> list:
    story = [
        Paragraph("SIPIA", estilos["SIPIATitulo"]),
        Paragraph("Sistema de Monitoreo Sismico Inteligente", estilos["Heading3"]),
        Paragraph(f"Reporte generado: {datetime.now().strftime('%d/%m/%Y %H:%M')}", estilos["Normal"]),
        Spacer(1, 0.8 * cm),
    ]

    n_lecturas = len(datos["lecturas"])
    n_anomalias = len(datos["eventos"])
    n_sismos = len(datos["sismos"])
    sensores = len({l.get("sensor_id") for l in datos["lecturas"] if l.get("sensor_id")})

    kpis = [
        ("Lecturas", str(n_lecturas)),
        ("Anomalias", str(n_anomalias)),
        ("Sismos VIB-01", str(n_sismos)),
        ("Sensores activos", str(sensores)),
    ]
    tabla_kpi = Table([[k for k, _ in kpis], [v for _, v in kpis]], colWidths=[4 * cm] * 4)
    tabla_kpi.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), COLOR_PRIMARIO),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 1), (-1, 1), 16),
        ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(tabla_kpi)
    story.append(Spacer(1, 1 * cm))

    if n_lecturas == 0:
        story.append(Paragraph(
            "No se encontraron lecturas en el periodo consultado. Las secciones "
            "siguientes se muestran vacias.", estilos["Normal"],
        ))
        story.append(Spacer(1, 0.5 * cm))

    return story


def bloque_resumen_estadistico(datos: dict, estilos) -> list:
    story = [Paragraph("Resumen Estadistico por Sensor", estilos["SIPIASubtitulo"])]

    por_sensor: dict[str, list[float]] = defaultdict(list)
    for l in datos["lecturas"]:
        sensor = l.get("sensor_id")
        if sensor is None or l.get("value") is None:
            continue
        por_sensor[sensor].append(_num(l["value"]))

    filas = [["Sensor", "Promedio", "Minimo", "Maximo", "Desv. estandar", "N"]]
    for sensor, valores in sorted(por_sensor.items()):
        if not valores:
            continue
        desv = f"{statistics.pstdev(valores):.2f}" if len(valores) > 1 else "-"
        filas.append([
            sensor,
            f"{statistics.mean(valores):.2f}",
            f"{min(valores):.2f}",
            f"{max(valores):.2f}",
            desv,
            str(len(valores)),
        ])

    if len(filas) == 1:
        story.append(Paragraph("Sin datos de lecturas disponibles.", estilos["Normal"]))
        return story

    tabla = Table(filas, colWidths=[2.8 * cm, 2.8 * cm, 2.8 * cm, 2.8 * cm, 3.2 * cm, 1.6 * cm])
    tabla.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), COLOR_PRIMARIO),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.white]),
    ]))
    story.append(tabla)
    story.append(Spacer(1, 0.8 * cm))
    return story


def bloque_tabla_lecturas(datos: dict, estilos) -> list:
    story = [Paragraph("Ultimas Lecturas (max. 50)", estilos["SIPIASubtitulo"])]

    filas = [["Sensor", "Valor", "Unidad", "Timestamp"]]
    for l in datos["lecturas"][:50]:
        filas.append([
            _texto(l.get("sensor_id")),
            f"{_num(l.get('value')):.2f}" if l.get("value") is not None else "-",
            _texto(l.get("unit"), ""),
            _texto(l.get("timestamp"), "")[:19],
        ])

    if len(filas) == 1:
        story.append(Paragraph("Sin lecturas registradas.", estilos["Normal"]))
        return story

    tabla = Table(filas, colWidths=[3 * cm, 3 * cm, 3 * cm, 5 * cm], repeatRows=1)
    tabla.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), COLOR_PRIMARIO),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.white]),
    ]))
    story.append(tabla)
    story.append(Spacer(1, 0.8 * cm))
    return story


def bloque_graficas(datos: dict, estilos, tmp_dir: Path) -> list:
    """Genera y embebe: barras (anomalias/sensor), linea (serie temporal), dona (severidad)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    story = [Paragraph("Graficas del Sistema", estilos["SIPIASubtitulo"])]

    def _figura_vacia(ax, mensaje="Sin datos disponibles"):
        ax.text(0.5, 0.5, mensaje, ha="center", va="center", color="#888888", transform=ax.transAxes)
        ax.set_xticks([])
        ax.set_yticks([])

    # --- Barras: anomalias por sensor
    conteo = Counter(e.get("sensor_id") for e in datos["eventos"] if e.get("sensor_id"))
    fig, ax = plt.subplots(figsize=(6, 3))
    if conteo:
        ax.bar(list(conteo.keys()), list(conteo.values()), color="#1E3A5F")
        ax.set_ylabel("Cantidad")
    else:
        _figura_vacia(ax)
    ax.set_title("Anomalias por Sensor")
    fig.tight_layout()
    ruta_barras = tmp_dir / "barras.png"
    fig.savefig(ruta_barras, dpi=140)
    plt.close(fig)
    story.append(Image(str(ruta_barras), width=15 * cm, height=7 * cm))
    story.append(Spacer(1, 0.4 * cm))

    # --- Linea: serie temporal de lecturas (todas mezcladas, orden cronologico)
    fig, ax = plt.subplots(figsize=(6, 3))
    lecturas_ord = [l for l in reversed(datos["lecturas"]) if l.get("value") is not None]
    if lecturas_ord:
        ax.plot(range(len(lecturas_ord)), [_num(l["value"]) for l in lecturas_ord], color="#1E3A5F")
        ax.set_xlabel("Muestra")
        ax.set_ylabel("Valor")
    else:
        _figura_vacia(ax)
    ax.set_title("Serie Temporal de Lecturas")
    fig.tight_layout()
    ruta_linea = tmp_dir / "linea.png"
    fig.savefig(ruta_linea, dpi=140)
    plt.close(fig)
    story.append(Image(str(ruta_linea), width=15 * cm, height=7 * cm))
    story.append(Spacer(1, 0.4 * cm))

    # --- Dona: distribucion de severidad
    sev_conteo = Counter(e.get("severity") for e in datos["eventos"] if e.get("severity"))
    fig, ax = plt.subplots(figsize=(5, 5))
    if sev_conteo:
        colores_map = {"CRITICAL": "#D32F2F", "WARNING": "#F9A825", "INFO": "#2E7D32"}
        ax.pie(
            list(sev_conteo.values()),
            labels=list(sev_conteo.keys()),
            colors=[colores_map.get(k, "#999999") for k in sev_conteo.keys()],
            autopct="%1.0f%%",
            wedgeprops=dict(width=0.4),
        )
    else:
        _figura_vacia(ax)
    ax.set_title("Distribucion de Severidad")
    fig.tight_layout()
    ruta_dona = tmp_dir / "dona.png"
    fig.savefig(ruta_dona, dpi=140)
    plt.close(fig)
    story.append(Image(str(ruta_dona), width=10 * cm, height=10 * cm))
    story.append(Spacer(1, 0.8 * cm))

    return story


def bloque_mapa(datos: dict, estilos, tmp_dir: Path) -> list:
    """Genera un mapa mundial simple con los sismos recientes (matplotlib, sin dependencias GIS).

    La consulta a USGS es de mejor esfuerzo: si falla (sin red, timeout,
    modulo ausente) el mapa se genera igual, mostrando que no hay datos.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    story = [Paragraph("Mapa Mundial de Sismos (USGS)", estilos["SIPIASubtitulo"])]

    sismos: list[dict] = []
    try:
        from real_sensors import obtener_sismos_usgs
        sismos = obtener_sismos_usgs(limite=30) or []
    except Exception as exc:
        logger.warning("No se pudo obtener sismos de USGS: %s", exc)

    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.set_xlim(-180, 180)
    ax.set_ylim(-90, 90)
    ax.set_facecolor("#E8F0F8")
    ax.set_title("Sismos Recientes -- API USGS")
    ax.set_xlabel("Longitud")
    ax.set_ylabel("Latitud")
    ax.axhline(0, color="grey", linewidth=0.5)
    ax.axvline(0, color="grey", linewidth=0.5)

    puntos = 0
    for s in sismos:
        lon, lat = s.get("longitud"), s.get("latitud")
        if lon is None or lat is None:
            continue
        mag = _num(s.get("magnitud"), default=1.0)
        color = "#D32F2F" if mag >= 4 else ("#F9A825" if mag >= 2.5 else "#2E7D32")
        ax.scatter(lon, lat, s=max(10, mag * 25), color=color, alpha=0.7)
        puntos += 1

    if puntos == 0:
        ax.text(0, 0, "Sin datos de sismos disponibles\n(revisar conexion a USGS)",
                 ha="center", va="center", color="#888888")

    fig.tight_layout()
    ruta_mapa = tmp_dir / "mapa.png"
    fig.savefig(ruta_mapa, dpi=140)
    plt.close(fig)

    story.append(Image(str(ruta_mapa), width=16 * cm, height=9 * cm))
    story.append(Spacer(1, 0.8 * cm))
    return story


def bloque_eventos(datos: dict, estilos) -> list:
    story = [Paragraph("Ultimos Eventos Criticos (max. 40)", estilos["SIPIASubtitulo"])]

    filas = [["Sensor", "Severidad", "Score", "Motivo", "Detectado"]]
    for e in datos["eventos"][:40]:
        score = e.get("score")
        filas.append([
            _texto(e.get("sensor_id")),
            _texto(e.get("severity")),
            f"{_num(score):.2f}" if score is not None else "-",
            _texto(e.get("reason"), "")[:40],
            _texto(e.get("detected_at"), "")[:19],
        ])

    if len(filas) == 1:
        story.append(Paragraph("Sin eventos registrados.", estilos["Normal"]))
        return story

    tabla = Table(filas, colWidths=[2.2 * cm, 2.3 * cm, 1.8 * cm, 6 * cm, 3.7 * cm], repeatRows=1)
    estilo_filas = [
        ("BACKGROUND", (0, 0), (-1, 0), COLOR_PRIMARIO),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
    ]
    for i, fila in enumerate(filas[1:], start=1):
        if fila[1] == "CRITICAL":
            estilo_filas.append(("TEXTCOLOR", (1, i), (1, i), COLOR_CRITICO))
        elif fila[1] == "WARNING":
            estilo_filas.append(("TEXTCOLOR", (1, i), (1, i), COLOR_WARNING))
    tabla.setStyle(TableStyle(estilo_filas))
    story.append(tabla)
    return story


# ---------------------------------------------------------------------------
# Pie de pagina
# ---------------------------------------------------------------------------

def _pie_de_pagina(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.grey)
    texto_izq = f"SIPIA - Generado el {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    canvas.drawString(1.5 * cm, 1 * cm, texto_izq)
    canvas.drawRightString(A4[0] - 1.5 * cm, 1 * cm, f"Pagina {doc.page}")
    canvas.restoreState()


# ---------------------------------------------------------------------------
# Funcion principal
# ---------------------------------------------------------------------------

def genera_reporte_pdf(ruta_salida: str | None = None, incluir_mapa: bool = True) -> str:
    """Genera el reporte PDF completo y devuelve la ruta del archivo generado.

    Args:
        ruta_salida: ruta destino del PDF; si es None se genera un nombre
            con timestamp dentro de OUTPUT_DIR.
        incluir_mapa: si es False, omite la seccion de mapa de sismos
            (evita la llamada de red a USGS, util para generacion rapida).
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    ruta = ruta_salida or os.path.join(OUTPUT_DIR, f"SIPIA_Reporte_{ts}.pdf")

    inicio = datetime.now()
    datos = obtener_datos()
    estilos = _estilos()

    doc = SimpleDocTemplate(
        ruta, pagesize=A4,
        topMargin=1.5 * cm, bottomMargin=1.8 * cm,
        leftMargin=1.5 * cm, rightMargin=1.5 * cm,
        title="Reporte SIPIA",
    )

    # Las graficas se guardan como PNG temporales; se limpian automaticamente
    # al salir del bloque `with`, incluso si algo falla en el medio.
    with tempfile.TemporaryDirectory(prefix="sipia_charts_") as tmp:
        tmp_dir = Path(tmp)

        story = []
        story += bloque_portada(datos, estilos)
        story += bloque_resumen_estadistico(datos, estilos)
        story += bloque_tabla_lecturas(datos, estilos)
        try:
            story += bloque_graficas(datos, estilos, tmp_dir)
        except Exception as exc:
            logger.error("No se pudieron generar las graficas: %s", exc)
            story.append(Paragraph("No fue posible generar las graficas del sistema.", estilos["Normal"]))
        if incluir_mapa:
            try:
                story += bloque_mapa(datos, estilos, tmp_dir)
            except Exception as exc:
                logger.error("No se pudo generar el mapa de sismos: %s", exc)
                story.append(Paragraph("No fue posible generar el mapa de sismos.", estilos["Normal"]))
        story += bloque_eventos(datos, estilos)

        doc.build(story, onFirstPage=_pie_de_pagina, onLaterPages=_pie_de_pagina)

    duracion = (datetime.now() - inicio).total_seconds()
    logger.info("Reporte generado en %.2fs: %s", duracion, ruta)
    return ruta


if __name__ == "__main__":
    genera_reporte_pdf()
