"""
alerta_sonido.py --- Sistema de Alertas Sonoras
==================================================
Modulo de alertas de audio multiplataforma que soporta 3 modos de alerta:

- BEEP: Tonos del sistema Windows (sin dependencias externas, via winsound)
- WAV:  Archivos de audio personalizados (alerta_critica.wav, advertencia.wav, ok.wav)
- VOZ:  Sintesis de voz en espanol con pyttsx3

Integracion con el sistema:
- Monitor automatico: vigila la base de datos cada 5 segundos
- Callback al dashboard HTML via JavaScript Web Audio API (endpoint Flask)
- Panel de control: boton silenciar + slider de volumen
- Integracion con el bot de Telegram para alertas remotas
"""

from __future__ import annotations

import logging
import platform
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("sipia.alerta_sonido")

SOUNDS_DIR = Path(__file__).parent
DB_PATH = Path(__file__).parent.parent / "sipia_readings.db"

ARCHIVOS_WAV = {
    "critico": SOUNDS_DIR / "alerta_critica.wav",
    "advertencia": SOUNDS_DIR / "advertencia.wav",
    "ok": SOUNDS_DIR / "ok.wav",
}

INTERVALO_MONITOR_SEG = 5


class SistemaAlertas:
    """Orquesta la reproduccion de alertas sonoras en 3 modos: beep, wav y voz."""

    def __init__(self, modo: str = "auto"):
        self.silenciado = False
        self.volumen = 0.8
        self.modo = self._detectar_modo() if modo == "auto" else modo
        self._monitor_hilo: Optional[threading.Thread] = None
        self._detener_monitor = threading.Event()
        self.callbacks: list[Callable[[str, str], None]] = []
        self._ultimo_event_id = self._max_event_id_actual()
        log.info("SistemaAlertas inicializado en modo '%s'", self.modo)

    # -- Deteccion de plataforma -------------------------------------------------

    @staticmethod
    def _detectar_modo() -> str:
        return "beep" if platform.system() == "Windows" else "wav"

    # -- Registro de callbacks (para el dashboard) -------------------------------

    def registrar_callback(self, callback: Callable[[str, str], None]) -> None:
        """Registra una funcion que se invoca al disparar una alerta.
        Firma: callback(tipo: str, sensor_id: str) -> None
        Usado por crear_rutas_flask para notificar al dashboard via SSE/WebSocket.
        """
        self.callbacks.append(callback)

    # -- Reproduccion --------------------------------------------------------

    def _reproducir(self, tipo: str, texto_voz: Optional[str] = None) -> None:
        if self.silenciado:
            log.debug("Alertas silenciadas; se omite reproduccion de '%s'", tipo)
            return

        try:
            if self.modo == "beep":
                self._beep_windows(tipo)
            elif self.modo == "wav":
                self._reproducir_wav(tipo)
            elif self.modo == "voz":
                self._reproducir_voz(texto_voz or tipo)
            else:
                log.warning("Modo de alerta desconocido: %s", self.modo)
        except Exception as exc:
            log.error("Error reproduciendo alerta '%s': %s", tipo, exc)

        for cb in self.callbacks:
            try:
                cb(tipo, texto_voz or "")
            except Exception as exc:
                log.debug("Error en callback de alerta: %s", exc)

    def _beep_windows(self, tipo: str) -> None:
        try:
            import winsound
        except ImportError:
            log.debug("winsound no disponible (no es Windows); usando WAV como fallback")
            self._reproducir_wav(tipo)
            return

        patrones = {
            "critico": [(1200, 180), (1500, 180), (1800, 260)],
            "advertencia": [(900, 150), (900, 150)],
            "ok": [(660, 120), (880, 120), (990, 160)],
        }
        for freq, dur in patrones.get(tipo, [(800, 150)]):
            winsound.Beep(freq, dur)

    def _reproducir_wav(self, tipo: str) -> None:
        ruta = ARCHIVOS_WAV.get(tipo)
        if not ruta or not ruta.exists():
            log.warning("Archivo WAV no encontrado para tipo '%s' (%s)", tipo, ruta)
            return
        try:
            import pygame

            pygame.mixer.init()
            sonido = pygame.mixer.Sound(str(ruta))
            sonido.set_volume(self.volumen)
            sonido.play()
        except ImportError:
            log.debug("pygame no disponible; intentando playsound")
            try:
                from playsound import playsound

                playsound(str(ruta))
            except Exception as exc:
                log.error("No se pudo reproducir WAV: %s", exc)

    def _reproducir_voz(self, texto: str) -> None:
        try:
            import pyttsx3

            engine = pyttsx3.init()
            engine.setProperty("volume", self.volumen)
            for voz in engine.getProperty("voices"):
                if "spanish" in voz.name.lower() or "es" in getattr(voz, "languages", []):
                    engine.setProperty("voice", voz.id)
                    break
            engine.say(texto)
            engine.runAndWait()
        except ImportError:
            log.warning("pyttsx3 no instalado; no se puede usar modo VOZ")

    # -- API publica -----------------------------------------------------------

    def alerta_critica(self, sensor_id: str = "", razon: str = "") -> None:
        print(f"ALERTA CRITICA --- Sensor: {sensor_id} --- {razon}")
        texto = f"Alerta critica en sensor {sensor_id}. {razon}"
        threading.Thread(target=self._reproducir, args=("critico", texto), daemon=True).start()

    def alerta_advertencia(self, sensor_id: str = "", razon: str = "") -> None:
        print(f"Advertencia --- Sensor: {sensor_id} --- {razon}")
        texto = f"Advertencia en sensor {sensor_id}. {razon}"
        threading.Thread(target=self._reproducir, args=("advertencia", texto), daemon=True).start()

    def alerta_ok(self) -> None:
        threading.Thread(target=self._reproducir, args=("ok", "Sistema estable"), daemon=True).start()

    def silenciar(self, silenciar: bool = True) -> None:
        self.silenciado = silenciar
        log.info("Alertas %s", "silenciadas" if silenciar else "activadas")

    def ajustar_volumen(self, volumen: float) -> None:
        self.volumen = max(0.0, min(1.0, volumen))
        log.info("Volumen ajustado a %.0f%%", self.volumen * 100)

    # -- Monitor automatico de la base de datos --------------------------------

    def _max_event_id_actual(self) -> int:
        if not DB_PATH.exists():
            return 0
        try:
            conn = sqlite3.connect(DB_PATH)
            row = conn.execute("SELECT MAX(event_id) m FROM events").fetchone()
            conn.close()
            return row[0] or 0
        except Exception:
            return 0

    def _loop_monitor(self) -> None:
        log.info("Monitor de alertas iniciado (cada %ss)", INTERVALO_MONITOR_SEG)
        while not self._detener_monitor.is_set():
            try:
                if DB_PATH.exists():
                    conn = sqlite3.connect(DB_PATH)
                    conn.row_factory = sqlite3.Row
                    nuevos = conn.execute(
                        "SELECT * FROM events WHERE event_id > ? ORDER BY event_id ASC",
                        (self._ultimo_event_id,),
                    ).fetchall()
                    conn.close()

                    for evento in nuevos:
                        self._ultimo_event_id = max(self._ultimo_event_id, evento["event_id"])
                        if evento["severity"] == "CRITICAL":
                            self.alerta_critica(evento["sensor_id"], evento["reason"] or "")
                        elif evento["severity"] == "WARNING":
                            self.alerta_advertencia(evento["sensor_id"], evento["reason"] or "")
            except Exception as exc:
                log.error("Error en monitor de alertas: %s", exc)

            self._detener_monitor.wait(INTERVALO_MONITOR_SEG)

    def iniciar_monitor(self) -> None:
        if self._monitor_hilo and self._monitor_hilo.is_alive():
            log.debug("El monitor ya esta corriendo")
            return
        self._detener_monitor.clear()
        self._monitor_hilo = threading.Thread(target=self._loop_monitor, daemon=True)
        self._monitor_hilo.start()

    def detener_monitor(self) -> None:
        self._detener_monitor.set()
        if self._monitor_hilo:
            self._monitor_hilo.join(timeout=2)


