# 🌍 SIPIA --- Sistema de Monitoreo Sismico Inteligente

Reconstruccion del proyecto SIPIA v1.0 (UAGRM --- Bolivia) a partir del
informe tecnico, ya que se perdieron los archivos originales.

## Estructura

```
SIPIA/
├── sipia_real.py             # Punto de entrada -- integra todos los modulos
├── industrial_platform.py    # Motor principal -- API REST + deteccion de anomalias
├── real_sensors.py           # Sensores fisicos + datos sismicos USGS
├── telegram_alerts.py        # Bot de Telegram
├── generar_reporte.py        # Reportes PDF profesionales
├── dashboard_graficas.html   # Dashboard web con graficas en tiempo real
├── mapa_sismos.html          # Mapa mundial de sismos (Leaflet + USGS)
├── requirements.txt
├── .env.example
├── sounds/
│   ├── alerta_sonido.py      # Sistema de alertas sonoras (beep/wav/voz)
│   ├── generar_wavs.py       # Genera los 3 archivos .wav (ya generados)
│   ├── alerta_critica.wav
│   ├── advertencia.wav
│   └── ok.wav
└── reportes/                 # Aqui se guardan los PDFs generados
```

La base de datos `sipia_readings.db` (SQLite) se crea automaticamente la
primera vez que corres el sistema --- no hace falta crearla a mano.

**Requisito:** Python 3.10 o superior.

## 1. Instalacion

```bash
cd SIPIA

# Crear entorno virtual
python -m venv .venv

# Activar entorno virtual
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # Linux/Mac

# Instalar dependencias
pip install -r requirements.txt
```

> `requirements.txt` incluye tanto `fastapi` como `flask`. El sistema
> corre sobre **FastAPI/Uvicorn** (`industrial_platform.py`); si no usas
> ningun modulo adicional basado en Flask, podes quitar esa dependencia.

## 2. Configuracion (`.env`)

1. Copia `.env.example` a `.env`:

   ```bash
   cp .env.example .env          # Linux/Mac
   copy .env.example .env        # Windows
   ```

2. Completa al menos el bloque de Telegram (opcional pero recomendado):
   - Habla con **@BotFather** en Telegram y crea un bot nuevo (`/newbot`).
   - Copia el TOKEN que te da en `SIPIA_TELEGRAM_TOKEN`.
   - Habla con **@userinfobot** para obtener tu `chat_id` y ponlo en
     `SIPIA_TELEGRAM_CHAT_ID`.
3. El resto de las variables (host/puerto de la API, ruta de la base de
   datos, volumen de alertas, etc.) tienen valores por defecto razonables;
   solo hace falta tocarlas si necesitas algo distinto. Ver la tabla
   completa mas abajo.

### Cargar las variables de entorno

**Opcion A -- recomendada: `python-dotenv`**

```bash
pip install python-dotenv
```

Y al inicio de `sipia_real.py`, **antes de cualquier `import` de
`industrial_platform` o `generar_reporte`**:

```python
from dotenv import load_dotenv
load_dotenv()
```

Esto es importante porque `industrial_platform.py` lee las variables de
entorno al momento de importarse (por ejemplo `DB_PATH`, `API_HOST`); si
`load_dotenv()` se llama despues del import, esas variables ya se habran
leido con sus valores por defecto.

**Opcion B -- exportarlas manualmente en la terminal**

En Windows PowerShell:

```powershell
$env:SIPIA_TELEGRAM_TOKEN="tu_token_aqui"
$env:SIPIA_TELEGRAM_CHAT_ID="tu_chat_id_aqui"
```

En Linux/Mac:

```bash
export SIPIA_TELEGRAM_TOKEN="tu_token_aqui"
export SIPIA_TELEGRAM_CHAT_ID="tu_chat_id_aqui"
```

Esta opcion no persiste entre sesiones de terminal; hay que repetirla cada
vez, por eso se recomienda la Opcion A para uso habitual.

### Variables de entorno disponibles

| Variable | Obligatoria | Por defecto | Descripcion |
|---|---|---|---|
| `SIPIA_TELEGRAM_TOKEN` | Solo si usas alertas por Telegram | -- | Token del bot (@BotFather) |
| `SIPIA_TELEGRAM_CHAT_ID` | Solo si usas alertas por Telegram | -- | Chat/grupo que recibe las alertas |
| `SIPIA_API_HOST` | No | `0.0.0.0` | Host donde escucha la API |
| `SIPIA_API_PORT` | No | `8000` | Puerto de la API |
| `SIPIA_CORS_ORIGINS` | No | `*` (todos) | Origenes permitidos, separados por coma |
| `SIPIA_DB_PATH` | No | `sipia_readings.db` junto al script | Ruta al archivo SQLite |
| `SIPIA_REPORTES_DIR` | No | `./reportes` | Carpeta de salida de los PDF |
| `SIPIA_SERIAL_PORT` | Solo con hardware real | -- | Puerto serie de los sensores (`COM3`, `/dev/ttyUSB0`, ...) |
| `SIPIA_SERIAL_BAUDRATE` | No | `9600` | Velocidad del puerto serie |
| `SIPIA_ALERTA_VOLUMEN` | No | `80` | Volumen inicial de alertas sonoras (0-100) |
| `SIPIA_LOG_LEVEL` | No | `INFO` | `DEBUG`, `INFO`, `WARNING` o `ERROR` |
| `SIPIA_API_KEY` | No | -- (sin auth) | Si se define, `POST /lecturas` exige el header `X-API-Key` con este valor |

