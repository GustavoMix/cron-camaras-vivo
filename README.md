# cron-camaras-vivo

Índice semanal de **cámaras públicas en vivo**, publicado como JSON estático en
GitHub Pages y listo para consumir desde una app Kotlin.

Un GitHub Action corre todos los **lunes a las 8:00 (hora Argentina)**, consulta
las fuentes, normaliza todo a un mismo esquema, deduplica y commitea el
resultado en `data/`, que además se publica en Pages.

## Alcance

Se indexan **únicamente transmisiones que los organismos publican
deliberadamente para consumo abierto**: cámaras de tránsito de agencias viales,
webcams turísticas de plataformas con API pública, y datasets de open data.

Este proyecto **no** escanea internet buscando cámaras privadas mal
configuradas. Eso es acceso no autorizado a equipos ajenos, y además da datos
peores: sin nombre, sin geolocalización y sin permiso de redistribución.

## Los datos

Todo se sirve bajo `https://gustavomix.github.io/cron-camaras-vivo/`:

| Archivo | Contenido |
| --- | --- |
| `v1/index.json` | Metadatos, conteos por país, salud de cada fuente |
| `v1/cameras.json` | Todas las cámaras |
| `v1/countries/AR.json` | Un archivo por país (ISO 3166-1 alfa-2) |
| `v1/sources.json` | Catálogo de fuentes con licencia y atribución |

Una cámara se ve así:

```json
{
  "id": "a3f9c1d4e8b2",
  "name": "Highway 401 - near Yonge St (East)",
  "source": "511on",
  "country": "CA",
  "region": "ON",
  "lat": 43.65107,
  "lon": -79.38393,
  "kind": "traffic",
  "image": "https://511on.ca/map/Cctv/1234",
  "stream": "https://511on.ca/live/1234/index.m3u8",
  "stream_format": "hls",
  "attribution": "Ontario 511",
  "license": "Open data published by Ontario 511"
}
```

Notas sobre los campos:

- **`id`** es estable entre corridas (hash de `source` + id nativo). Cachealo
  tranquilo; sólo cambia si la fuente cambia su identificador.
- **`image`** es una foto fija que el organismo refresca en el lugar: para
  actualizarla, volvé a pedir la misma URL.
- **`stream`** es video continuo. Puede faltar; muchas agencias sólo publican
  fotos. Filtrá por `stream != null` si necesitás video.
- **`stream_format`** es `hls`, `dash`, `mp4`, `mjpeg`, `rtsp` o `rtmp`.
- **`lat`/`lon`** pueden faltar si la fuente no los informa.
- **`attribution`** y **`license`**: respetalos, varias fuentes exigen crédito
  visible.

## Por qué GitHub Pages y no `raw.githubusercontent.com`

Preguntabas cuál conviene. **Pages**, por bastante:

| | GitHub Pages | raw.githubusercontent |
| --- | --- | --- |
| CDN | Sí, con caché en el borde | No, sirve desde origen |
| `Access-Control-Allow-Origin: *` | Sí | Sí, pero sin garantías |
| Rate limiting | Sin límite práctico | Agresivo, y te tira 429 |
| `ETag` / `304` | Sí | Parcial |
| `Content-Type` | `application/json` | `text/plain` |
| Dominio propio | Sí | No |

Para una app móvil la diferencia que más se nota es el rate limiting: `raw`
empieza a devolver 429 cuando varios usuarios pegan al mismo tiempo. Los datos
igual quedan commiteados en `data/`, así que tenés historial semanal versionado
además de la copia servida.

Para activarlo: **Settings → Pages → Source: GitHub Actions**. Una sola vez.

## Consumir desde Kotlin

Con `kotlinx.serialization` y OkHttp. Los campos opcionales tienen default, así
que agregar campos nuevos al esquema no te rompe el parseo.

```kotlin
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

@Serializable
data class Camera(
    val id: String,
    val name: String,
    val source: String,
    val kind: String = "other",
    val country: String? = null,
    val region: String? = null,
    val city: String? = null,
    val lat: Double? = null,
    val lon: Double? = null,
    val image: String? = null,
    val stream: String? = null,
    @SerialName("stream_format") val streamFormat: String? = null,
    val attribution: String? = null,
    val license: String? = null,
)

@Serializable
data class CameraFeed(
    val schema: Int,
    @SerialName("generated_at") val generatedAt: String,
    val count: Int,
    val cameras: List<Camera>,
)

// ignoreUnknownKeys es importante: permite agregar campos al esquema sin
// romper las versiones de la app que ya están instaladas.
val json = Json { ignoreUnknownKeys = true }
```

Descarga con caché en disco, para que el `ETag` haga su trabajo:

```kotlin
import okhttp3.Cache
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File

private const val BASE = "https://gustavomix.github.io/cron-camaras-vivo/"

class CameraRepository(cacheDir: File) {
    private val client = OkHttpClient.Builder()
        // Los datos cambian una vez por semana; con 10 MB de caché la app
        // arranca offline y sólo baja de nuevo cuando el ETag cambió.
        .cache(Cache(File(cacheDir, "cameras"), 10L * 1024 * 1024))
        .build()

    suspend fun load(country: String? = null): List<Camera> = withContext(Dispatchers.IO) {
        val path = country?.let { "v1/countries/$it.json" } ?: "v1/cameras.json"
        val request = Request.Builder().url(BASE + path).build()

        client.newCall(request).execute().use { response ->
            check(response.isSuccessful) { "HTTP ${response.code} al pedir $path" }
            val body = response.body?.string() ?: error("respuesta vacía")
            json.decodeFromString<CameraFeed>(body).cameras
        }
    }
}
```

