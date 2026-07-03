"""
telegram_alerts.py --- Bot de Telegram
=========================================
Sistema de notificaciones automaticas via Telegram. Cuando SIPIA detecta
una anomalia critica, envia un mensaje instantaneo al celular del operador.

- Envio automatico de alertas criticas al celular
- Formato de mensaje con sensor, severidad y descripcion (con escape de
  caracteres Markdown para que un sensor_id o reason con guion bajo /
  asterisco no rompa el mensaje)
- Cooldown por sensor para evitar flood/ban de la API de Telegram cuando
  llegan varias alertas seguidas
- Comandos /estado y /reporte (compatibles con python-telegram-bot v20+, API async)
- Configuracion con TOKEN del bot de BotFather

Configura las variables de entorno antes de usar:
    SIPIA_TELEGRAM_TOKEN     -> token entregado por @BotFather
    SIPIA_TELEGRAM_CHAT_ID   -> chat_id del operador / grupo que recibe alertas
    SIPIA_TELEGRAM_COOLDOWN  -> segundos minimos entre alertas del mismo sensor (default 30)
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("sipia.telegram_alerts")

TELEGRAM_TOKEN = os.environ.get("SIPIA_TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("SIPIA_TELEGRAM_CHAT_ID", "")
COOLDOWN_SEG = float(os.environ.get("SIPIA_TELEGRAM_COOLDOWN", "30"))

EMOJI_SEVERIDAD = {
    "CRITICAL": "🔴",
    "WARNING": "🟡",
    "INFO": "🟢",
}

# Caracteres que rompen el parse_mode "Markdown" (legacy) de Telegram si
# aparecen sueltos dentro de texto dinamico (sensor_id, reason, etc).
_MARKDOWN_ESPECIALES = re.compile(r"([_*`\[])")

# ---------------------------------------------------------------------------
# Sesion HTTP reutilizable con reintentos + backoff
# ---------------------------------------------------------------------------

_sesion_http = requests.Session()
_sesion_http.mount(
    "https://",
    HTTPAdapter(max_retries=Retry(total=3, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504))),
)

# ---------------------------------------------------------------------------
# Cooldown anti-flood por sensor
# ---------------------------------------------------------------------------

_ultimo_envio: dict[str, float] = {}
_cooldown_lock = threading.Lock()


def _en_cooldown(sensor_id: str) -> bool:
    """True si ya se mando una alerta de este sensor hace menos de COOLDOWN_SEG."""
    if COOLDOWN_SEG <= 0:
        return False
    ahora = time.monotonic()
    with _cooldown_lock:
        ultimo = _ultimo_envio.get(sensor_id)
        if ultimo is not None and (ahora - ultimo) < COOLDOWN_SEG:
            return True
        _ultimo_envio[sensor_id] = ahora
        return False


def _escapar_markdown(texto: object) -> str:
    """Escapa caracteres especiales de Markdown (legacy) para que texto
    dinamico (sensor_id, reason, etc.) no rompa el formato del mensaje."""
    if texto is None:
        return "N/D"
    return _MARKDOWN_ESPECIALES.sub(r"\\\1", str(texto))


def _fmt_score(score: object) -> str:
    """Formatea el score de forma segura; evita el TypeError si es None."""
    if score is None:
        return "N/D"
    try:
        return f"{float(score):.2f}"
    except (TypeError, ValueError):
        return _escapar_markdown(score)


def _formatear_mensaje(evento: dict) -> str:
    severidad = evento.get("severity", "DESCONOCIDA")
    emoji = EMOJI_SEVERIDAD.get(severidad, "⚠️")
    return (
        f"{emoji} *ALERTA {_escapar_markdown(severidad)}* --- SIPIA\n\n"
        f"*Sensor:* {_escapar_markdown(evento.get('sensor_id'))}\n"
        f"*Z-Score:* {_fmt_score(evento.get('score'))}\n"
        f"*Motivo:* {_escapar_markdown(evento.get('reason'))}\n"
        f"*Detectado:* {_escapar_markdown(evento.get('detected_at'))}\n"
    )


def enviar_alerta(evento: dict, chat_id: Optional[str] = None, ignorar_cooldown: bool = False) -> bool:
    """Envia una alerta critica al celular del operador via Telegram Bot API.

    Retorna True si el envio fue exitoso, False si no se pudo enviar
    (TOKEN/CHAT_ID no configurados, en cooldown, o fallo de red tras reintentos).
    """
    token = TELEGRAM_TOKEN
    destino = chat_id or TELEGRAM_CHAT_ID

    if not token or not destino:
        log.debug("Telegram no configurado (falta TOKEN o CHAT_ID); alerta no enviada")
        return False

    sensor_id = evento.get("sensor_id", "desconocido")
    if not ignorar_cooldown and _en_cooldown(sensor_id):
        log.info("Alerta de %s omitida por cooldown (%ss)", sensor_id, COOLDOWN_SEG)
        return False

    mensaje = _formatear_mensaje(evento)
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = _sesion_http.post(
            url,
            json={"chat_id": destino, "text": mensaje, "parse_mode": "Markdown"},
            timeout=8,
        )
        resp.raise_for_status()
        log.info("Alerta Telegram enviada para sensor %s", sensor_id)
        return True
    except Exception as exc:
        log.error("Error enviando alerta Telegram para %s: %s", sensor_id, exc)
        return False


# ---------------------------------------------------------------------------
# Bot interactivo (comandos) --- usa python-telegram-bot v20+ (API async)
# ---------------------------------------------------------------------------

async def _cmd_start(update, context) -> None:  # pragma: no cover - requiere entorno con bot corriendo
    await update.message.reply_text(
        "👋 SIPIA Monitor activo.\nComandos disponibles:\n"
        "/estado - ver estado del sistema\n"
        "/reporte - generar y recibir reporte PDF"
    )


async def _cmd_estado(update, context) -> None:  # pragma: no cover
    try:
        from industrial_platform import estado_sistema

        estado = estado_sistema()
        sensores = ", ".join(estado.get("sensores_activos", [])) or "ninguno"
        texto = (
            f"📊 *Estado de SIPIA*\n\n"
            f"Lecturas: {estado.get('lecturas', 'N/D')}\n"
            f"Eventos: {estado.get('eventos', 'N/D')}\n"
            f"Sensores activos: {_escapar_markdown(sensores)}"
        )
    except Exception as exc:
        log.exception("Error obteniendo estado del sistema para /estado")
        texto = f"No se pudo obtener el estado: {_escapar_markdown(exc)}"
    await update.message.reply_text(texto, parse_mode="Markdown")


async def _cmd_reporte(update, context) -> None:  # pragma: no cover
    await update.message.reply_text("Generando reporte PDF, un momento...")
    try:
        import asyncio

        from generar_reporte import genera_reporte_pdf

        # genera_reporte_pdf es sincrono/bloqueante (I/O de disco); se corre
        # en un executor para no congelar el loop de eventos del bot.
        ruta = await asyncio.to_thread(genera_reporte_pdf)
        with open(ruta, "rb") as f:
            await update.message.reply_document(document=f, filename=os.path.basename(ruta))
    except Exception as exc:
        log.exception("Error generando el reporte PDF para /reporte")
        await update.message.reply_text(f"Error generando el reporte: {exc}")


def iniciar_bot() -> None:  # pragma: no cover - requiere token real
    """Arranca el bot de Telegram en modo polling (comandos /estado, /reporte)."""
    if not TELEGRAM_TOKEN:
        raise RuntimeError(
            "Configura la variable de entorno SIPIA_TELEGRAM_TOKEN con el "
            "token entregado por @BotFather antes de iniciar el bot."
        )

    from telegram.ext import ApplicationBuilder, CommandHandler

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("estado", _cmd_estado))
    app.add_handler(CommandHandler("reporte", _cmd_reporte))

    log.info("Bot de Telegram SIPIA iniciado (polling)")
    app.run_polling()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    iniciar_bot()