> ⚠️ El archivo `dashboard_graficas.html` tiene la URL de la API
> (`http://127.0.0.1:8000`) escrita directamente en el HTML. Si cambias
> `SIPIA_API_HOST`/`SIPIA_API_PORT`, tenes que actualizar tambien la
> constante `API_BASE` al inicio del `<script>` del dashboard.

> ⚠️ Nunca subas tu `.env` real (con tokens/valores reales) a git --
> agregalo a tu `.gitignore`. Solo `.env.example` (sin valores) va al
> repositorio.

## 3. Ejecutar el sistema completo

```bash
python sipia_real.py
```

Esto levanta en un mismo proceso:
- La API REST (FastAPI/Uvicorn) en `http://127.0.0.1:8000` (o el host/puerto que hayas configurado)
- La flota de sensores reales (lee VIB-01 desde USGS + sensores fisicos simulados/USB)
- El monitor de alertas sonoras y Telegram

Otras variantes:

```bash
python sipia_real.py --no-monitor       # sin alertas de audio
python sipia_real.py --no-api           # sin levantar la API (usar otra ya corriendo)
python sipia_real.py --intervalo 5      # leer sensores cada 5 segundos
```

### Endpoints de la API

| Metodo | Ruta | Descripcion |
|---|---|---|
| `GET` | `/` | Info basica del proyecto |
| `GET` | `/salud` | Chequeo de disponibilidad (para monitoreo) |
| `GET` | `/estado` | Contadores generales + estado de alertas |
| `POST` | `/lecturas` | Registra una lectura de sensor (requiere `X-API-Key` si `SIPIA_API_KEY` esta configurada) |
| `GET` | `/lecturas` | Ultimas lecturas (`?sensor_id=&limite=`, `limite` maximo 1000) |
| `GET` | `/eventos` | Ultimos eventos de anomalia (`?severity=&limite=`) |
| `POST` | `/alertas/silenciar` | Activa/desactiva las alertas sonoras del dashboard |
| `POST` | `/alertas/volumen` | Ajusta el volumen (0-1) de las alertas sonoras |
| `GET` | `/reporte` | Genera y descarga el PDF del reporte |

## 4. Ver el dashboard

Con el sistema corriendo, simplemente abre en tu navegador:

- `dashboard_graficas.html` --- graficas en tiempo real por sensor
- `mapa_sismos.html` --- mapa mundial de sismos (USGS)

(Puedes abrirlos con doble clic, o servirlos con `python -m http.server` desde la carpeta del proyecto.)

## 5. Generar un reporte PDF

```bash
python generar_reporte.py
```

El PDF se guarda en `reportes/SIPIA_Reporte_<fecha>.pdf`. Tambien puedes
generarlo:
- Desde el dashboard (boton "Exportar PDF")
- Desde Telegram con el comando `/reporte` (una vez el bot este activo)
- Via API: `GET http://127.0.0.1:8000/reporte` (descarga el PDF directamente)

## 6. Probar solo el sonido de alertas

```bash
python -c "from sounds.alerta_sonido import SistemaAlertas; SistemaAlertas().alerta_critica('VIB-01', 'Prueba manual')"
```

## Solucion de problemas

- **"database is locked"**: ocurre si varios procesos escriben a la vez en
  SQLite. `industrial_platform.py` ya activa `journal_mode=WAL`, lo cual
  deberia evitarlo en la mayoria de los casos; si persiste, revisa que no
  tengas varias instancias de `sipia_real.py` corriendo contra el mismo
  `sipia_readings.db`.
- **El dashboard muestra "Desconectado"**: confirma que la API este
  corriendo y que `API_BASE` dentro de `dashboard_graficas.html` apunte al
  host/puerto correcto.
- **El mapa de sismos del PDF sale vacio**: `bloque_mapa()` en
  `generar_reporte.py` depende de la conexion a la API publica de USGS; sin
  internet, el mapa se genera igual pero sin puntos.
- **No llegan alertas a Telegram**: revisa que `SIPIA_TELEGRAM_TOKEN` y
  `SIPIA_TELEGRAM_CHAT_ID` esten cargados en el proceso (ver seccion 2) y
  que el bot haya sido iniciado al menos una vez con `/start` desde tu
  cuenta o el grupo correspondiente.

## Notas sobre esta reconstruccion

- El codigo fue reescrito desde cero siguiendo fielmente la arquitectura,
  nombres de archivo y funcionalidades descritas en el informe tecnico
  original (`SIPIA_Informe_Tecnico_Completo.docx`).
- Los sensores fisicos (ACCEL, GYRO, TEMP, PRESS) usan **datos simulados**
  como fallback si no detectan un puerto serie real conectado --- reemplaza
  las lineas correspondientes en `real_sensors.py` con la logica de tu
  hardware especifico si tienes sensores fisicos conectados.
- El sensor **VIB-01** si consume datos reales: la API publica del USGS
  (United States Geological Survey), tal como en el proyecto original.
- Revisa y ajusta los umbrales de deteccion de anomalias (`Z_SCORE_WARNING`,
  `Z_SCORE_CRITICAL`) en `industrial_platform.py` segun tus necesidades.
