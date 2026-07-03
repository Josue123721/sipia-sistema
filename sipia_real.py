"""
sipia_real.py --- Punto de Entrada
=====================================
Integracion de SIPIA con sensores reales y sistema de alertas.
Este es el script principal: arranca la API REST, la flota de sensores
reales y el monitor de alertas sonoras/Telegram, todo en un mismo proceso.

Uso:
    python sipia_real.py                  # todo activo
    python sipia_real.py --no-monitor      # sin monitor de audio
    python sipia_real.py --flask-routes    # expone tambien rutas Flask de alertas

Variables de entorno:
    SIPIA_API_URL       URL base de la API REST (default http://127.0.0.1:8000)
    SIPIA_LOG_FILE       Ruta de archivo de log (default sipia.log; vacio = solo consola)
    SIPIA_MAX_REINTENTOS Reintentos maximos ante caida inesperada de la flota (default 5)
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from typing import Optional

import industrial_platform as _orig
from real_sensors import real_sensor_fleet, LecturaSensor
from telegram_alerts import enviar_alerta
from sounds.alerta_sonido import SistemaAlertas, crear_rutas_flask

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------

API_URL = os.getenv("SIPIA_API_URL", "http://127.0.0.1:8000")
LOG_FILE = os.getenv("SIPIA_LOG_FILE", "sipia.log")
MAX_REINTENTOS_FLOTA = int(os.getenv("SIPIA_MAX_REINTENTOS", "5"))
HEARTBEAT_SEG = int(os.getenv("SIPIA_HEARTBEAT_SEG", "300"))


def _configurar_logging() -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if LOG_FILE:
        handlers.append(
            logging.handlers.RotatingFileHandler(
                LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
        )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


_configurar_logging()
log = logging.getLogger("sipia.runtime")

_detener = threading.Event()

# Sesion HTTP reutilizable (evita abrir una conexion nueva por cada lectura)
_sesion_http = requests.Session()
_sesion_http.mount(
    "http://",
    HTTPAdapter(max_retries=Retry(total=2, backoff_factor=0.3, status_forcelist=(500, 502, 503, 504))),
)

# Estado del fallback a DB directa
_db_lock = threading.Lock()
_db_inicializada = False


def _asegurar_db() -> None:
    global _db_inicializada
    if _db_inicializada:
        return
    with _db_lock:
        if not _db_inicializada:
            from industrial_platform import init_db

            init_db()
            _db_inicializada = True


def _on_lectura(alertas: SistemaAlertas, lectura: LecturaSensor) -> None:
    """Se ejecuta por cada lectura de sensor: la envia a la API REST/DB."""
    try:
        resp = _sesion_http.post(
            f"{API_URL}/lecturas",
            json={"sensor_id": lectura.sensor_id, "value": lectura.value, "unit": lectura.unit},
            timeout=5,
        )
        resp.raise_for_status()
    except Exception as exc:
        log.warning("No se pudo registrar lectura via API (%s); usando DB directa", exc)
        _guardar_directo(alertas, lectura)


def _guardar_directo(alertas: SistemaAlertas, lectura: LecturaSensor) -> None:
    """Fallback: guarda la lectura directo en SQLite si la API no responde."""
    from industrial_platform import get_conn, procesar_lectura, LecturaIn

    try:
        _asegurar_db()
        with get_conn() as conn:
            _, evento = procesar_lectura(conn, LecturaIn(
                sensor_id=lectura.sensor_id, value=lectura.value, unit=lectura.unit,
            ))
    except Exception:
        log.exception("Fallo tambien el guardado directo en DB para %s", lectura.sensor_id)
        return

    if evento:
        try:
            if evento["severity"] == "CRITICAL":
                alertas.alerta_critica(evento["sensor_id"], evento["reason"])
            else:
                alertas.alerta_advertencia(evento["sensor_id"], evento["reason"])
            enviar_alerta(evento)
        except Exception:
            log.exception("Fallo al disparar alerta para el evento de %s", evento.get("sensor_id"))


def _manejar_senales() -> None:
    def _handler(signum, frame):
        log.info("Senal %s recibida, deteniendo SIPIA de forma ordenada...", signum)
        _detener.set()

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def _vigilar_hilo_api(hilo_api: threading.Thread) -> None:
    """Hilo watchdog: si la API REST se cae inesperadamente, lo deja registrado."""
    hilo_api.join()
    if not _detener.is_set():
        log.critical("El hilo de la API REST termino inesperadamente; SIPIA sigue con fallback a DB directa")


def _ejecutar_flota_con_resiliencia(alertas: SistemaAlertas, intervalo: int) -> None:
    """Corre real_sensor_fleet y la reinicia con backoff si se cae por un error
    inesperado, para que un sistema de monitoreo 24/7 no muera silenciosamente."""
    intentos = 0
    ultimo_heartbeat = time.monotonic()

    while not _detener.is_set():
        try:
            real_sensor_fleet(
                on_lectura=lambda l: _on_lectura(alertas, l),
                intervalo_seg=intervalo,
                detener=lambda: _detener.is_set(),
            )
            break  # salio limpio porque _detener se activo
        except Exception:
            intentos += 1
            log.exception(
                "La flota de sensores fallo inesperadamente (intento %s/%s)",
                intentos, MAX_REINTENTOS_FLOTA,
            )
            if intentos >= MAX_REINTENTOS_FLOTA:
                log.critical("Se alcanzo el maximo de reintentos; deteniendo SIPIA")
                break
            espera = min(30, 2 ** intentos)
            log.info("Reintentando flota de sensores en %ss...", espera)
            if _detener.wait(espera):
                break

        if time.monotonic() - ultimo_heartbeat > HEARTBEAT_SEG:
            log.info("SIPIA activo (heartbeat)")
            ultimo_heartbeat = time.monotonic()


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="SIPIA runtime")
    parser.add_argument("--no-monitor", action="store_true", help="Desactiva el monitor de alertas de audio")
    parser.add_argument("--flask-routes", action="store_true", help="Expone rutas Flask de control de alertas")
    parser.add_argument("--no-api", action="store_true", help="No levantar el servidor FastAPI en este proceso")
    parser.add_argument("--intervalo", type=int, default=10, help="Intervalo de lectura de sensores (segundos)")
    args = parser.parse_args(argv)

    if args.intervalo <= 0:
        parser.error("--intervalo debe ser mayor a 0")

    _manejar_senales()

    alertas = SistemaAlertas()
    if not args.no_monitor:
        alertas.iniciar_monitor()
        log.info("Monitor de alertas sonoras/Telegram activo")

    if not args.no_api:
        _asegurar_db()
        hilo_api = threading.Thread(target=_orig.main, daemon=True, name="sipia-api")
        hilo_api.start()
        threading.Thread(target=_vigilar_hilo_api, args=(hilo_api,), daemon=True, name="sipia-api-watchdog").start()
        log.info("API REST SIPIA iniciada en segundo plano (%s)", API_URL)

    log.info("Iniciando flota de sensores reales (Ctrl+C para detener)")
    try:
        _ejecutar_flota_con_resiliencia(alertas, args.intervalo)
    except KeyboardInterrupt:
        log.info("SIPIA detenido por el usuario")
    finally:
        _detener.set()
        alertas.detener_monitor()
        log.info("SIPIA finalizado")


if __name__ == "__main__":
    main()