# ---------------------------------------------------------------------------
# Integracion Flask (dashboard) --- botón silenciar + slider de volumen
# ---------------------------------------------------------------------------

def crear_rutas_flask(app, sistema: SistemaAlertas):
    """Registra rutas Flask para controlar el sistema de alertas desde el dashboard."""
    from flask import jsonify, request

    @app.route("/alertas/silenciar", methods=["POST"])
    def _silenciar():
        valor = request.json.get("silenciar", True) if request.is_json else True
        sistema.silenciar(valor)
        return jsonify({"silenciado": sistema.silenciado})

    @app.route("/alertas/volumen", methods=["POST"])
    def _volumen():
        valor = float(request.json.get("volumen", 0.8)) if request.is_json else 0.8
        sistema.ajustar_volumen(valor)
        return jsonify({"volumen": sistema.volumen})

    @app.route("/alertas/test/<tipo>", methods=["POST"])
    def _test(tipo):
        if tipo == "critico":
            sistema.alerta_critica("TEST", "Prueba manual desde dashboard")
        elif tipo == "advertencia":
            sistema.alerta_advertencia("TEST", "Prueba manual desde dashboard")
        else:
            sistema.alerta_ok()
        return jsonify({"ok": True, "tipo": tipo})

    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    s = SistemaAlertas()
    s.alerta_critica("VIB-01", "2.4 Richter | Z-Score: -4.50")
    time.sleep(2)
