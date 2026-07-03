"""
generar_wavs.py --- Genera los archivos de audio de alerta de SIPIA
======================================================================
Crea alerta_critica.wav, advertencia.wav y ok.wav de forma sintetica
(sin depender de archivos externos), tal como describe el informe:

    sounds/alerta_critica.wav -> Patron urgente ascendente
    sounds/advertencia.wav    -> Doble beep de advertencia
    sounds/ok.wav             -> Acorde positivo (Do-Mi-Sol)

Ejecutar una sola vez: python generar_wavs.py
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

SAMPLE_RATE = 44100
OUT_DIR = Path(__file__).parent


def _tono(freq: float, duracion_s: float, volumen: float = 0.5) -> list[int]:
    n = int(SAMPLE_RATE * duracion_s)
    muestras = []
    for i in range(n):
        t = i / SAMPLE_RATE
        # pequeño fade-out para evitar clics
        fade = min(1.0, (n - i) / (SAMPLE_RATE * 0.02))
        valor = math.sin(2 * math.pi * freq * t) * volumen * fade
        muestras.append(int(valor * 32767))
    return muestras


def _silencio(duracion_s: float) -> list[int]:
    return [0] * int(SAMPLE_RATE * duracion_s)


def _escribir_wav(nombre: str, muestras: list[int]) -> Path:
    ruta = OUT_DIR / nombre
    with wave.open(str(ruta), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        datos = struct.pack("<" + "h" * len(muestras), *muestras)
        wf.writeframes(datos)
    print(f"Generado: {ruta} ({ruta.stat().st_size // 1024} KB)")
    return ruta


def generar_alerta_critica():
    # Patron urgente ascendente: 3 tonos subiendo de frecuencia, repetido x3
    muestras: list[int] = []
    frecuencias = [880, 1046, 1318]  # A5, C6, E6
    for _ in range(3):
        for f in frecuencias:
            muestras += _tono(f, 0.15, volumen=0.6)
            muestras += _silencio(0.03)
        muestras += _silencio(0.12)
    _escribir_wav("alerta_critica.wav", muestras)


def generar_advertencia():
    # Doble beep de advertencia
    muestras: list[int] = []
    for _ in range(2):
        muestras += _tono(660, 0.18, volumen=0.5)
        muestras += _silencio(0.15)
    _escribir_wav("advertencia.wav", muestras)


def generar_ok():
    # Acorde positivo: Do-Mi-Sol (C-E-G) tocado en secuencia rapida
    muestras: list[int] = []
    for f in (523.25, 659.25, 783.99):  # C5, E5, G5
        muestras += _tono(f, 0.18, volumen=0.45)
    muestras += _silencio(0.05)
    _escribir_wav("ok.wav", muestras)


if __name__ == "__main__":
    generar_alerta_critica()
    generar_advertencia()
    generar_ok()
