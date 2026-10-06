#!/usr/bin/env python3
"""Vigila la agenda de un médico de Vithas y avisa por Telegram cuando hay huecos.

Usa la API interna de la web de citas (https://areaprivada.vithas.es), la misma
que llama el navegador al pedir cita sin identificarse. Solo depende de la
biblioteca estándar de Python.

Uso:
    python monitor.py                  # una comprobación (lo que ejecuta GitHub Actions)
    python monitor.py --loop 300       # comprobar cada 300 s (para un PC / Raspberry)
    python monitor.py --heartbeat      # además avisa "sigo buscando" aunque no haya huecos
    python monitor.py --test-notify    # manda un mensaje de prueba a Telegram
    python monitor.py --telegram-chat-id   # muestra el chat_id tras escribir al bot

Variables de entorno:
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   credenciales del bot (si faltan, solo imprime)
    STATE_FILE                             fichero de estado (por defecto state.json)
    VITHAS_CONFIG                          JSON con el médico/hospital/seguro a vigilar,
                                           con las mismas claves que CONFIG (ver README)
    VITHAS_<CLAVE>                         alternativa a VITHAS_CONFIG, una variable por
                                           clave (p. ej. VITHAS_DOCTOR_ID)
"""

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://app-services-prod.vithas.es/api"
BOOKING_URL = "https://areaprivada.vithas.es/appointment/step-1?lang=es"


def env(name, default):
    return os.environ.get(name) or default


# Qué vigilar. Los IDs no están en el código (el repo puede ser público): vienen
# del secreto VITHAS_CONFIG (JSON) o de variables VITHAS_<CLAVE>. Se sacan de
# /appointments/v1/filter/* y /appointments/v1/filterdoctor/* (ver README).
DEFAULTS = {
    "doctor_name": "el médico",
    "hospital_name": "",
    "insurance_name": "",
    "instance_id": "tt_portal_vithas_one_prod",
    "hospital_id": "",
    "doctor_id": "",
    "insurance_id": "",
    "is_private": "0",
    # Nombres de actividad a vigilar, separados por comas ("" = todas),
    # p. ej. "Consulta,Revision".
    "activities": "",
    # La web busca en bloques: si no hay huecos ofrece "buscar a partir de"
    # ~10 semanas después. Miramos varios bloques para cubrir ~1 año.
    "window_days": 70,
    "windows": 5,
}
REQUIRED = ("hospital_id", "doctor_id", "insurance_id")


def load_config():
    cfg = dict(DEFAULTS)
    raw = os.environ.get("VITHAS_CONFIG", "").strip()
    if raw:
        try:
            cfg.update(json.loads(raw))
        except json.JSONDecodeError as e:
            sys.exit(f"VITHAS_CONFIG no es un JSON válido: {e}")
    for key in DEFAULTS:
        cfg[key] = env(f"VITHAS_{key.upper()}", cfg[key])
    cfg["is_private"] = str(cfg["is_private"]).lower()
    cfg["window_days"] = int(cfg["window_days"])
    cfg["windows"] = int(cfg["windows"])
    return cfg


CONFIG = load_config()
STATE_FILE = env("STATE_FILE", "state.json")
FAILURE_ALERT_THRESHOLD = 3


# --------------------------------------------------------------------------- API

def api_get(path, params, retries=3):
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "platform": "web",
        "accept": "application/json, text/plain, */*",
        "accept-language": "es",
        "referer": "https://areaprivada.vithas.es/",
        "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/141.0 Safari/537.36",
    })
    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = e
            if isinstance(e, urllib.error.HTTPError):
                body = e.read().decode("utf-8", "replace")[:300]
                last_err = RuntimeError(f"HTTP {e.code} en {path}: {body}")
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Fallo consultando {path}: {last_err}")


