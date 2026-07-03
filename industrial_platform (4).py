"""
industrial_platform.py --- Motor Principal de SIPIA
======================================================
Gestiona la recepcion de datos de sensores, el almacenamiento en SQLite,
la deteccion de anomalias y la exposicion de la API REST con FastAPI/Uvicorn.

Reconstruido a partir del Informe Tecnico SIPIA v1.0 (UAGRM - Bolivia).
"""

from __future__ import annotations

import logging
import math
import os
import sqlite3
import statistics
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, field_validator

# ---------------------------------------------------------------------------
# Configuracion (variables de entorno, ver .env.example)
# ---------------------------------------------------------------------------

DB_PATH = Path(os.environ.get("SIPIA_DB_PATH") or (Path(__file__).parent / "sipia_readings.db"))

# Si DATABASE_URL esta definida (Render/Neon en produccion), SIPIA usa
# Postgres. Si no existe (por ejemplo corriendo local en tu computadora),
# sigue usando SQLite exactamente como antes. Esto evita que tengas que
# instalar/configurar Postgres solo para desarrollar en tu maquina.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras

API_HOST = os.environ.get("SIPIA_API_HOST", "0.0.0.0")
API_PORT = int(os.environ.get("SIPIA_API_PORT", "8000"))

_cors_env = os.environ.get("SIPIA_CORS_ORIGINS", "").strip()
CORS_ORIGINS = [o.strip() for o in _cors_env.split(",") if o.strip()] or ["*"]

# Si se define, las escrituras (POST) requieren el header X-API-Key con este
# valor. Si se deja vacio, no se exige autenticacion (comodo para desarrollo
# local, pero se recomienda configurarla en produccion).
API_KEY = os.environ.get("SIPIA_API_KEY", "").strip()

# URL publica final del proyecto (una vez desplegado), usada en la landing page,
# el sitemap.xml y las etiquetas canonical/OpenGraph. Mientras se corre en local
# queda vacia y esas etiquetas simplemente se omiten.
PUBLIC_URL = os.environ.get("SIPIA_PUBLIC_URL", "").strip().rstrip("/")

SENSORES_CONOCIDOS = ["VIB-01", "ACCEL", "GYRO", "TEMP", "PRESS"]

# Umbrales de severidad por sensor (z-score sobre la ventana movil)
Z_SCORE_WARNING = 2.5
Z_SCORE_CRITICAL = 4.0
VENTANA_MOVIL = 50  # cantidad de lecturas usadas para calcular media/desvio

LIMITE_MAXIMO_CONSULTA = 1000

