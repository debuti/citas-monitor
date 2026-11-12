#!/usr/bin/env python3
"""Bot de Telegram de citas-monitor: cada familiar da de alta sus propias vigilancias.

Un solo proceso hace dos cosas:
  - escucha al bot (getUpdates, sin abrir puertos) y lleva el asistente
    /nueva -> proveedor -> médico -> seguro -> consulta -> plazo;
  - cada CHECK_INTERVAL segundos revisa todas las vigilancias (las de los usuarios,
    guardadas en SQLite, y las de MONITOR_CONFIG / MONITOR_CONFIG_FILE) con monitor.py.

Solo usan el bot los chats autorizados: el administrador (ADMIN_CHAT_ID) acepta o
rechaza con un botón a cada persona nueva que le escribe.

Variables de entorno:
    TELEGRAM_BOT_TOKEN     token del bot
    ADMIN_CHAT_ID          chat del administrador
    DATA_DIR               carpeta de datos (por defecto ./data): bot.db y state.json
    CHECK_INTERVAL         segundos entre revisiones (por defecto 600)
    MAX_WATCHES_PER_USER   vigilancias por persona (por defecto 5; el admin, sin límite)
    MONITOR_CONFIG[_FILE]  vigilancias fijas del administrador (opcional, ver README)
"""

import concurrent.futures
import datetime as dt
import html
import json
import os
import sqlite3
import sys
import threading
import time
import traceback

DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
os.environ.setdefault("STATE_FILE", os.path.join(DATA_DIR, "state.json"))

import catalog  # noqa: E402
import monitor  # noqa: E402

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN = str(os.environ.get("ADMIN_CHAT_ID", "")).strip()
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL") or 600)
MAX_WATCHES = int(os.environ.get("MAX_WATCHES_PER_USER") or 5)
MAX_BUTTONS = 12
DAYS = [15, 30, 60, 90, 0]

state_lock = threading.Lock()  # protege state.json (revisión periódica y altas nuevas)


def log(*args):
    print(dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *args, flush=True)


# ------------------------------------------------------------------------ Datos

