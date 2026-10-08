#!/usr/bin/env python3
"""Vigila la agenda de médicos de Vithas y Quirónsalud y avisa por Telegram cuando hay huecos.

Usa las APIs internas de las webs de citas (https://areaprivada.vithas.es y
https://www.quironsalud.com/es/cita-medica), las mismas que llama el navegador al
pedir cita sin identificarse. Solo depende de la biblioteca estándar de Python.

Una ejecución revisa todos los "vigilantes" configurados; cada uno tiene su médico
y su chat de Telegram, así que se pueden avisar a personas distintas.

Uso:
    python monitor.py                  # una comprobación (lo que ejecuta GitHub Actions)
    python monitor.py --loop 300       # comprobar cada 300 s (para un PC / Raspberry)
    python monitor.py --heartbeat      # además avisa "sigo buscando" aunque no haya huecos
    python monitor.py --test-notify    # manda un mensaje de prueba a cada chat
    python monitor.py --telegram-chat-id   # muestra el chat_id tras escribir al bot
    python monitor.py --check-config   # comprueba MONITOR_CONFIG sin buscar nada

Variables de entorno:
    MONITOR_CONFIG        JSON con la lista de vigilantes (ver README)
    TELEGRAM_BOT_TOKEN    token del bot (cada vigilante puede usar otro con "bot_token")
    STATE_FILE            fichero de estado (por defecto state.json)
    VITHAS_CONFIG, TELEGRAM_CHAT_ID
                          formato antiguo (un solo médico de Vithas); se usa si no hay
                          MONITOR_CONFIG
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

STATE_FILE = os.environ.get("STATE_FILE") or "state.json"
FAILURE_ALERT_THRESHOLD = 3
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/141.0 Safari/537.36")


def http_json(url, headers, data=None, retries=3):
    req = urllib.request.Request(url, data=data, headers={"user-agent": USER_AGENT, **headers})
    path = urllib.parse.urlsplit(url).path
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


def wanted_names(csv):
    return {a.strip().lower() for a in csv.split(",") if a.strip()}


# ------------------------------------------------------------------------ Vithas

class Vithas:
    API = "https://app-services-prod.vithas.es/api"
    BOOKING_URL = "https://areaprivada.vithas.es/appointment/step-1?lang=es"
    DEFAULTS = {
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

    def __init__(self, cfg):
        self.cfg = cfg
        cfg["is_private"] = str(cfg["is_private"]).lower()
        cfg["window_days"] = int(cfg["window_days"])
        cfg["windows"] = int(cfg["windows"])

    def get(self, path, params):
        return http_json(f"{self.API}{path}?{urllib.parse.urlencode(params)}", {
            "platform": "web",
            "accept": "application/json, text/plain, */*",
            "accept-language": "es",
            "referer": "https://areaprivada.vithas.es/",
        })

    def get_activities(self):
        """Actividades (Consulta, Revision...) que el médico ofrece para ese seguro."""
        cfg = self.cfg
        data = self.get("/appointments/v1/filterdoctor/speciality", {
            "doctorId": cfg["doctor_id"],
            "instanceTuotempoId": cfg["instance_id"],
            "hospitalTuotempoId": cfg["hospital_id"],
            "isPrivate": cfg["is_private"],
            "insuranceId": cfg["insurance_id"],
        })
        wanted = wanted_names(cfg["activities"])
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

    def get_slots(self, activity_id, start_date=None):
        cfg = self.cfg
        params = {
            "instanceTuotempoId": cfg["instance_id"],
            "hospitalTuotempoId": cfg["hospital_id"],
            "insuranceId": cfg["insurance_id"],
            "doctorId": cfg["doctor_id"],
            "activityId": activity_id,
        }
        if start_date:
            params["startDate"] = start_date.strftime("%Y-%m-%dT00:00:00")
        data = self.get("/appointments/v1/appointments/availabilities", params)
        if not isinstance(data, list):
            raise RuntimeError(f"Respuesta inesperada de availabilities ({type(data).__name__})")
        slots = []
        for day in data:
            for by_time in day.get("availabilitiesByTime", []):
                for av in by_time.get("availabilities", []):
                    slots.append(av["startDateTime"])
        return slots

    def check(self):
        """Devuelve {actividad: [fecha ISO, ...]} con todos los huecos encontrados."""
        today = dt.date.today()
        activities = self.get_activities()
        starts = [None] + [today + dt.timedelta(days=i * self.cfg["window_days"])
                           for i in range(1, self.cfg["windows"])]
        # La API tarda ~10 s por llamada; en paralelo para no gastar minutos de Actions.
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            futures = {(act_name, start): pool.submit(self.get_slots, act_id, start)
                       for act_id, act_name in activities for start in starts}
            found = {act_name: set() for _, act_name in activities}
            for (act_name, _), fut in futures.items():
                found[act_name].update(fut.result())
        return {act: sorted(slots) for act, slots in found.items()}


# ------------------------------------------------------------------- Quirónsalud

class Quiron:
    API = "https://www.quironsalud.com/idcsalud-client/cm/quironsalud/pdp-api/v1"
    BOOKING_URL = "https://www.quironsalud.com/es/cita-medica"
    DEFAULTS = {
        "professional_id": "",       # idProfesionalPdP
        "specialty_id": "",          # idEspecialidadGlobal
        "province_id": "",           # idProvincia
        "center_id": "",             # idCentro (hospital)
        "associated_center_id": "",  # codCentroAsociado
        # idCentroAsistencial de la aseguradora/colectivo ("" = pago privado).
        "insurance_id": "",
        # Nombres de prestación a vigilar, separados por comas ("" = todas),
        # p. ej. "Consulta Primera Pie y Tobillo".
        "services": "",
    }
    REQUIRED = ("professional_id", "specialty_id", "province_id", "center_id",
                "associated_center_id")

    def __init__(self, cfg):
        self.cfg = cfg

    def post(self, path, params):
        return http_json(f"{self.API}{path}", {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
            "referer": self.BOOKING_URL,
        }, data=urllib.parse.urlencode(params).encode())

    def get_services(self):
        """Prestaciones (Consulta Primera, Sucesiva...) que se pueden citar con el médico."""
        cfg = self.cfg
        data = self.post("/catalog/professionals/services/channels", {
            "idProvincia": cfg["province_id"],
            "idCentro": cfg["center_id"],
            "codCentroAsociado": cfg["associated_center_id"],
            "idProfesionalPdP": cfg["professional_id"],
            "idEspecialidadGlobal": cfg["specialty_id"],
            "financialType": "2",  # la web siempre lo pide así, con o sin seguro
        })
        wanted = wanted_names(cfg["services"])
        services = []
        for svc in data:
            if not (svc.get("publicable") and svc.get("citable")):
                continue
            for ch in svc.get("canales", []):
                name = ch["nombrePrestacion"]
                if ch.get("publicable") and (not wanted or name.strip().lower() in wanted):
                    services.append((ch["idPrestacionGlobal"], ch["codCitacion"], name))
        if not services:
            raise RuntimeError("El médico no tiene prestaciones citables "
                               "(¿ha cambiado algún ID?).")
        return services

    def get_slots(self, service_id, channel):
        cfg = self.cfg
        insured = bool(cfg["insurance_id"])
        data = self.post("/citas/huecos", {
            "codCitacion": channel,
            "idCentro": cfg["center_id"],
            "codCentroAsociado": cfg["associated_center_id"],
            "idPrestacion": service_id,
            "idGarante": cfg["insurance_id"] if insured else "0",
            "idEspecialidad": cfg["specialty_id"],
            "financialType": "1" if insured else "2",
            "garanteHIS": "false",
            "espPresHIS": "false",
            "fechaInicio": dt.date.today().strftime("%d/%m/%Y"),
            "horaInicio": "000000",
            "idProvincia": cfg["province_id"],
            "idProfesionalPdP": cfg["professional_id"],
            # SIMPLE devuelve los huecos desde fechaInicio en adelante (~4 meses).
            "tipoBusqueda": "SIMPLE",
        })
        if not isinstance(data, list):
            raise RuntimeError(f"Respuesta inesperada de huecos ({type(data).__name__})")
        return [dt.datetime.strptime(f"{g['fechaCitaStr']} {g['horaCitaStr']}", "%d/%m/%Y %H:%M")
                .isoformat() for g in data if g.get("valido", True)]

    def check(self):
        """Devuelve {prestación: [fecha ISO, ...]} con todos los huecos encontrados."""
        services = self.get_services()
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            futures = {name: pool.submit(self.get_slots, sid, ch) for sid, ch, name in services}
            found = {name: set() for name in futures}
            for name, fut in futures.items():
                found[name].update(fut.result())
        return {name: sorted(slots) for name, slots in found.items()}


PROVIDERS = {"vithas": Vithas, "quiron": Quiron}
PROVIDER_NAMES = {"vithas": "Vithas", "quiron": "Quirónsalud"}


# ------------------------------------------------------------------------ Config

COMMON_DEFAULTS = {
    # false = no se vigila (se conserva lo ya avisado por si se vuelve a activar).
    "enabled": True,
    "doctor_name": "el médico",
    "hospital_name": "",
    "insurance_name": "",
    # Solo avisa de huecos en los próximos N días (0 = sin límite).
    "max_days": 0,
    # Mandar el "sigo buscando" en cada búsqueda (si se ejecuta con --heartbeat).
    "heartbeat": True,
}


def as_bool(value):
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "si", "sí")
    return bool(value)


def load_watches():
    """Lista de vigilantes: cada uno es un dict con provider, enabled, chat_ids, id y su config."""
    raw = os.environ.get("MONITOR_CONFIG", "").strip()
    if raw:
        try:
            watches = json.loads(raw)
        except json.JSONDecodeError as e:
            sys.exit(f"MONITOR_CONFIG no es un JSON válido: {e}")
        if isinstance(watches, dict):
            watches = watches.get("watches", [watches])
    elif os.environ.get("VITHAS_CONFIG", "").strip():
        # Formato antiguo: un médico de Vithas y un único chat.
        try:
            legacy = json.loads(os.environ["VITHAS_CONFIG"])
        except json.JSONDecodeError as e:
            sys.exit(f"VITHAS_CONFIG no es un JSON válido: {e}")
        watches = [{"id": "vithas", "provider": "vithas",
                    "chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""), **legacy}]
    else:
        sys.exit("Falta configurar MONITOR_CONFIG (ver README).")

    result, errors = [], []
    for i, w in enumerate(watches):
        provider = str(w.get("provider", "")).lower()
        if provider not in PROVIDERS:
            errors.append(f"vigilante {i}: provider debe ser uno de {', '.join(PROVIDERS)}")
            continue
        cls = PROVIDERS[provider]
        cfg = {**COMMON_DEFAULTS, **cls.DEFAULTS, **w, "provider": provider}
        cfg["id"] = str(w.get("id") or f"{provider}-{i}")
        cfg["enabled"] = as_bool(cfg["enabled"])
        cfg["heartbeat"] = as_bool(cfg["heartbeat"])
        # "chat_ids" es una lista; "chat_id" (uno solo) se acepta por comodidad.
        chats = cfg.pop("chat_id", None) or cfg.get("chat_ids") or []
        cfg["chat_ids"] = [str(c) for c in (chats if isinstance(chats, list) else [chats]) if c]
        cfg["max_days"] = int(cfg["max_days"])
        result.append(cfg)
        if not cfg["enabled"]:
            continue  # uno desactivado puede estar a medio configurar
        missing = [k for k in cls.REQUIRED if not cfg[k]]
        if not cfg["chat_ids"]:
            missing.append("chat_ids")
        if missing:
            errors.append(f"vigilante {cfg['id']}: falta {', '.join(missing)}")
    ids = [w["id"] for w in result]
    if len(ids) != len(set(ids)):
        errors.append("hay vigilantes con el mismo id")
    if errors:
        sys.exit("Configuración incorrecta:\n  " + "\n  ".join(errors))
    return result


# ---------------------------------------------------------------------- Telegram

def tg_call(token, method, params):
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                 data=urllib.parse.urlencode(params).encode())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)["result"]
    except urllib.error.HTTPError as e:
        try:
            desc = json.loads(e.read()).get("description", "")
        except ValueError:
            desc = ""
        raise RuntimeError(f"Telegram {method}: HTTP {e.code} {desc}") from None


def telegram(watch, text, wstate=None, status=False):
    """Manda `text` a todos los chats del vigilante.

    Los avisos (citas, fallos) son mensajes nuevos con sonido. Los de estado
    ("sigo buscando", status=True) van sin sonido y editan el último mensaje de
    estado de cada chat en vez de mandar otro; tras un aviso se empieza uno nuevo,
    para que el estado quede siempre debajo del último aviso.
    """
    token = watch.get("bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN")
    status_msgs = {} if wstate is None else wstate.setdefault("status_msgs", {})
    if not status:
        status_msgs.clear()
    for chat_id in list(status_msgs):
        if chat_id not in watch["chat_ids"]:
            del status_msgs[chat_id]
    if not token or not watch["chat_ids"]:
        print(f"--- mensaje [{watch['id']}] ---\n{text}\n---------------")
        print("(bot_token/chat_id no configurados: no se envía)")
        return
    # Con Telegram configurado no imprimimos el texto: los logs de Actions de un
    # repo público son visibles y el mensaje lleva el nombre del médico.
    for chat_id in watch["chat_ids"]:
        params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": "true"}
        try:
            msg_id = status_msgs.get(chat_id) if status else None
            if msg_id:
                try:
                    tg_call(token, "editMessageText", {**params, "message_id": msg_id})
                    print(f"[{watch['id']}] Mensaje de estado actualizado en Telegram.")
                    continue
                except RuntimeError as e:
                    if "message is not modified" in str(e):
                        continue
                    # Lo han borrado o ya no se puede editar: mandamos uno nuevo.
                    print(f"[{watch['id']}] No se pudo editar el estado ({e}); se manda otro.")
            msg = tg_call(token, "sendMessage", {
                **params,
                # Los "sigo buscando" llegan sin sonido; las citas y los fallos, con sonido.
                "disable_notification": "true" if status else "false",
            })
            if status:
                status_msgs[chat_id] = msg["message_id"]
        except Exception as e:  # noqa: BLE001 - un chat roto no debe tumbar a los demás
            print(f"[{watch['id']}] Error enviando a Telegram: {e}", file=sys.stderr)
            continue
        print(f"[{watch['id']}] Mensaje enviado a Telegram.")


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
        print(f"chat_id={cid}  ({name})")


# ------------------------------------------------------------------------- State

def load_state(watches):
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    if state and "watches" not in state:
        # Estado del formato antiguo (un solo médico de Vithas): se lo damos al
        # primer vigilante de Vithas para no repetir avisos de huecos ya conocidos.
        first = next((w["id"] for w in watches if w["provider"] == "vithas"), None)
        state = {"watches": {first: state} if first else {}}
    state.setdefault("watches", {})
    return state


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def fmt_slot(iso):
    d = dt.datetime.fromisoformat(iso)
    days = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
    return f"{days[d.weekday()]} {d:%d/%m/%Y %H:%M}"


def header(w):
    extra = " · ".join(x for x in (PROVIDER_NAMES[w["provider"]], w["hospital_name"],
                                   w["insurance_name"]) if x)
    return f"<b>{w['doctor_name']}</b>\n{extra}"


def local_now():
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("Europe/Madrid"))
    except Exception:  # noqa: BLE001 - sin base de datos de zonas horarias
        return dt.datetime.now(dt.timezone.utc)


def run_watch(w, wstate, heartbeat=False):
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    hhmm = local_now().strftime("%H:%M")
    heartbeat = heartbeat and w["heartbeat"]
    booking_url = PROVIDERS[w["provider"]].BOOKING_URL
    try:
        found = PROVIDERS[w["provider"]](w).check()
    except Exception as e:  # noqa: BLE001 - cualquier fallo cuenta como fallo de la comprobación
        failures = wstate.get("consecutive_failures", 0) + 1
        wstate.update(consecutive_failures=failures, last_error=str(e), last_error_at=now)
        print(f"[{w['id']}] ERROR ({failures} seguidos): {e}", file=sys.stderr)
        if failures == FAILURE_ALERT_THRESHOLD:
            telegram(w, f"⚠️ El vigilante de citas está fallando "
                        f"({failures} veces seguidas).\n\n{header(w)}\n\n"
                        f"Error: <code>{str(e)[:500]}</code>\n\n"
                        f"Mientras tanto, revisa a mano: {booking_url}", wstate)
        elif heartbeat:
            telegram(w, f"❌ {hhmm} La búsqueda ha fallado ({failures} seguidas): "
                        f"<code>{str(e)[:200]}</code>\n{header(w)}", wstate, status=True)
        return

    if wstate.get("consecutive_failures", 0) >= FAILURE_ALERT_THRESHOLD:
        telegram(w, f"✅ El vigilante de citas vuelve a funcionar.\n\n{header(w)}", wstate)

    # Solo cuentan los huecos dentro del plazo; los de más allá se mencionan en
    # el "sigo buscando" para saber cómo va la agenda.
    later = []
    if w["max_days"]:
        limit = (local_now() + dt.timedelta(days=w["max_days"])).replace(tzinfo=None)
        later = sorted(s for slots in found.values() for s in slots
                       if dt.datetime.fromisoformat(s) > limit)
        found = {act: [s for s in slots if dt.datetime.fromisoformat(s) <= limit]
                 for act, slots in found.items()}

    seen = set(wstate.get("seen_slots", []))
    current = {f"{act}|{s}" for act, slots in found.items() for s in slots}
    new = current - seen
    total = len(current)
    print(f"[{w['id']}] {now} huecos: {total} ({len(new)} nuevos) -> "
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
        telegram(w, "🚨 <b>¡HAY CITAS DISPONIBLES!</b>\n\n" + header(w) + "\n\n"
                    + "\n".join(lines)
                    + f"\n\n👉 Reserva ya: {booking_url}", wstate)
    elif heartbeat:
        plazo = f" en los próximos {w['max_days']} días" if w["max_days"] else ""
        estado = (f"{total} huecos{plazo} (ya avisados, sigue habiendo)" if total
                  else f"sin huecos todavía{plazo}")
        if not total and later:
            estado += f"; el primero libre es el {fmt_slot(later[0])}"
        telegram(w, f"👀 {hhmm} Sigo buscando: {estado}.\n{header(w)}", wstate, status=True)

    wstate.update(
        consecutive_failures=0,
        last_check_at=now,
        last_total=total,
        # Recordamos los huecos que siguen existiendo; si uno desaparece y vuelve
        # a aparecer (cancelación), se avisará de nuevo.
        seen_slots=sorted(current),
    )


def run_once(watches, heartbeat=False):
    state = load_state(watches)
    for w in watches:
        if w["enabled"]:
            run_watch(w, state["watches"].setdefault(w["id"], {}), heartbeat=heartbeat)
        else:
            print(f"[{w['id']}] desactivado, no se vigila.")
    # Olvidamos el estado de vigilantes que ya no están configurados.
    state["watches"] = {w["id"]: state["watches"][w["id"]]
                        for w in watches if w["id"] in state["watches"]}
    save_state(state)
    # Salimos con 0 aunque falle algo para que GitHub no mande un email por cada
    # ejecución fallida: el aviso de fallos ya va por Telegram.
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--loop", type=int, metavar="SEGUNDOS", help="repetir cada N segundos")
    p.add_argument("--heartbeat", action="store_true", help='avisar "sigo buscando" aunque no haya novedades')
    p.add_argument("--test-notify", action="store_true", help="enviar un mensaje de prueba a cada chat")
    p.add_argument("--telegram-chat-id", action="store_true", help="mostrar los chat_id que han escrito al bot")
    p.add_argument("--check-config", action="store_true", help="comprobar MONITOR_CONFIG y resumirla")
    args = p.parse_args()

    if args.telegram_chat_id:
        print_chat_ids()
        return 0
    watches = load_watches()
    if args.check_config:
        for w in watches:
            print(f"{w['id']}: {PROVIDER_NAMES[w['provider']]}, {w['doctor_name']}, "
                  + ("activo" if w["enabled"] else "desactivado")
                  + f", {len(w['chat_ids'])} chat(s)"
                  + (f", próximos {w['max_days']} días" if w["max_days"] else ""))
        return 0
    if args.test_notify:
        for w in watches:
            if w["enabled"]:
                telegram(w, f"🔔 Prueba: las alertas de citas funcionan.\n\n{header(w)}")
        return 0
    if args.loop:
        while True:
            run_once(watches, heartbeat=args.heartbeat)
            time.sleep(args.loop)
    return run_once(watches, heartbeat=args.heartbeat)


if __name__ == "__main__":
    sys.exit(main())