Reproducir una cámara:

```kotlin
// Foto fija: cualquier librería de imágenes. Para refrescar, invalidá la caché
// o agregá un parámetro propio, porque la URL no cambia.
AsyncImage(model = camera.image, contentDescription = camera.name)

// Video en vivo: HLS necesita ExoPlayer con el módulo hls.
val player = ExoPlayer.Builder(context).build().apply {
    setMediaItem(MediaItem.fromUri(camera.stream!!))
    prepare()
}
```

Si sólo te interesa un país, pedí el shard (`v1/countries/AR.json`) en lugar del
archivo completo: baja muchísimo el tamaño de descarga.

## Fuentes

Sin API key (funcionan apenas clonás el repo):

- **Plataforma 511** — Ontario, Alberta, New Brunswick, Nova Scotia, PEI,
  Saskatchewan, Manitoba
- **Caltrans** — cámaras de California, por distrito
- **NYC DOT** — cámaras de tránsito de Nueva York
- **TfL JamCams** — Londres
- **Transport for NSW** — cámaras de tránsito de Nueva Gales del Sur, Australia
- **Waka Kotahi NZTA** — cámaras de rutas de Nueva Zelanda

Con API key gratuita (agregalas como *repository secrets* para ampliar la
cobertura):

| Secret | Fuente | Dónde sacarla |
| --- | --- | --- |
| `WINDY_API_KEY` | Windy Webcams — **global, la más grande** | <https://api.windy.com/webcams> |
| `TFL_APP_KEY` | TfL (sube el rate limit) | <https://api-portal.tfl.gov.uk/> |
| `WSDOT_ACCESS_CODE` | Washington State DOT | <https://wsdot.wa.gov/traffic/api/> |
| `QLD_TRAFFIC_KEY` | Queensland, Australia | <https://qldtraffic.qld.gov.au/> |
| `KEY_511NY`, `KEY_FL511`, `KEY_511GA`, `KEY_MASS511`, `KEY_511PA`, `KEY_511VA`, `KEY_UDOT`, `KEY_511IA`, `KEY_511WI`, `KEY_511NE` | Instancias 511 de EE.UU. | Portal de cada agencia |

Una fuente sin su key **se saltea limpiamente**, no rompe la corrida. Si querés
cobertura mundial de verdad, la que más mueve la aguja es `WINDY_API_KEY`.

## Uso local

```bash
pip install -r requirements.txt

python -m cameras list                 # ver el registro de fuentes
python -m cameras build                # corrida completa, escribe en ./data
python -m cameras build --only 511on   # una sola fuente, para desarrollar
```

Opciones útiles de `build`:

- `--min-cameras N` — aborta **sin escribir** si junta menos de N cámaras. Es la
  red de contención contra una caída de red que reemplazaría un dataset bueno
  por uno vacío. El workflow usa 500.
- `--only ID` — repetible, restringe la corrida.
- `--concurrency N` — requests HTTP en paralelo (default 8).

Tests:

```bash
pip install -r requirements-dev.txt
pytest tests -q
```

No tocan la red: los adaptadores se prueban contra las formas de respuesta
documentadas en cada módulo.

## Agregar una fuente

1. Creá `cameras/sources/mi_fuente.py` con una clase que herede de `Source` y
   una función `build_sources()`.
2. Construí cada cámara con `make_camera(...)`, que valida y normaliza.
3. Registrá el módulo en `_MODULES`, en `cameras/sources/__init__.py`.
4. Agregá un test con una respuesta de ejemplo real.

Si la agencia usa la plataforma 511, no hace falta código: sumá una fila a
`_OPEN` o `_KEYED` en `cameras/sources/castlerock.py`.

Los adaptadores deben ser **tolerantes a formas inesperadas**: descartá el
registro malo y seguí. Sólo lanzá excepción si la fuente entera es inalcanzable
— eso queda registrado en el reporte de salud sin frenar a las demás.

## Cambiar el horario

En `.github/workflows/update-cameras.yml`. El cron de GitHub es **siempre UTC**:

```yaml
- cron: "0 11 * * 1"   # lunes 11:00 UTC = 8:00 en Argentina (UTC-3)
```

Si estás en otro huso, sumale el offset. Ojo: los cron de GitHub no son puntuales,
suelen demorar entre unos minutos y ~1 hora si los runners están cargados.

## Operación

- Cada corrida escribe un **reporte de salud por fuente** en `v1/index.json` y
  un resumen en la página del Action. Si una fuente cambia su API, aparece ahí
  como `ok: false` sin frenar el resto.
- El commit semanal es un diff chico: la salida está ordenada de forma
  determinística y los ids son estables, así que sólo cambian las cámaras que
  realmente cambiaron.
- Podés correrlo a mano desde **Actions → Update camera index → Run workflow**,
  con inputs para limitar fuentes o el umbral mínimo.
