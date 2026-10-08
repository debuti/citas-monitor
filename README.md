# citas-monitor

Vigila la agenda de médicos de **Vithas** y **Quirónsalud** (con un hospital y un seguro
concretos) y avisa por **Telegram** en cuanto aparecen huecos para reservar. Una sola
ejecución revisa todos los médicos configurados, y cada uno avisa a su propio chat de
Telegram, así que sirve para varias personas a la vez. Qué médicos vigilar y a quién
avisar no está en el código: se configura con un secreto (ver abajo).

## Cómo funciona

Ninguna de las dos tiene API pública, pero sus webs de citas usan APIs internas que se
pueden llamar sin iniciar sesión.

**Vithas** (`areaprivada.vithas.es`):

| Paso | Endpoint (`https://app-services-prod.vithas.es/api`) |
|---|---|
| Actividades del médico (Consulta / Revision) | `GET /appointments/v1/filterdoctor/speciality` |
| Huecos disponibles | `GET /appointments/v1/appointments/availabilities` |

Las llamadas llevan la cabecera `platform: web`. Si no hay huecos, la API devuelve `[]`.
La web busca por bloques de unas 10 semanas, así que el script mira 5 bloques
seguidos (casi un año) para cada actividad.

**Quirónsalud** (`www.quironsalud.com/es/cita-medica`):

| Paso | Endpoint (`POST https://www.quironsalud.com/idcsalud-client/cm/quironsalud/pdp-api/v1`) |
|---|---|
| Prestaciones del médico (Consulta Primera, Sucesiva...) | `/catalog/professionals/services/channels` |
| Huecos disponibles | `/citas/huecos` |

Los parámetros van como formulario (`application/x-www-form-urlencoded`). La búsqueda
devuelve los huecos de unos 4 meses a partir de hoy; si no hay, devuelve `[]`.

`monitor.py`:
- avisa **solo de los huecos nuevos**: si un hueco desaparece y vuelve a salir (por
  ejemplo, por una cancelación), avisa otra vez;
- con `max_days` solo avisa de los huecos de los próximos N días;
- si un médico falla 3 veces seguidas (la web cambia, está caída...), avisa por Telegram
  para que no te quedes esperando sin saberlo, y avisa también cuando se recupera;
- en cada búsqueda manda un "👀 Sigo buscando: sin huecos todavía" **sin sonido**, para
  que sepas que funciona; las citas nuevas (🚨) y los fallos (⚠️) llegan **con sonido**;
- solo usa la biblioteca estándar de Python 3, sin dependencias.

## Puesta en marcha

### 1. Crear el bot de Telegram

Basta con un bot para todos: cada persona le escribe y recibe solo los avisos de sus
médicos. Un bot de Telegram no puede escribir a un número de teléfono: necesita un
`chat_id` y que la persona le haya escrito antes.

1. En Telegram, habla con **@BotFather**, envía `/newbot` y sigue los pasos. Te dará
   un **token** del tipo `123456:ABC-...`.
2. Cada persona que vaya a recibir avisos abre el chat con el bot y le envía `/start`.
3. Saca los `chat_id`:
   ```bash
   TELEGRAM_BOT_TOKEN=123456:ABC-... python3 monitor.py --telegram-chat-id
   ```
   Si no tienes Python a mano, abre `https://api.telegram.org/bot<TOKEN>/getUpdates`
   en el navegador y busca `"chat":{"id": ...}`.

Para avisar a un grupo, añade el bot al grupo y escribe algo en él: su `chat_id`
(negativo) saldrá igual.

### 2. Configurar GitHub

