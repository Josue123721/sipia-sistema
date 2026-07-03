"""
real_sensors.py --- Sensores Reales
=====================================
Conecta SIPIA con sensores fisicos reales (acelerometro, giroscopio,
temperatura y presion) e integra datos sismicos de la API publica del
USGS (United States Geological Survey).

- Sensor VIB-01: monitoreo sismico en tiempo real via API USGS
- Datos de sismos mundiales con magnitud, ubicacion, coordenadas,
  nivel de alerta y bandera de tsunami
- Cache con TTL para no saturar la API del USGS
- Reintentos automaticos con backoff exponencial ante fallas de red
- Conexiones serie persistentes (no se abre/cierra el puerto en cada lectura)
- Fallback automatico a datos simulados si el sensor o la API fallan
- Configuracion por variables de entorno (puertos, baudrates, intervalos)
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("sipia.real_sensors")
log.addHandler(logging.NullHandler())

__all__ = [
    "LecturaSensor",
    "obtener_sismos_usgs",
    "leer_vib01",
    "leer_accel",
    "leer_gyro",
    "leer_temp",
    "leer_press",
    "leer_todos_los_sensores",
    "real_sensor_fleet",
    "clasificar_magnitud",
]

# ---------------------------------------------------------------------------
# Configuracion (via variables de entorno, con defaults sensatos)
# ---------------------------------------------------------------------------

USGS_FEED_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson"

INTERVALO_DEFECTO_SEG = int(os.getenv("SIPIA_SENSOR_INTERVALO", "10"))
USGS_TIMEOUT_SEG = int(os.getenv("SIPIA_USGS_TIMEOUT", "8"))
USGS_CACHE_TTL_SEG = int(os.getenv("SIPIA_USGS_CACHE_TTL", "20"))

PUERTOS = {
    "ACCEL": os.getenv("SIPIA_PUERTO_ACCEL", "COM3"),
    "GYRO": os.getenv("SIPIA_PUERTO_GYRO", "COM4"),
    "TEMP": os.getenv("SIPIA_PUERTO_TEMP", "COM5"),
    "PRESS": os.getenv("SIPIA_PUERTO_PRESS", "COM6"),
}
BAUDRATE = int(os.getenv("SIPIA_SERIAL_BAUDRATE", "9600"))


@dataclass
class LecturaSensor:
    sensor_id: str
    value: float
    unit: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    meta: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Sesion HTTP con reintentos + backoff, y cache simple con TTL
# ---------------------------------------------------------------------------

def _crear_sesion() -> requests.Session:
    sesion = requests.Session()
    reintentos = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    adaptador = HTTPAdapter(max_retries=reintentos)
    sesion.mount("https://", adaptador)
    sesion.mount("http://", adaptador)
    return sesion


_SESION_HTTP = _crear_sesion()
_cache_lock = threading.Lock()
_cache: dict = {"expira": 0.0, "sismos": []}


def clasificar_magnitud(magnitud: float) -> str:
    """Clasifica una magnitud Richter en una etiqueta descriptiva (escala USGS)."""
    if magnitud is None:
        return "desconocida"
    if magnitud < 2.0:
        return "micro"
    if magnitud < 4.0:
        return "menor"
    if magnitud < 5.0:
        return "ligero"
    if magnitud < 6.0:
        return "moderado"
    if magnitud < 7.0:
        return "fuerte"
    if magnitud < 8.0:
        return "mayor"
    return "gran_terremoto"


# ---------------------------------------------------------------------------
# VIB-01 --- Sensor sismico via USGS
# ---------------------------------------------------------------------------

def obtener_sismos_usgs(limite: int = 20, usar_cache: bool = True) -> list[dict]:
    """Consulta la API publica del USGS y devuelve los sismos mas recientes.

    Usa una cache con TTL (SIPIA_USGS_CACHE_TTL, default 20s) para evitar
    saturar la API cuando se llama con frecuencia, y reintentos con backoff
    exponencial ante fallas transitorias de red.
    """
    ahora = time.monotonic()
    with _cache_lock:
        if usar_cache and ahora < _cache["expira"] and _cache["sismos"]:
            return _cache["sismos"][:limite]

    try:
        resp = _SESION_HTTP.get(USGS_FEED_URL, timeout=USGS_TIMEOUT_SEG)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.warning("Fallo consulta USGS (%s); usando datos simulados", exc)
        return _sismos_simulados(limite)

    sismos = []
    for feature in data.get("features", []):
        props = feature.get("properties", {})
        coords = feature.get("geometry", {}).get("coordinates", [None, None, None])
        magnitud = props.get("mag")
        sismos.append(
            {
                "magnitud": magnitud,
                "clasificacion": clasificar_magnitud(magnitud),
                "lugar": props.get("place"),
                "tiempo": props.get("time"),
                "longitud": coords[0],
                "latitud": coords[1],
                "profundidad_km": coords[2],
                "alerta": props.get("alert"),
                "tsunami": bool(props.get("tsunami")),
            }
        )

    with _cache_lock:
        _cache["sismos"] = sismos
        _cache["expira"] = ahora + USGS_CACHE_TTL_SEG

    return sismos[:limite]


def _sismos_simulados(limite: int) -> list[dict]:
    lugares = [
        "Costa de Chile", "Sur de Peru", "Bolivia - Cordillera",
        "Islas Salomon", "Japon - Honshu", "California, EEUU",
    ]
    resultado = []
    for _ in range(limite):
        magnitud = round(random.uniform(1.5, 5.5), 1)
        resultado.append(
            {
                "magnitud": magnitud,
                "clasificacion": clasificar_magnitud(magnitud),
                "lugar": random.choice(lugares),
                "tiempo": int(time.time() * 1000),
                "longitud": round(random.uniform(-180, 180), 3),
                "latitud": round(random.uniform(-60, 60), 3),
                "profundidad_km": round(random.uniform(5, 120), 1),
                "alerta": None,
                "tsunami": False,
            }
        )
    return resultado


def leer_vib01() -> LecturaSensor:
    """Convierte el sismo mas reciente del USGS en una lectura del sensor VIB-01."""
    sismos = obtener_sismos_usgs(limite=1)
    if sismos:
        magnitud = sismos[0]["magnitud"] or 0.0
        meta = sismos[0]
    else:
        magnitud = 0.0
        meta = {}
    return LecturaSensor(sensor_id="VIB-01", value=float(magnitud), unit="Richter", meta=meta)


# ---------------------------------------------------------------------------
# Sensores fisicos (acelerometro, giroscopio, temperatura, presion)
# ---------------------------------------------------------------------------
# Las conexiones serie se mantienen abiertas entre lecturas (en vez de
# abrir/cerrar el puerto COM en cada llamada), lo cual es mas rapido y
# reduce el riesgo de errores de acceso al dispositivo en Windows.

_conexiones_serie: dict[str, "object"] = {}
_serie_lock = threading.Lock()


def _obtener_conexion_serie(puerto: str, baudrate: int):
    try:
        import serial  # pyserial
    except ImportError:
        return None

    with _serie_lock:
        conexion = _conexiones_serie.get(puerto)
        if conexion is not None and getattr(conexion, "is_open", False):
            return conexion

        try:
            conexion = serial.Serial(puerto, baudrate, timeout=1)
            _conexiones_serie[puerto] = conexion
            return conexion
        except Exception as exc:
            log.debug("No se pudo abrir el puerto %s (%s); se usara fallback simulado", puerto, exc)
            _conexiones_serie.pop(puerto, None)
            return None


def _leer_puerto_serie(puerto: str, baudrate: int = BAUDRATE) -> Optional[float]:
    """Lee un valor crudo desde un puerto serie USB, reutilizando la conexion abierta."""
    conexion = _obtener_conexion_serie(puerto, baudrate)
    if conexion is None:
        return None
    try:
        linea = conexion.readline().decode(errors="ignore").strip()
        return float(linea) if linea else None
    except Exception as exc:
        log.debug("Error leyendo %s (%s); se cerrara la conexion y se reintentara luego", puerto, exc)
        with _serie_lock:
            try:
                conexion.close()
            except Exception:
                pass
            _conexiones_serie.pop(puerto, None)
        return None


def cerrar_conexiones_serie() -> None:
    """Cierra todas las conexiones serie abiertas. Util al finalizar el proceso."""
    with _serie_lock:
        for puerto, conexion in list(_conexiones_serie.items()):
            try:
                conexion.close()
            except Exception:
                pass
        _conexiones_serie.clear()


def leer_accel() -> LecturaSensor:
    valor = _leer_puerto_serie(PUERTOS["ACCEL"])
    if valor is None:
        valor = round(random.uniform(-2.0, 2.0), 3)
    return LecturaSensor(sensor_id="ACCEL", value=valor, unit="g")


def leer_gyro() -> LecturaSensor:
    valor = _leer_puerto_serie(PUERTOS["GYRO"])
    if valor is None:
        valor = round(random.uniform(-250, 250), 2)
    return LecturaSensor(sensor_id="GYRO", value=valor, unit="deg/s")


def leer_temp() -> LecturaSensor:
    valor = _leer_puerto_serie(PUERTOS["TEMP"])
    if valor is None:
        valor = round(random.gauss(24, 1.5), 2)
    return LecturaSensor(sensor_id="TEMP", value=valor, unit="C")


def leer_press() -> LecturaSensor:
    valor = _leer_puerto_serie(PUERTOS["PRESS"])
    if valor is None:
        valor = round(random.gauss(1013, 4), 2)
    return LecturaSensor(sensor_id="PRESS", value=valor, unit="hPa")


LECTORES: dict[str, Callable[[], LecturaSensor]] = {
    "VIB-01": leer_vib01,
    "ACCEL": leer_accel,
    "GYRO": leer_gyro,
    "TEMP": leer_temp,
    "PRESS": leer_press,
}


def leer_todos_los_sensores() -> list[LecturaSensor]:
    lecturas = []
    for sensor_id, lector in LECTORES.items():
        try:
            lecturas.append(lector())
        except Exception as exc:
            log.error("Error leyendo sensor %s: %s", sensor_id, exc)
    return lecturas


# ---------------------------------------------------------------------------
# Fleet --- bucle continuo de lectura, usado por sipia_real.py
# ---------------------------------------------------------------------------

def real_sensor_fleet(
    on_lectura: Optional[Callable[[LecturaSensor], None]] = None,
    intervalo_seg: int = INTERVALO_DEFECTO_SEG,
    detener: Optional[Callable[[], bool]] = None,
) -> None:
    """Bucle infinito que lee todos los sensores periodicamente.

    Si se provee `on_lectura`, se invoca por cada lectura obtenida
    (tipicamente para enviarla a industrial_platform via API REST).

    El chequeo de `detener` se hace en pasos de hasta 1s durante la espera,
    para que el bucle reaccione rapido a una senal de parada aunque el
    intervalo configurado sea largo.
    """
    log.info("Iniciando flota de sensores reales (intervalo=%ss)", intervalo_seg)
    try:
        while True:
            if detener and detener():
                log.info("Deteniendo flota de sensores por senal externa")
                break
            for lectura in leer_todos_los_sensores():
                log.info("Lectura: %s=%s %s", lectura.sensor_id, lectura.value, lectura.unit)
                if on_lectura:
                    on_lectura(lectura)

            espera_restante = intervalo_seg
            while espera_restante > 0:
                if detener and detener():
                    log.info("Deteniendo flota de sensores por senal externa (durante espera)")
                    return
                paso = min(1, espera_restante)
                time.sleep(paso)
                espera_restante -= paso
    finally:
        cerrar_conexiones_serie()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for l in leer_todos_los_sensores():
        print(l)