logging.basicConfig(
    level=os.environ.get("SIPIA_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("sipia.industrial_platform")


# ---------------------------------------------------------------------------
# Acceso a base de datos (SQLite en local / Postgres en produccion)
# ---------------------------------------------------------------------------

class _PGCursorCompat:
    """Envuelve un cursor de psycopg2 para que se comporte como uno de sqlite3
    (fetchone/fetchall + atributo lastrowid), asi el resto del codigo no
    necesita saber cual backend esta usando."""

    def __init__(self, cursor):
        self._cursor = cursor
        self.lastrowid: Optional[int] = None

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()


class _PGConnCompat:
    """Adapta una conexion psycopg2 para que conn.execute(...) funcione igual
    que en sqlite3.Connection.execute(...): traduce los placeholders '?' a
    '%s', y emula 'cursor.lastrowid' agregando RETURNING en los INSERT sobre
    la tabla readings (el unico lugar del codigo que lo necesita)."""

    def __init__(self, pg_conn):
        self._conn = pg_conn

    def execute(self, query: str, params=()):
        sql = query.replace("?", "%s")
        es_insert_readings = (
            "INSERT INTO READINGS" in sql.upper() and "RETURNING" not in sql.upper()
        )
        if es_insert_readings:
            sql += " RETURNING reading_id"
        cur = self._conn.cursor()
        cur.execute(sql, params)
        compat = _PGCursorCompat(cur)
        if es_insert_readings:
            fila = cur.fetchone()
            compat.lastrowid = fila["reading_id"] if fila else None
        return compat

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


@contextmanager
def get_conn():
    if USE_POSTGRES:
        conn_cruda = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        conn = _PGConnCompat(conn_cruda)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
    else:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        # WAL permite lecturas concurrentes mientras se escribe, y el busy_timeout
        # evita errores "database is locked" bajo carga de sensores frecuente.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init_db() -> None:
    """Crea las tablas readings y events si no existen (sintaxis compatible
    con SQLite o con Postgres, segun cual backend este activo)."""
    if USE_POSTGRES:
        ddl_readings = """
            CREATE TABLE IF NOT EXISTS readings (
                reading_id SERIAL PRIMARY KEY,
                sensor_id  TEXT NOT NULL,
                value      DOUBLE PRECISION NOT NULL,
                unit       TEXT NOT NULL,
                timestamp  TEXT NOT NULL
            )
        """
        ddl_events = """
            CREATE TABLE IF NOT EXISTS events (
                event_id    SERIAL PRIMARY KEY,
                sensor_id   TEXT NOT NULL,
                reading_id  INTEGER NOT NULL REFERENCES readings(reading_id),
                severity    TEXT NOT NULL,
                score       DOUBLE PRECISION NOT NULL,
                reason      TEXT,
                detected_at TEXT NOT NULL
            )
        """
    else:
        ddl_readings = """
            CREATE TABLE IF NOT EXISTS readings (
                reading_id INTEGER PRIMARY KEY AUTOINCREMENT,
                sensor_id  TEXT NOT NULL,
                value      REAL NOT NULL,
                unit       TEXT NOT NULL,
                timestamp  TEXT NOT NULL
            )
        """
        ddl_events = """
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

    with get_conn() as conn:
        conn.execute(ddl_readings)
        conn.execute(ddl_events)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_readings_sensor ON readings(sensor_id, timestamp)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_severity ON events(severity, detected_at)"
        )
    log.info(
        "Base de datos inicializada (%s)",
        "Postgres/Neon" if USE_POSTGRES else DB_PATH,
    )


# ---------------------------------------------------------------------------
# Modelos Pydantic (API)
# ---------------------------------------------------------------------------

class LecturaIn(BaseModel):
    sensor_id: str
    value: float
    unit: str

    @field_validator("sensor_id")
    @classmethod
    def _sensor_no_vacio(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("sensor_id no puede estar vacio")
        return v

    @field_validator("value")
    @classmethod
    def _valor_finito(cls, v: float) -> float:
        if math.isnan(v) or math.isinf(v):
            raise ValueError("value debe ser un numero finito (no NaN/Infinity)")
        return v

    @field_validator("unit")
    @classmethod
    def _unit_no_vacia(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("unit no puede estar vacia")
        return v


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


class SilenciarIn(BaseModel):
    silenciar: bool


class VolumenIn(BaseModel):
    volumen: float

    @field_validator("volumen")
    @classmethod
    def _volumen_en_rango(cls, v: float) -> float:
        if not 0 <= v <= 1:
            raise ValueError("volumen debe estar entre 0 y 1")
        return v


# ---------------------------------------------------------------------------
# Estado de alertas en memoria (silenciar / volumen)
# ---------------------------------------------------------------------------

class EstadoAlertas:
    def __init__(self) -> None:
        self.silenciado = False
        self.volumen = float(os.environ.get("SIPIA_ALERTA_VOLUMEN", "80")) / 100


estado_alertas = EstadoAlertas()


# ---------------------------------------------------------------------------
# Autenticacion opcional para endpoints de escritura
# ---------------------------------------------------------------------------

def verificar_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    if not API_KEY:
        return  # sin API key configurada, no se exige autenticacion
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="X-API-Key invalida o ausente")


# ---------------------------------------------------------------------------
# Deteccion de anomalias
# ---------------------------------------------------------------------------

def _historial_reciente(conn, sensor_id: str, limite: int = VENTANA_MOVIL) -> list[float]:
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
    desvio_real = statistics.pstdev(datos)

    # Piso minimo de desvio proporcional a la escala tipica del sensor (no un
    # numero fijo microscopico como 1e-6). Sin esto, cuando el historial es
    # casi constante (ej. VIB-01 sin sismos recientes, o PRESS estable), el
    # pstdev real tiende a 0 y dividir por un piso demasiado chico dispara
    # Z-Scores absurdos (cientos de miles) que generan falsas alarmas CRITICAL.
    # Usamos el 1% del valor absoluto tipico de los datos como referencia,
    # con un piso absoluto de 1e-3 para sensores centrados en cero (ACCEL, GYRO).
    escala = abs(media) if abs(media) > 1e-9 else (max((abs(v) for v in datos), default=1.0) or 1.0)
    piso_desvio = max(escala * 0.01, 1e-3)
    desvio = max(desvio_real, piso_desvio)

    return (valor - media) / desvio


def clasificar_severidad(z: float) -> Optional[str]:
    az = abs(z)
    if az >= Z_SCORE_CRITICAL:
        return "CRITICAL"
    if az >= Z_SCORE_WARNING:
        return "WARNING"
    return None


def procesar_lectura(conn, lectura: LecturaIn) -> tuple[int, Optional[dict]]:
    """Guarda una lectura y evalua si genera un evento de anomalia."""
    historial = _historial_reciente(conn, lectura.sensor_id)
    z = calcular_zscore(lectura.value, historial)

    ts = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO readings (sensor_id, value, unit, timestamp) VALUES (?, ?, ?, ?)",
        (lectura.sensor_id, lectura.value, lectura.unit, ts),
    )
    reading_id = cur.lastrowid

    severidad = clasificar_severidad(z)
    evento = None
    if severidad:
        razon = f"Z-Score={z:.2f} fuera de rango normal para {lectura.sensor_id}"
        detected_at = datetime.now(timezone.utc).isoformat()
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


def _notificar_evento(evento: dict) -> None:
    """Envia la alerta por Telegram si el modulo esta disponible. Nunca lanza excepcion."""
    try:
        from telegram_alerts import enviar_alerta
        enviar_alerta(evento)
    except ImportError:
        log.debug("Modulo telegram_alerts no disponible; se omite notificacion externa")
    except Exception as exc:  # pragma: no cover
        log.warning("No se pudo enviar alerta Telegram: %s", exc)


# ---------------------------------------------------------------------------
# API REST (FastAPI)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    log.info(
        "SIPIA API lista (DB: %s, CORS: %s)",
        "Postgres/Neon" if USE_POSTGRES else DB_PATH,
        CORS_ORIGINS,
    )
    yield


app = FastAPI(
    title="SIPIA API",
    version="1.0.0",
    description="Sistema de Monitoreo Sismico Inteligente",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _valor_serializable(obj):
    """Convierte un valor arbitrario de un error de pydantic a algo JSON-serializable.

    Necesario porque si un cliente envia value=NaN/Infinity, el detalle de
    error de FastAPI incluye ese valor invalido tal cual (json.dumps no
    puede serializar NaN/Infinity), y ademas el campo 'ctx' puede traer la
    excepcion Python original (tampoco serializable). Sin esto, el intento
    de devolver un 422 limpio terminaba en un 500 al construir la respuesta.
    """
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return str(obj)
    if isinstance(obj, BaseException):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _valor_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_valor_serializable(v) for v in obj]
    return obj


@app.exception_handler(RequestValidationError)
async def manejador_errores_validacion(request: Request, exc: RequestValidationError):
    errores = [
        {"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")}
        for e in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": _valor_serializable(errores)})


_LANDING_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SIPIA — Sistema de Monitoreo Sismico Inteligente</title>
<meta name="description" content="SIPIA es un sistema de monitoreo sismico inteligente que integra sensores fisicos en tiempo real, datos publicos del USGS, deteccion de anomalias con Z-Score y alertas automaticas por Telegram y audio.">
<meta name="keywords" content="SIPIA, monitoreo sismico, sismos Bolivia, deteccion de anomalias, USGS, sensores IoT, UAGRM">
{canonical_tag}
<meta property="og:title" content="SIPIA — Sistema de Monitoreo Sismico Inteligente">
<meta property="og:description" content="Monitoreo sismico en tiempo real con sensores fisicos, datos del USGS y alertas automaticas.">
<meta property="og:type" content="website">
<style>
  :root {{ color-scheme: dark; }}
  body {{
    font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
    max-width: 760px;
    margin: 0 auto;
    padding: 2.5rem 1.5rem 4rem;
    background: #0f1115;
    color: #e6e6e6;
    line-height: 1.6;
  }}
  h1 {{ font-size: 1.9rem; margin-bottom: 0.2rem; }}
  .subtitulo {{ color: #9aa0a6; margin-top: 0; margin-bottom: 2rem; }}
  h2 {{ font-size: 1.15rem; margin-top: 2.2rem; border-bottom: 1px solid #2a2d33; padding-bottom: 0.4rem; }}
  ul {{ padding-left: 1.2rem; }}
  li {{ margin-bottom: 0.5rem; }}
  a {{ color: #7dc4ff; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .enlaces a {{
    display: inline-block;
    margin: 0.3rem 0.6rem 0.3rem 0;
    padding: 0.5rem 0.9rem;
    border: 1px solid #2a2d33;
    border-radius: 6px;
  }}
  footer {{ margin-top: 3rem; color: #6b6f76; font-size: 0.85rem; }}
</style>
</head>
<body>
  <h1>SIPIA</h1>
  <p class="subtitulo">Sistema de Monitoreo Sismico Inteligente</p>

  <p>
    SIPIA integra sensores fisicos (vibracion, aceleracion, giroscopio, temperatura
    y presion), datos sismicos publicos del USGS (United States Geological Survey)
    y un motor de deteccion de anomalias basado en Z-Score sobre ventana movil,
    para generar alertas en tiempo real cuando se detecta actividad fuera de lo normal.
  </p>

  <h2>Funcionalidades</h2>
  <ul>
    <li>Lectura continua de sensores fisicos y del feed sismico del USGS</li>
    <li>Deteccion automatica de anomalias (severidad WARNING / CRITICAL)</li>
    <li>Alertas por Telegram y sonido en tiempo real</li>
    <li>Reportes en PDF generados bajo demanda</li>
    <li>Dashboard con graficas y mapa de sismos</li>
    <li>API REST documentada (FastAPI / OpenAPI)</li>
  </ul>

  <h2>Explorar el sistema</h2>
  <p class="enlaces">
    <a href="/docs">Documentacion de la API</a>
    <a href="/estado">Estado del sistema</a>
    <a href="/eventos">Eventos detectados</a>
    <a href="/lecturas">Ultimas lecturas</a>
  </p>

  <footer>
    SIPIA &mdash; proyecto academico UAGRM (Universidad Autonoma Gabriel Rene Moreno), Bolivia.
  </footer>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def landing_page():
    """Pagina de inicio en HTML (no JSON) para que motores de busqueda como
    Google puedan indexar el proyecto con un titulo, descripcion y contenido
    real. La info programatica que antes vivia aca se movio a /api."""
    canonical_tag = f'<link rel="canonical" href="{PUBLIC_URL}/">' if PUBLIC_URL else ""
    return _LANDING_HTML_TEMPLATE.format(canonical_tag=canonical_tag)


@app.get("/api")
def info_api():
    """Info programatica del proyecto (lo que antes devolvia la raiz '/')."""
    return {"proyecto": "SIPIA", "version": "1.0.0", "estado": "en produccion"}


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots_txt():
    """Le indica a los motores de busqueda que pueden rastrear el sitio y
    donde esta el sitemap. Sin esto, algunos crawlers son mas cautelosos."""
    lineas = ["User-agent: *", "Allow: /"]
    if PUBLIC_URL:
        lineas.append(f"Sitemap: {PUBLIC_URL}/sitemap.xml")
    return "\n".join(lineas) + "\n"


@app.get("/sitemap.xml")
def sitemap_xml():
    """Sitemap minimo con las paginas publicas relevantes para indexar.
    Requiere SIPIA_PUBLIC_URL configurada (no tiene sentido con localhost)."""
    if not PUBLIC_URL:
        raise HTTPException(
            status_code=404,
            detail="Configura SIPIA_PUBLIC_URL para habilitar el sitemap (no aplica en localhost)",
        )
    paginas = ["/", "/docs", "/estado"]
    urls = "".join(f"<url><loc>{PUBLIC_URL}{p}</loc></url>" for p in paginas)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    return Response(content=xml, media_type="application/xml")


@app.get("/salud")
def salud():
    """Chequeo simple de disponibilidad, util para monitoreo/orquestadores."""
    return {
        "ok": True,
        "backend_db": "postgres" if USE_POSTGRES else "sqlite",
        "db_existe": True if USE_POSTGRES else DB_PATH.exists(),
    }


@app.post("/lecturas", response_model=LecturaOut, dependencies=[Depends(verificar_api_key)])
def crear_lectura(lectura: LecturaIn):
    with get_conn() as conn:
        reading_id, evento = procesar_lectura(conn, lectura)
        row = conn.execute(
            "SELECT * FROM readings WHERE reading_id = ?", (reading_id,)
        ).fetchone()

    if evento:
        _notificar_evento(evento)

    return dict(row)


@app.get("/lecturas", response_model=list[LecturaOut])
def listar_lecturas(
    sensor_id: Optional[str] = None,
    limite: int = Query(default=100, ge=1, le=LIMITE_MAXIMO_CONSULTA),
):
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
def listar_eventos(
    severity: Optional[Literal["CRITICAL", "WARNING"]] = None,
    limite: int = Query(default=40, ge=1, le=LIMITE_MAXIMO_CONSULTA),
):
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
        "silenciado": estado_alertas.silenciado,
        "volumen": estado_alertas.volumen,
    }


@app.post("/alertas/silenciar")
def silenciar_alertas(datos: SilenciarIn):
    """Activa o desactiva las alertas sonoras. Usado por el boton del dashboard."""
    estado_alertas.silenciado = datos.silenciar
    log.info("Alertas %s", "silenciadas" if datos.silenciar else "activadas")
    return {"silenciado": estado_alertas.silenciado}


@app.post("/alertas/volumen")
def ajustar_volumen(datos: VolumenIn):
    """Ajusta el volumen (0-1) de las alertas sonoras. Usado por el slider del dashboard."""
    estado_alertas.volumen = datos.volumen
    log.info("Volumen de alertas ajustado a %.2f", datos.volumen)
    return {"volumen": estado_alertas.volumen}


@app.get("/reporte")
def endpoint_reporte():
    """Genera un reporte PDF y lo devuelve como archivo descargable.

    Antes este endpoint devolvia solo la ruta del archivo en el servidor
    (un JSON), lo cual no sirve para el boton "Exportar PDF" del dashboard,
    que espera abrir directamente el binario del PDF.
    """
    try:
        from generar_reporte import genera_reporte_pdf
    except ImportError as exc:
        raise HTTPException(status_code=500, detail=f"Modulo de reportes no disponible: {exc}")

    try:
        ruta = genera_reporte_pdf()
    except Exception as exc:
        log.exception("Fallo generando el reporte PDF")
        raise HTTPException(status_code=500, detail=f"No se pudo generar el reporte: {exc}")

    return FileResponse(
        path=ruta,
        media_type="application/pdf",
        filename=Path(ruta).name,
    )


# ---------------------------------------------------------------------------
# Punto de entrada directo (uvicorn)
# ---------------------------------------------------------------------------

def main() -> None:
    """Arranca el servidor FastAPI/Uvicorn (host/puerto configurables via .env)."""
    import uvicorn
    import os

    # Si está en Render usa su puerto, si no, usa el tuyo por defecto
    puerto_render = int(os.environ.get("PORT", API_PORT))

    log.info("Iniciando SIPIA API REST")
    uvicorn.run(app, host="0.0.0.0", port=puerto_render, log_level=os.environ.get("SIPIA_LOG_LEVEL", "info").lower())
