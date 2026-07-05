"""
industrial_platform.py --- Motor Principal de SIPIA
======================================================
Gestiona la recepcion de datos de sensores, el almacenamiento en SQLite,
la deteccion de anomalias y la exposicion de la API REST con FastAPI/Uvicorn.

Reconstruido a partir del Informe Tecnico SIPIA v1.0 (UAGRM - Bolivia).
"""

from __future__ import annotations

import logging
import sqlite3
import statistics
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------

DB_PATH = Path(__file__).parent / "sipia_readings.db"

SENSORES_CONOCIDOS = ["VIB-01", "ACCEL", "GYRO", "TEMP", "PRESS"]

# Umbrales de severidad por sensor (z-score sobre la ventana movil)
Z_SCORE_WARNING = 2.5
Z_SCORE_CRITICAL = 4.0
VENTANA_MOVIL = 50  # cantidad de lecturas usadas para calcular media/desvio

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("sipia.industrial_platform")


# ---------------------------------------------------------------------------
# Acceso a base de datos (SQLite)
# ---------------------------------------------------------------------------

@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Crea las tablas readings y events si no existen."""
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS readings (
                reading_id INTEGER PRIMARY KEY AUTOINCREMENT,
                sensor_id  TEXT NOT NULL,
                value      REAL NOT NULL,
                unit       TEXT NOT NULL,
                timestamp  TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                sensor_id   TEXT NOT NULL,
                reading_id  INTEGER NOT NULL,
                severity    TEXT NOT NULL,
                score       REAL NOT NULL,
                reason      TEXT,
                detected_at TEXT NOT NULL,
                FOREIGN KEY (reading_id) REFERENCES readings(reading_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_readings_sensor ON readings(sensor_id, timestamp)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_severity ON events(severity, detected_at)"
        )
    log.info("Base de datos inicializada en %s", DB_PATH)


# ---------------------------------------------------------------------------
# Modelos Pydantic (API)
# ---------------------------------------------------------------------------

class LecturaIn(BaseModel):
    sensor_id: str
    value: float
    unit: str


class LecturaOut(BaseModel):
    reading_id: int
    sensor_id: str
    value: float
    unit: str
    timestamp: str


class EventoOut(BaseModel):
    event_id: int
    sensor_id: str
    reading_id: int
    severity: str
    score: float
    reason: Optional[str]
    detected_at: str


# ---------------------------------------------------------------------------
# Deteccion de anomalias
# ---------------------------------------------------------------------------

def _historial_reciente(conn: sqlite3.Connection, sensor_id: str, limite: int = VENTANA_MOVIL) -> list[float]:
    rows = conn.execute(
        "SELECT value FROM readings WHERE sensor_id = ? ORDER BY reading_id DESC LIMIT ?",
        (sensor_id, limite),
    ).fetchall()
    return [r["value"] for r in rows]


def calcular_zscore(valor: float, historial: Iterable[float]) -> float:
    datos = list(historial)
    if len(datos) < 5:
        return 0.0
    media = statistics.mean(datos)
    desvio = statistics.pstdev(datos) or 1e-6
    return (valor - media) / desvio


def clasificar_severidad(z: float) -> Optional[str]:
    az = abs(z)
    if az >= Z_SCORE_CRITICAL:
        return "CRITICAL"
    if az >= Z_SCORE_WARNING:
        return "WARNING"
    return None


def procesar_lectura(conn: sqlite3.Connection, lectura: LecturaIn) -> tuple[int, Optional[dict]]:
    """Guarda una lectura y evalua si genera un evento de anomalia."""
    historial = _historial_reciente(conn, lectura.sensor_id)
    z = calcular_zscore(lectura.value, historial)

    ts = datetime.utcnow().isoformat()
    cur = conn.execute(
        "INSERT INTO readings (sensor_id, value, unit, timestamp) VALUES (?, ?, ?, ?)",
        (lectura.sensor_id, lectura.value, lectura.unit, ts),
    )
    reading_id = cur.lastrowid

    severidad = clasificar_severidad(z)
    evento = None
    if severidad:
        razon = f"Z-Score={z:.2f} fuera de rango normal para {lectura.sensor_id}"
        detected_at = datetime.utcnow().isoformat()
        conn.execute(
            """INSERT INTO events (sensor_id, reading_id, severity, score, reason, detected_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (lectura.sensor_id, reading_id, severidad, z, razon, detected_at),
        )
        evento = {
            "sensor_id": lectura.sensor_id,
            "reading_id": reading_id,
            "severity": severidad,
            "score": z,
            "reason": razon,
            "detected_at": detected_at,
        }
        log.warning("Anomalia detectada: %s", evento)

    return reading_id, evento