def get_activities(cfg):
    """Actividades (Consulta, Revision...) que el médico ofrece para ese seguro."""
    data = api_get("/appointments/v1/filterdoctor/speciality", {
        "doctorId": cfg["doctor_id"],
        "instanceTuotempoId": cfg["instance_id"],
        "hospitalTuotempoId": cfg["hospital_id"],
        "isPrivate": cfg["is_private"],
        "insuranceId": cfg["insurance_id"],
    })
    wanted = {a.strip().lower() for a in cfg["activities"].split(",") if a.strip()}
    activities = []
    specs = data.get("coveredSpecialities", [])
    if cfg["is_private"] in ("1", "true"):
        # Sin seguro (pago privado) la API las devuelve como "no cubiertas".
        specs = specs + data.get("specialitiesNotCovered", [])
    for spec in specs:
        for act in spec.get("activityData", []):
            if not wanted or act["activityName"].strip().lower() in wanted:
                activities.append((act["activityId"], act["activityName"]))
    if not activities:
        raise RuntimeError("El médico no tiene actividades cubiertas para este seguro "
                           "(¿ha cambiado algún ID?).")
    return activities


def get_slots(cfg, activity_id, start_date=None):
    params = {
        "instanceTuotempoId": cfg["instance_id"],
        "hospitalTuotempoId": cfg["hospital_id"],
        "insuranceId": cfg["insurance_id"],
        "doctorId": cfg["doctor_id"],
        "activityId": activity_id,
    }
    if start_date:
        params["startDate"] = start_date.strftime("%Y-%m-%dT00:00:00")
    data = api_get("/appointments/v1/appointments/availabilities", params)
    if not isinstance(data, list):
        raise RuntimeError(f"Respuesta inesperada de availabilities ({type(data).__name__})")
    slots = []
    for day in data:
        for by_time in day.get("availabilitiesByTime", []):
            for av in by_time.get("availabilities", []):
                slots.append(av["startDateTime"])
    return slots


def check(cfg):
    """Devuelve {actividad: [startDateTime, ...]} con todos los huecos encontrados."""
    today = dt.date.today()
    activities = get_activities(cfg)
    starts = [None] + [today + dt.timedelta(days=i * cfg["window_days"])
                       for i in range(1, cfg["windows"])]
    # La API tarda ~10 s por llamada; en paralelo para no gastar minutos de Actions.
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        futures = {(act_id, act_name, start): pool.submit(get_slots, cfg, act_id, start)
                   for act_id, act_name in activities for start in starts}
        found = {act_name: set() for _, act_name in activities}
        for (_, act_name, _), fut in futures.items():
            found[act_name].update(fut.result())
    return {act: sorted(slots) for act, slots in found.items()}


# ---------------------------------------------------------------------- Telegram

def telegram(text, silent=False):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print(f"--- mensaje ---\n{text}\n---------------")
        print("(TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID no configurados: no se envía)")
        return
    # Con Telegram configurado no imprimimos el texto: los logs de Actions de un
    # repo público son visibles y el mensaje lleva el nombre del médico.
    body = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
        # Los "sigo buscando" llegan sin sonido; las citas y los fallos, con sonido.
        "disable_notification": "true" if silent else "false",
    }).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body)
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Telegram respondió {resp.status}")
    print("Mensaje enviado a Telegram.")


def print_chat_ids():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("Define TELEGRAM_BOT_TOKEN primero.")
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30) as r:
        updates = json.load(r).get("result", [])
    chats = {}
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat.get("first_name") or chat.get("title") or ""
    if not chats:
        print("No hay mensajes. Escribe /start a tu bot en Telegram y vuelve a ejecutar.")
    for cid, name in chats.items():
        print(f"TELEGRAM_CHAT_ID={cid}  ({name})")


# ------------------------------------------------------------------------- State

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def fmt_slot(iso):
    d = dt.datetime.fromisoformat(iso)
    days = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
    return f"{days[d.weekday()]} {d:%d/%m/%Y %H:%M}"


def header(cfg):
    extra = " · ".join(x for x in (cfg["hospital_name"], cfg["insurance_name"]) if x)
    return f"<b>{cfg['doctor_name']}</b>" + (f"\n{extra}" if extra else "")


def local_time():
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("Europe/Madrid")).strftime("%H:%M")
    except Exception:  # noqa: BLE001 - sin base de datos de zonas horarias
        return dt.datetime.now(dt.timezone.utc).strftime("%H:%M UTC")


