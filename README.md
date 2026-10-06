# vithas-query

Vigila la agenda de un médico de **Vithas** (con un hospital y un seguro concretos) y
avisa por **Telegram** en cuanto aparecen huecos para reservar. Qué médico vigilar no
está en el código: se configura con un secreto (ver abajo).

## Cómo funciona

Vithas no tiene API pública, pero su web de citas (`areaprivada.vithas.es`) usa una API
interna que se puede llamar sin iniciar sesión:

| Paso | Endpoint (`https://app-services-prod.vithas.es/api`) |
|---|---|
| Actividades del médico (Consulta / Revision) | `GET /appointments/v1/filterdoctor/speciality` |
| Huecos disponibles | `GET /appointments/v1/appointments/availabilities` |

Las llamadas llevan la cabecera `platform: web`. Si no hay huecos, la API devuelve `[]`.
La web busca por bloques de unas 10 semanas, así que el script mira 5 bloques
seguidos (casi un año) para cada actividad.

`monitor.py`:
- avisa **solo de los huecos nuevos**: si un hueco desaparece y vuelve a salir (por
  ejemplo, por una cancelación), avisa otra vez;
- si falla 3 veces seguidas (la web cambia, está caída...), avisa por Telegram para que
  no te quedes esperando sin saberlo, y avisa también cuando se recupera;
- en cada búsqueda manda un "👀 Sigo buscando: sin huecos todavía" **sin sonido**, para
  que sepas que funciona; las citas nuevas (🚨) y los fallos (⚠️) llegan **con sonido**;
- solo usa la biblioteca estándar de Python 3, sin dependencias.

## Puesta en marcha

### 1. Crear el bot de Telegram

Un bot de Telegram no puede escribir a un número de teléfono: necesita un `chat_id`
y que tú le hayas escrito antes.

1. En Telegram, habla con **@BotFather**, envía `/newbot` y sigue los pasos. Te dará
   un **token** del tipo `123456:ABC-...`.
2. Abre el chat con tu bot nuevo y envíale `/start`.
3. Saca tu `chat_id`:
   ```bash
   TELEGRAM_BOT_TOKEN=123456:ABC-... python3 monitor.py --telegram-chat-id
   ```
   Si no tienes Python a mano, abre `https://api.telegram.org/bot<TOKEN>/getUpdates`
   en el navegador y busca `"chat":{"id": ...}`.

### 2. Configurar GitHub

En el repo, ve a **Settings → Secrets and variables → Actions → New repository
secret** y crea estos tres secretos:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `VITHAS_CONFIG`: el médico a vigilar, en JSON (ver "Qué médico vigilar" más abajo).

Los workflows programados solo se ejecutan desde la **rama por defecto** del repo. Si
mueves el código a otra rama (por ejemplo `main`), ponla como rama por defecto.

### 3. Probar

En **Actions → Vigilar citas Vithas → Run workflow**:
- elige `test-notify`: te debería llegar un mensaje de prueba;
- elige `check`: hace una búsqueda real y te manda el "sigo buscando".

A partir de ahí busca cada 30 minutos y te escribe en cada búsqueda.

## Frecuencia

Como el repo es privado, GitHub Actions regala 2000 minutos al mes. Con una
comprobación cada 30 minutos se gastan unos 1450 minutos al mes. Para comprobar más a
menudo tienes dos opciones:

- **Hacer el repo público**: los minutos pasan a ser ilimitados y puedes cambiar el
  cron a `*/10 * * * *` en `.github/workflows/monitor.yml`. El repo no contiene datos
  personales: el token, el chat_id y el médico van en los secretos, y con Telegram
  configurado el texto de los mensajes no se escribe en los logs.
- **Ejecutarlo en un PC o una Raspberry** que esté siempre encendido:
  ```bash
  export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... VITHAS_CONFIG='{...}'
  python3 monitor.py --loop 300 --heartbeat   # cada 5 minutos
  ```

Ten en cuenta que GitHub puede retrasar unos minutos las ejecuciones programadas.

## Qué médico vigilar (`VITHAS_CONFIG`)

Es un JSON como este:

```json
{
  "doctor_name": "Dr. Nombre Apellido (Especialidad)",
  "hospital_name": "Hospital Vithas ...",
  "insurance_name": "Nombre del seguro",
  "hospital_id": "sc...@tt_vithas_one_..._prod",
  "doctor_id": "sc...@tt_vithas_one_..._prod",
  "insurance_id": "sc..."
}
```

- Obligatorios: `hospital_id`, `doctor_id` e `insurance_id`. Los nombres solo se usan
  para el texto de los mensajes.
- Opcionales: `is_private` (`"1"` si pagas sin seguro), `activities` (por ejemplo
  `"Consulta"`; por defecto vigila todas), `windows` y `window_days` (cuánto mira
  hacia delante; por defecto 5 × 70 días).
- Cada clave se puede sobrescribir con una variable `VITHAS_<CLAVE>`, por ejemplo
  `VITHAS_DOCTOR_ID`.

Los IDs salen de la propia API, con la cabecera `platform: web`:
- `GET /appointments/v1/filter/hospital?idSite=<provincia>` (Madrid es `16`)
- `GET /appointments/v1/filter/insurance?instanceTuotempoId=tt_portal_vithas_one_prod&hospitalTuotempoId=<hospital>`
- `GET /appointments/v1/filter/doctors?...&insuranceId=<seguro>&specialityId=<especialidad>`