En el repo, ve a **Settings → Secrets and variables → Actions → New repository
secret** y crea estos dos secretos:
- `TELEGRAM_BOT_TOKEN`
- `MONITOR_CONFIG`: los médicos a vigilar y a quién avisar, en JSON (ver "Qué médicos
  vigilar" más abajo).

Los workflows programados solo se ejecutan desde la **rama por defecto** del repo. Si
mueves el código a otra rama (por ejemplo `main`), ponla como rama por defecto.

### 3. Probar

En **Actions → Vigilar citas médicas → Run workflow**:
- elige `test-notify`: a cada chat le debería llegar un mensaje de prueba;
- elige `check`: hace una búsqueda real y manda el "sigo buscando".

A partir de ahí busca cada 10 minutos y escribe en cada búsqueda.

## Qué médicos vigilar (`MONITOR_CONFIG`)

Es una lista JSON con un elemento ("vigilante") por médico. Tienes un ejemplo completo,
con los dos proveedores y uno desactivado, en
[`monitor_config.example.json`](monitor_config.example.json):

```json
[
  {
    "id": "vithas-traumato",
    "provider": "vithas",
    "enabled": true,
    "chat_ids": ["123456789"],
    "doctor_name": "Dr. Nombre Apellido (Especialidad)",
    "hospital_name": "Hospital Vithas ...",
    "insurance_name": "Nombre del seguro",
    "hospital_id": "sc...@tt_vithas_one_..._prod",
    "doctor_id": "sc...@tt_vithas_one_..._prod",
    "insurance_id": "sc..."
  },
  {
    "id": "quiron-traumato",
    "provider": "quiron",
    "enabled": true,
    "chat_ids": ["987654321", "123456789"],
    "doctor_name": "Dra. Nombre Apellido (Especialidad)",
    "hospital_name": "Hospital Universitario Quirónsalud ...",
    "insurance_name": "Nombre del seguro",
    "professional_id": "1234",
    "specialty_id": "47",
    "province_id": "28",
    "center_id": "0342...",
    "associated_center_id": "0342...",
    "insurance_id": "23",
    "max_days": 30
  }
]
```

Claves comunes:
- `provider`: `vithas` o `quiron`.
- `chat_ids`: lista de chats de Telegram a los que avisar (una o varias personas o
  grupos). Si solo es uno, también vale `"chat_id": "111"`.
- `enabled`: `false` para dejar de vigilar ese médico sin borrarlo de la lista (por
  defecto `true`). Mientras está desactivado no se comprueba ni avisa, no hace falta
  que esté completo y se conserva lo ya avisado por si se vuelve a activar.
- `id`: nombre corto y único; con él se guarda qué huecos ya se avisaron. Si lo
  cambias, se volverá a avisar de los huecos que haya.
- Opcionales: `doctor_name`, `hospital_name`, `insurance_name` (solo para el texto de
  los mensajes); `max_days` (solo avisa de huecos en los próximos N días; por defecto
  sin límite); `heartbeat` (`false` para no mandar el "sigo buscando" a ese chat);
  `bot_token` (para avisar desde otro bot distinto de `TELEGRAM_BOT_TOKEN`).

### Vithas

- Obligatorios: `hospital_id`, `doctor_id` e `insurance_id`.
- Opcionales: `is_private` (`"1"` si pagas sin seguro), `activities` (por ejemplo
  `"Consulta"`; por defecto vigila todas), `windows` y `window_days` (cuánto mira
  hacia delante; por defecto 5 × 70 días).

Los IDs salen de la propia API, con la cabecera `platform: web`:
- `GET /appointments/v1/filter/hospital?idSite=<provincia>` (Madrid es `16`)
- `GET /appointments/v1/filter/insurance?instanceTuotempoId=tt_portal_vithas_one_prod&hospitalTuotempoId=<hospital>`
- `GET /appointments/v1/filter/doctors?...&insuranceId=<seguro>&specialityId=<especialidad>`

### Quirónsalud

- Obligatorios: `professional_id`, `specialty_id`, `province_id`, `center_id` y
  `associated_center_id`.
- `insurance_id`: el seguro/colectivo; vacío o sin poner si pagas sin seguro.
- Opcional: `services`, las prestaciones a vigilar separadas por comas, con el nombre
  exacto (por ejemplo `"Consulta Primera Pie y Tobillo"`); por defecto vigila todas.

Los IDs salen de la propia API (`POST`, parámetros como formulario):
- Médico: `/catalog/professionals/search` con `searchQuery=<nombre>`. De la respuesta:
  `idProfesionalPdP` → `professional_id`, `idEspecialidadGlobal` → `specialty_id`, y en
  `centros`: `idProvincia` → `province_id`, `idCentro` → `center_id` y
  `codCentroAsociado` → `associated_center_id`.
- Seguro: `/citas/aseguradoras` con `idCentro=`. Cada aseguradora tiene una lista
  `centrosAsistenciales` (por ejemplo ASISA → MUFACE, ISFAS, Póliza/Colectivos
  privados...): su `idCentroAsistencial` es `insurance_id`.
- Prestaciones: `/catalog/professionals/services/channels` con `idProvincia`,
  `idCentro`, `codCentroAsociado`, `idProfesionalPdP`, `idEspecialidadGlobal` y
  `financialType=2`.

### Formato antiguo

Si no existe `MONITOR_CONFIG`, se usa el formato de cuando solo había Vithas: el
secreto `VITHAS_CONFIG` (un solo objeto con las claves de Vithas) y `TELEGRAM_CHAT_ID`.

## Frecuencia

Busca cada 10 minutos (`cron` en `.github/workflows/monitor.yml`). Como el repo es
público, los minutos de GitHub Actions son ilimitados. Si lo vuelves privado, solo hay
2000 minutos gratis al mes: sube el cron a `*/30 * * * *` (unos 1450 min/mes).

El repo no contiene datos personales: el token, los chat_id y los médicos van en los
secretos, y con Telegram configurado el texto de los mensajes no se escribe en los logs.

GitHub desactiva los workflows programados de los repos públicos tras 60 días sin
actividad; el paso "Mantener activo" del workflow lo evita.

Para buscar todavía más a menudo, puedes ejecutarlo en un PC o una Raspberry que esté
siempre encendido:
  ```bash
  export TELEGRAM_BOT_TOKEN=... MONITOR_CONFIG='[...]'
  python3 monitor.py --loop 300 --heartbeat   # cada 5 minutos
  ```

Ten en cuenta que GitHub puede retrasar unos minutos las ejecuciones programadas.