# ---------------------------------------------------------------------------
# API REST (FastAPI)
# ---------------------------------------------------------------------------

app = FastAPI(title="SIPIA API", version="1.0.0", description="Sistema de Monitoreo Sismico Inteligente")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/")
def root():
    return {"proyecto": "SIPIA", "version": "1.0.0", "estado": "en produccion"}


@app.post("/lecturas", response_model=LecturaOut)
def crear_lectura(lectura: LecturaIn):
    with get_conn() as conn:
        reading_id, evento = procesar_lectura(conn, lectura)
        row = conn.execute(
            "SELECT * FROM readings WHERE reading_id = ?", (reading_id,)
        ).fetchone()

    if evento:
        # Notificaciones externas (Telegram / audio) se disparan desde sipia_real.py
        try:
            from telegram_alerts import enviar_alerta
            enviar_alerta(evento)
        except Exception as exc:  # pragma: no cover
            log.debug("No se pudo enviar alerta Telegram: %s", exc)

    return dict(row)


@app.get("/lecturas", response_model=list[LecturaOut])
def listar_lecturas(sensor_id: Optional[str] = None, limite: int = 100):
    query = "SELECT * FROM readings"
    params: list = []
    if sensor_id:
        query += " WHERE sensor_id = ?"
        params.append(sensor_id)
    query += " ORDER BY reading_id DESC LIMIT ?"
    params.append(limite)
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


@app.get("/eventos", response_model=list[EventoOut])
def listar_eventos(severity: Optional[str] = None, limite: int = 40):
    query = "SELECT * FROM events"
    params: list = []
    if severity:
        query += " WHERE severity = ?"
        params.append(severity)
    query += " ORDER BY event_id DESC LIMIT ?"
    params.append(limite)
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


@app.get("/estado")
def estado_sistema():
    with get_conn() as conn:
        total_lecturas = conn.execute("SELECT COUNT(*) c FROM readings").fetchone()["c"]
        total_eventos = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
        sensores_activos = conn.execute(
            "SELECT DISTINCT sensor_id FROM readings"
        ).fetchall()
    return {
        "lecturas": total_lecturas,
        "eventos": total_eventos,
        "sensores_activos": [r["sensor_id"] for r in sensores_activos],
    }


@app.get("/reporte")
def endpoint_reporte():
    """Genera y devuelve la ruta de un reporte PDF (ver generar_reporte.py)."""
    try:
        from generar_reporte import genera_reporte_pdf
    except ImportError as exc:
        raise HTTPException(status_code=500, detail=f"Modulo de reportes no disponible: {exc}")
    ruta = genera_reporte_pdf()
    return {"reporte": str(ruta)}


# --- Control de alertas sonoras desde el dashboard -------------------------
# Nota: el audio real se reproduce en el proceso de sipia_real.py (que tiene
# la instancia de SistemaAlertas). Estos endpoints permiten que el dashboard
# no falle al llamarlos aunque ese proceso corra aparte; el sonido en el
# navegador ya se reproduce localmente via Web Audio API como respaldo.

class SilenciarIn(BaseModel):
    silenciar: bool = True


class VolumenIn(BaseModel):
    volumen: float = 0.8


@app.post("/alertas/silenciar")
def alertas_silenciar(body: SilenciarIn):
    log.info("Solicitud de silenciar alertas: %s", body.silenciar)
    return {"silenciado": body.silenciar}


@app.post("/alertas/volumen")
def alertas_volumen(body: VolumenIn):
    vol = max(0.0, min(1.0, body.volumen))
    log.info("Solicitud de ajuste de volumen: %.2f", vol)
    return {"volumen": vol}


@app.post("/alertas/test/{tipo}")
def alertas_test(tipo: str):
    log.info("Prueba de alerta solicitada desde el dashboard: %s", tipo)
    return {"ok": True, "tipo": tipo}


# ---------------------------------------------------------------------------
# Punto de entrada directo (uvicorn)
# ---------------------------------------------------------------------------

def main() -> None:
    """Arranca el servidor FastAPI/Uvicorn en el puerto 8000."""
    import uvicorn

    init_db()
    log.info("Iniciando SIPIA API REST en http://0.0.0.0:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")


if __name__ == "__main__":
    main()