def run_once(cfg, heartbeat=False):
    state = load_state()
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    try:
        found = check(cfg)
    except Exception as e:  # noqa: BLE001 - cualquier fallo cuenta como fallo de la comprobación
        failures = state.get("consecutive_failures", 0) + 1
        state.update(consecutive_failures=failures, last_error=str(e), last_error_at=now)
        print(f"ERROR ({failures} seguidos): {e}", file=sys.stderr)
        if failures == FAILURE_ALERT_THRESHOLD:
            telegram(f"⚠️ El vigilante de citas Vithas está fallando "
                     f"({failures} veces seguidas).\n\n{header(cfg)}\n\n"
                     f"Error: <code>{str(e)[:500]}</code>\n\n"
                     f"Mientras tanto, revisa a mano: {BOOKING_URL}")
        elif heartbeat:
            telegram(f"❌ {local_time()} La búsqueda ha fallado ({failures} seguidas): "
                     f"<code>{str(e)[:200]}</code>", silent=True)
        save_state(state)
        # Salimos con 0 para que GitHub no mande un email por cada ejecución
        # fallida: el aviso de fallos ya va por Telegram.
        return 0

    if state.get("consecutive_failures", 0) >= FAILURE_ALERT_THRESHOLD:
        telegram(f"✅ El vigilante de citas Vithas vuelve a funcionar.\n\n{header(cfg)}")

    seen = set(state.get("seen_slots", []))
    current = {f"{act}|{s}" for act, slots in found.items() for s in slots}
    new = current - seen
    total = len(current)
    print(f"{now} huecos: {total} ({len(new)} nuevos) -> "
          + ", ".join(f"{a}: {len(s)}" for a, s in found.items()))

    if new:
        lines = []
        for act, slots in found.items():
            new_for_act = [s for s in slots if f"{act}|{s}" in new]
            if new_for_act:
                lines.append(f"<b>{act}</b> ({len(new_for_act)} nuevos):")
                lines += [f"  • {fmt_slot(s)}" for s in new_for_act[:15]]
                if len(new_for_act) > 15:
                    lines.append(f"  … y {len(new_for_act) - 15} más")
        telegram("🚨 <b>¡HAY CITAS DISPONIBLES!</b>\n\n" + header(cfg) + "\n\n"
                 + "\n".join(lines)
                 + f"\n\n👉 Reserva ya: {BOOKING_URL}")
    elif heartbeat:
        estado = (f"{total} huecos (ya avisados, sigue habiendo)" if total
                  else "sin huecos todavía")
        telegram(f"👀 {local_time()} Sigo buscando: {estado}.\n{header(cfg)}", silent=True)

    state.update(
        consecutive_failures=0,
        last_check_at=now,
        last_total=total,
        # Recordamos los huecos que siguen existiendo; si uno desaparece y vuelve
        # a aparecer (cancelación), se avisará de nuevo.
        seen_slots=sorted(current),
    )
    save_state(state)
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--loop", type=int, metavar="SEGUNDOS", help="repetir cada N segundos")
    p.add_argument("--heartbeat", action="store_true", help='avisar "sigo buscando" aunque no haya novedades')
    p.add_argument("--test-notify", action="store_true", help="enviar un mensaje de prueba a Telegram")
    p.add_argument("--telegram-chat-id", action="store_true", help="mostrar los chat_id que han escrito al bot")
    args = p.parse_args()

    if args.telegram_chat_id:
        print_chat_ids()
        return 0
    missing = [k for k in REQUIRED if not CONFIG[k]]
    if missing:
        sys.exit("Falta configurar " + ", ".join(missing) + ": define el secreto "
                 "VITHAS_CONFIG (ver README).")
    if args.test_notify:
        telegram(f"🔔 Prueba: las alertas de citas Vithas funcionan.\n\n{header(CONFIG)}")
        return 0
    if args.loop:
        while True:
            run_once(CONFIG, heartbeat=args.heartbeat)
            time.sleep(args.loop)
    return run_once(CONFIG, heartbeat=args.heartbeat)


if __name__ == "__main__":
    sys.exit(main())