class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.lock = threading.Lock()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id TEXT PRIMARY KEY, name TEXT, status TEXT NOT NULL,
                created TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS watches (
                id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, cfg TEXT NOT NULL,
                created TEXT DEFAULT CURRENT_TIMESTAMP);
        """)

    def q(self, sql, *args):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def user_status(self, chat_id):
        if chat_id == ADMIN:
            return "allowed"
        rows = self.q("SELECT status FROM users WHERE chat_id=?", chat_id)
        return rows[0][0] if rows else None

    def set_user(self, chat_id, name, status):
        self.q("INSERT INTO users(chat_id, name, status) VALUES(?,?,?) "
               "ON CONFLICT(chat_id) DO UPDATE SET status=excluded.status, "
               "name=COALESCE(excluded.name, users.name)", chat_id, name, status)

    def users(self):
        return self.q("SELECT chat_id, name, status FROM users ORDER BY created")

    def watches(self, chat_id=None):
        rows = (self.q("SELECT cfg FROM watches WHERE chat_id=? ORDER BY created", chat_id)
                if chat_id else self.q("SELECT cfg FROM watches ORDER BY created"))
        return [json.loads(r[0]) for r in rows]

    def add_watch(self, cfg):
        self.q("INSERT INTO watches(id, chat_id, cfg) VALUES(?,?,?)",
               cfg["id"], cfg["chat_ids"][0], json.dumps(cfg, ensure_ascii=False))

    def delete_watch(self, chat_id, watch_id):
        with self.lock:
            cur = self.db.execute("DELETE FROM watches WHERE id=? AND (chat_id=? OR ?=?)",
                                  (watch_id, chat_id, chat_id, ADMIN))
            return cur.rowcount

    def delete_user_watches(self, chat_id):
        self.q("DELETE FROM watches WHERE chat_id=?", chat_id)


store = None


# --------------------------------------------------------------------- Telegram

def tg(method, **params):
    for k, v in params.items():
        if isinstance(v, (dict, list)):
            params[k] = json.dumps(v, ensure_ascii=False)
    return monitor.tg_call(TOKEN, method, params)


def send(chat_id, text, buttons=None, silent=False):
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": "true",
              "disable_notification": "true" if silent else "false"}
    if buttons:
        params["reply_markup"] = {"inline_keyboard": buttons}
    try:
        return tg("sendMessage", **params)
    except Exception as e:  # noqa: BLE001
        log(f"Error enviando a {chat_id}: {e}")


def button(text, data):
    return [{"text": text[:60], "callback_data": data[:64]}]


# -------------------------------------------------------------------- Asistente

sessions = {}  # chat_id -> {"step": ..., "provider": ..., "cfg": {...}, "options": [...]}

HELP = ("Te aviso por aquí cuando salgan citas libres con el médico que me digas "
        "(Vithas, Quirónsalud o Viamed).\n\n"
        "/nueva — vigilar un médico\n"
        "/mis — ver o borrar tus vigilancias\n"
        "/cancelar — salir del asistente\n\n"
        "Reviso cada 10 minutos. La cita la reservas tú en la web con el enlace del aviso.")


def esc(text):
    return html.escape(str(text))


def start_wizard(chat_id):
    mine = store.watches(chat_id)
    if chat_id != ADMIN and len(mine) >= MAX_WATCHES:
        send(chat_id, f"Ya tienes {len(mine)} vigilancias, el máximo. Borra alguna con /mis.")
        return
    sessions[chat_id] = {"step": "provider", "cfg": {}}
    send(chat_id, "¿De qué hospital es el médico?",
         [button(name, f"prov:{p}") for p, name in monitor.PROVIDER_NAMES.items()]
         + [button("Cancelar", "cancel")])


def show_options(chat_id, sess, options, prefix, question, extra=None):
    sess["options"] = options[:MAX_BUTTONS]
    # El texto de los botones se corta en el móvil: la lista completa va en el mensaje.
    lines = [f"{i}. {esc(o['label'])}" for i, o in enumerate(sess["options"], 1)]
    rows = [button(f"{i}. {o['label']}", f"{prefix}:{i - 1}")
            for i, o in enumerate(sess["options"], 1)]
    send(chat_id, question + "\n\n" + "\n".join(lines),
         rows + (extra or []) + [button("Cancelar", "cancel")])


def on_text(chat_id, text):
    sess = sessions.get(chat_id)
    step = sess and sess["step"]
    if step == "doctor":
        if len(text.strip()) < 3:
            send(chat_id, "Escribe al menos 3 letras del apellido.")
            return
        send(chat_id, "🔎 Buscando…")
        try:
            options = catalog.DOCTORS[sess["provider"]](text)
        except Exception as e:  # noqa: BLE001
            log(f"Búsqueda de médico: {e}")
            send(chat_id, "La web del hospital no responde ahora. Prueba otra vez en un rato.")
            return
        if not options:
            send(chat_id, "No encuentro a nadie con ese nombre. Prueba solo con el primer "
                          "apellido, sin el nombre.")
        elif len(options) > MAX_BUTTONS:
            send(chat_id, f"Hay {len(options)} resultados. Escribe nombre y apellidos "
                          "para afinar.")
        else:
            show_options(chat_id, sess, options, "doc", "¿Cuál es?")
    elif step == "insurance":
        options = [o for o in sess["all_insurances"] if catalog.matches(text, o["label"])]
        if not options:
            send(chat_id, "Ningún seguro coincide. Prueba con otra palabra "
                          "(por ejemplo «adeslas», «sanitas» o «privado»).")
        elif len(options) > MAX_BUTTONS:
            send(chat_id, f"Coinciden {len(options)}. Escribe algo más concreto.")
        else:
            show_options(chat_id, sess, options, "ins", "¿Cuál es tu seguro?")
    else:
        send(chat_id, HELP)


def ask_insurance(chat_id, sess):
    send(chat_id, "🔎 Mirando qué seguros acepta…")
    try:
        sess["all_insurances"] = catalog.INSURANCES[sess["provider"]](sess["cfg"])
    except Exception as e:  # noqa: BLE001
        log(f"Seguros: {e}")
        send(chat_id, "La web del hospital no responde ahora. Prueba otra vez con /nueva "
                      "en un rato.")
        sessions.pop(chat_id, None)
        return
    sess["step"] = "insurance"
    if len(sess["all_insurances"]) <= MAX_BUTTONS:
        show_options(chat_id, sess, sess["all_insurances"], "ins", "¿Cuál es tu seguro?")
    else:
        send(chat_id, "¿Qué seguro tienes? Escribe su nombre (por ejemplo «adeslas», "
                      "«sanitas»…), o «privado» si pagas tú la consulta.")


def ask_activity(chat_id, sess):
    try:
        names = catalog.activities(sess["provider"], sess["cfg"])
    except Exception as e:  # noqa: BLE001
        log(f"Actividades: {e}")
        names = None
    if names is None or not names:
        send(chat_id, "Ese médico no da citas por internet con ese seguro (o la web no "
                      "responde ahora). Prueba otro seguro u otro médico con /nueva.")
        sessions.pop(chat_id, None)
        return
    sess["step"] = "activity"
    sess["activities"] = names
    if len(names) == 1:
        return ask_days(chat_id, sess, "")
    rows = [button(n, f"act:{i}") for i, n in enumerate(names[:40])]
    send(chat_id, "¿Qué tipo de cita buscas?",
         rows + [button("Cualquiera", "act:all"), button("Cancelar", "cancel")])


def ask_days(chat_id, sess, activity):
    key = "services" if sess["provider"] == "quiron" else "activities"
    sess["cfg"][key] = activity
    sess["step"] = "days"
    send(chat_id, "¿Hasta cuándo te sirve la cita?",
         [[{"text": f"{d} días" if d else "Sin límite", "callback_data": f"days:{d}"}
           for d in DAYS[:3]],
          [{"text": f"{d} días" if d else "Sin límite", "callback_data": f"days:{d}"}
           for d in DAYS[3:]],
          button("Cancelar", "cancel")])


def finish(chat_id, sess, days):
    cfg = {k: v for k, v in sess["cfg"].items() if not k.startswith("_")}
    cfg.update(provider=sess["provider"], chat_ids=[chat_id], max_days=days,
               id=f"u{chat_id}-{int(time.time())}")
    watches, errors = monitor.normalize_watches([cfg])
    if errors:
        send(chat_id, "Algo ha fallado al guardar. Avisa al administrador.")
        log(f"Vigilancia incompleta: {errors}")
        return
    store.add_watch(cfg)
    sessions.pop(chat_id, None)
    act = cfg.get("services") or cfg.get("activities") or "cualquier cita"
    send(chat_id, f"✅ Hecho. Vigilo a <b>{esc(cfg['doctor_name'])}</b>\n"
                  f"{esc(cfg.get('hospital_name', ''))} · {esc(cfg.get('insurance_name', ''))}\n"
                  f"{esc(act)}, "
                  + (f"en los próximos {days} días." if days else "sin límite de fecha.")
                  + "\n\nAhora miro si ya hay huecos; luego, cada 10 minutos.")
    if chat_id != ADMIN:
        send(ADMIN, f"ℹ️ {esc(user_name(chat_id))} vigila a {esc(cfg['doctor_name'])} "
                    f"({monitor.PROVIDER_NAMES[cfg['provider']]}).")
    threading.Thread(target=check_now, args=(watches[0],), daemon=True).start()


def fmt_when(iso):
    try:
        when = dt.datetime.fromisoformat(iso).astimezone(monitor.local_now().tzinfo)
    except (TypeError, ValueError):
        return "?"
    return when.strftime("%d/%m %H:%M")


def watch_status(wstate):
    """Línea de estado para el administrador: huecos ahora y avisos mandados."""
    if not wstate.get("last_check_at"):
        return "   ⏳ aún sin revisar"
    if wstate.get("consecutive_failures"):
        now = f"❌ falla ({wstate['consecutive_failures']} seguidas)"
    else:
        now = f"{wstate.get('last_total', 0)} huecos ahora"
    if wstate.get("alerts"):
        alerts = (f"🔔 {wstate['alerts']} aviso(s), el último {fmt_when(wstate['last_alert_at'])}"
                  f" ({wstate.get('last_alert_new', '?')} huecos)")
    else:
        alerts = "sin avisos todavía"
    return f"   {now} · {alerts}"


def list_watches(chat_id):
    mine = store.watches(None if chat_id == ADMIN else chat_id)
    if not mine:
        send(chat_id, "No tienes vigilancias. Crea una con /nueva.")
        return
    states = monitor.load_state([])["watches"] if chat_id == ADMIN else {}
    lines, rows = [], []
    for i, w in enumerate(mine, 1):
        act = w.get("services") or w.get("activities") or "cualquier cita"
        plazo = f"{w['max_days']} días" if w.get("max_days") else "sin límite"
        who = (f" — de {esc(user_name(w['chat_ids'][0]))}"
               if chat_id == ADMIN and w["chat_ids"][0] != ADMIN else "")
        lines.append(f"{i}. <b>{esc(w['doctor_name'])}</b>{who}\n"
                     f"   {esc(monitor.PROVIDER_NAMES[w['provider']])} · "
                     f"{esc(w.get('hospital_name', ''))} · {esc(w.get('insurance_name', ''))}\n"
                     f"   {esc(act)}, {plazo}"
                     + ("\n" + watch_status(states.get(w["id"], {})) if chat_id == ADMIN else ""))
        rows.append(button(f"🗑 Borrar {i}. {w['doctor_name']}", f"del:{w['id']}"))
    send(chat_id, "\n\n".join(lines), rows)


def user_name(chat_id):
    rows = store.q("SELECT name FROM users WHERE chat_id=?", chat_id)
    return (rows[0][0] if rows and rows[0][0] else None) or chat_id


def list_users(chat_id):
    users = store.users()
    if not users:
        send(chat_id, "Nadie más ha escrito al bot todavía. Cuando alguien lo haga, te "
                      "llegará un mensaje para aceptarlo o rechazarlo.")
        return
    rows = []
    for uid, name, status in users:
        n = len(store.watches(uid))
        estado = {"allowed": "con acceso", "pending": "pendiente",
                  "blocked": "sin acceso"}.get(status, status)
        label = f"{name or uid} · {estado} · {n} vig."
        rows.append(button(("❌ Quitar " if status == "allowed" else "✅ Dar acceso ") + label,
                           f"{'deny' if status == 'allowed' else 'allow'}:{uid}"))
    send(chat_id, "Personas que han escrito al bot:", rows)


# ------------------------------------------------------------------- Mensajes

def handle_message(msg):
    chat = msg.get("chat", {})
    if chat.get("type") != "private":
        return
    chat_id = str(chat["id"])
    text = (msg.get("text") or "").strip()
    name = " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) \
        or chat.get("username") or chat_id
    status = store.user_status(chat_id)
    if status != "allowed":
        if status is None:
            store.set_user(chat_id, name, "pending")
            send(chat_id, "Hola 👋 Este bot es privado. He pedido permiso para que lo "
                          "uses; te escribo cuando te lo den.")
            send(ADMIN, f"🙋 <b>{esc(name)}</b> quiere usar el bot de citas.",
                 [[{"text": "✅ Aceptar", "callback_data": f"allow:{chat_id}"},
                   {"text": "❌ Rechazar", "callback_data": f"deny:{chat_id}"}]])
        elif status == "pending":
            send(chat_id, "Todavía no me han dado permiso para atenderte.")
        return
    command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
    if command in ("/start", "/ayuda", "/help"):
        sessions.pop(chat_id, None)
        send(chat_id, HELP + ("\n\n/usuarios — gestionar quién usa el bot"
                              if chat_id == ADMIN else ""))
    elif command == "/nueva":
        start_wizard(chat_id)
    elif command == "/mis":
        sessions.pop(chat_id, None)
        list_watches(chat_id)
    elif command == "/cancelar":
        sessions.pop(chat_id, None)
        send(chat_id, "Vale, cancelado.")
    elif command == "/usuarios" and chat_id == ADMIN:
        list_users(chat_id)
    elif command:
        send(chat_id, HELP)
    else:
        on_text(chat_id, text)


def handle_callback(cb):
    chat_id = str(cb["message"]["chat"]["id"])
    data = cb.get("data", "")
    try:
        tg("answerCallbackQuery", callback_query_id=cb["id"])
    except Exception:  # noqa: BLE001
        pass
    if store.user_status(chat_id) != "allowed":
        return
    kind, _, arg = data.partition(":")
    sess = sessions.get(chat_id)

    if kind in ("allow", "deny") and chat_id == ADMIN:
        if kind == "allow":
            store.set_user(arg, None, "allowed")
            send(arg, "✅ Ya puedes usar el bot.\n\n" + HELP)
            send(ADMIN, f"Acceso dado a {esc(user_name(arg))}.")
        else:
            store.set_user(arg, None, "blocked")
            store.delete_user_watches(arg)
            send(ADMIN, f"Acceso quitado a {esc(user_name(arg))} (y sus vigilancias).")
    elif kind == "del":
        if store.delete_watch(chat_id, arg):
            send(chat_id, "🗑 Borrada. Ya no te aviso de ese médico.")
        else:
            send(chat_id, "Esa vigilancia ya no existe.")
    elif kind == "done":
        watch = next((w for w in store.watches() if w["id"] == arg), None)
        if watch and store.delete_watch(chat_id, arg):
            send(chat_id, f"🎉 ¡Genial! Dejo de vigilar a {esc(watch['doctor_name'])}. "
                          "Si necesitas otra, /nueva.")
            if chat_id != ADMIN:
                send(ADMIN, f"🎉 <b>{esc(user_name(chat_id))}</b> ha conseguido cita con "
                            f"{esc(watch['doctor_name'])} y ha dejado de vigilarlo.", silent=True)
        else:
            send(chat_id, "Esa vigilancia ya no existe.")
    elif kind == "keep":
        send(chat_id, "👍 Sigo buscando y te aviso de los próximos huecos.")
    elif kind == "cancel":
        sessions.pop(chat_id, None)
        send(chat_id, "Vale, cancelado.")
    elif not sess:
        send(chat_id, "Ese menú ha caducado. Empieza otra vez con /nueva.")
    elif kind == "prov" and sess["step"] == "provider" and arg in monitor.PROVIDERS:
        sess.update(provider=arg, step="doctor")
        send(chat_id, f"{monitor.PROVIDER_NAMES[arg]}. Escribe el apellido del médico.")
    elif kind == "doc" and sess["step"] == "doctor":
        sess["cfg"].update(sess["options"][int(arg)]["cfg"])
        ask_insurance(chat_id, sess)
    elif kind == "ins" and sess["step"] == "insurance":
        sess["cfg"].update(sess["options"][int(arg)]["cfg"])
        send(chat_id, "🔎 Mirando qué consultas tiene…")
        ask_activity(chat_id, sess)
    elif kind == "act" and sess["step"] == "activity":
        ask_days(chat_id, sess, "" if arg == "all" else sess["activities"][int(arg)])
    elif kind == "days" and sess["step"] == "days":
        finish(chat_id, sess, int(arg))
    else:
        send(chat_id, "Ese botón ya no vale. Sigue con la última pregunta o /cancelar.")


chat_locks = {}


def handle_update(update):
    if "callback_query" in update:
        chat_id = str(update["callback_query"]["message"]["chat"]["id"])
    elif "message" in update:
        chat_id = str(update["message"]["chat"]["id"])
    else:
        return
    # Los mensajes de un mismo chat, en orden; los de chats distintos, a la vez.
    with chat_locks.setdefault(chat_id, threading.Lock()):
        try:
            if "callback_query" in update:
                handle_callback(update["callback_query"])
            else:
                handle_message(update["message"])
        except Exception:  # noqa: BLE001
            log(f"Error atendiendo a {chat_id}:\n{traceback.format_exc()}")
            send(chat_id, "Ups, algo ha fallado. Prueba otra vez con /nueva.")


def poll_forever():
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    offset = None
    while True:
        try:
            params = {"timeout": 50, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                params["offset"] = offset
            updates = tg("getUpdates", **params)
        except Exception as e:  # noqa: BLE001
            log(f"getUpdates: {e}")
            time.sleep(10)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            pool.submit(handle_update, u)


# -------------------------------------------------------------------- Vigilante

def all_watches():
    fixed = monitor.load_watches(required=False)
    users, errors = monitor.normalize_watches(store.watches())
    if errors:
        log(f"Vigilancias de usuarios con errores: {errors}")
    ids = {w["id"] for w in fixed}
    return fixed + [w for w in users if w["id"] not in ids]


def on_alert(watch, new_slots):
    """Tras un aviso de citas: pregunta al dueño si ya tiene cita y avisa al administrador."""
    owner = watch["chat_ids"][0]
    if not watch["id"].startswith("u"):  # vigilancias fijas de MONITOR_CONFIG
        return
    send(owner, "¿Has conseguido cita? Si es así, dejo de vigilar a "
                f"{esc(watch['doctor_name'])}.",
         [[{"text": "✅ Sí, dejar de vigilar", "callback_data": f"done:{watch['id']}"},
           {"text": "🔁 No, sigue buscando", "callback_data": "keep"}]], silent=True)
    if not ADMIN or owner == ADMIN:
        return
    send(ADMIN, f"🔔 <b>{esc(user_name(owner))}</b> ha recibido aviso de "
                f"{len(new_slots)} hueco(s) con {esc(watch['doctor_name'])} "
                f"({monitor.PROVIDER_NAMES[watch['provider']]}). "
                f"El primero: {monitor.fmt_slot(new_slots[0])}.", silent=True)


monitor.ON_ALERT = on_alert


def check_now(watch):
    with state_lock:
        state = monitor.load_state([])
        try:
            monitor.run_watch(watch, state["watches"].setdefault(watch["id"], {}))
        finally:
            monitor.save_state(state)
    total = state["watches"][watch["id"]].get("last_total")
    if total == 0:
        send(watch["chat_ids"][0], "De momento no hay huecos. Te aviso en cuanto salga uno.")


def check_forever():
    while True:
        started = time.time()
        try:
            with state_lock:  # dentro, para no olvidar el estado de un alta reciente
                watches = all_watches()
                monitor.run_once(watches, heartbeat=True)
            log(f"Revisión hecha: {len(watches)} vigilancias en {time.time() - started:.0f} s")
        except BaseException:  # noqa: BLE001 - sys.exit de una config mala no debe pararlo
            log(f"Error en la revisión:\n{traceback.format_exc()}")
        time.sleep(max(30, CHECK_INTERVAL - (time.time() - started)))


def main():
    global store
    if not TOKEN or not ADMIN:
        sys.exit("Faltan TELEGRAM_BOT_TOKEN y/o ADMIN_CHAT_ID.")
    store = Store(os.path.join(DATA_DIR, "bot.db"))
    try:
        tg("deleteWebhook")
        commands = [
            {"command": "nueva", "description": "Vigilar un médico"},
            {"command": "mis", "description": "Ver o borrar mis vigilancias"},
            {"command": "cancelar", "description": "Salir del asistente"},
            {"command": "ayuda", "description": "Cómo funciona"}]
        tg("setMyCommands", commands=commands)
        # El administrador ve además /usuarios en su menú (solo en su chat).
        tg("setMyCommands", scope={"type": "chat", "chat_id": int(ADMIN)}, commands=commands + [
            {"command": "usuarios", "description": "Dar o quitar acceso a la gente"}])
    except Exception as e:  # noqa: BLE001
        log(f"Aviso al configurar el bot: {e}")
    log("Bot en marcha.")
    threading.Thread(target=check_forever, daemon=True).start()
    poll_forever()


if __name__ == "__main__":
    main()
