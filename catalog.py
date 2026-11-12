"""Búsquedas para el asistente del bot: médicos por nombre y seguros de cada médico.

Cada búsqueda devuelve una lista de opciones {"label": texto del botón, "cfg": claves
para el vigilante de monitor.py}. Solo usa la biblioteca estándar.
"""

import threading
import time
import unicodedata
import urllib.parse

from monitor import Quiron, Viamed, Vithas, http_json


def norm(text):
    """Minúsculas y sin tildes, para comparar nombres."""
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return " ".join(text.lower().split())


def matches(query, *texts):
    words = norm(query).split()
    haystack = norm(" ".join(t for t in texts if t))
    return all(w in haystack for w in words)


def nice(name):
    """'GARCIA PEREZ, JUAN' -> 'Garcia Perez, Juan' (solo si viene todo en mayúsculas)."""
    name = " ".join(str(name or "").split())
    return name.title() if name.isupper() else name


_cache, _cache_lock = {}, threading.Lock()


def cached(key, ttl, fetch):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    value = fetch()
    with _cache_lock:
        _cache[key] = (time.time(), value)
    return value


# ------------------------------------------------------------------------ Vithas

def _vithas_get(path, params):
    return http_json(f"{Vithas.API}{path}?{urllib.parse.urlencode(params)}", {
        "platform": "web", "accept": "application/json, text/plain, */*",
        "accept-language": "es", "referer": "https://areaprivada.vithas.es/",
    }, retries=2)


def vithas_doctors(query):
    groups = _vithas_get("/medical-staff/v1/doctors", {"doctorName": query})
    options, seen = [], set()
    for group in groups or []:
        for doc in group.get("doctors", []):
            name = " ".join(x for x in (doc.get("name"), doc.get("surname1"),
                                        doc.get("surname2")) if x)
            if not matches(query, name):
                continue
            for h in doc.get("hospitals", []):
                key = (h.get("idTuotempoDoctor"), h.get("hospitalTuotempoId"))
                if not all(key) or key in seen:
                    continue
                seen.add(key)
                options.append({
                    "label": f"{name} · {h.get('name', '')}",
                    "cfg": {"doctor_name": name, "hospital_name": h.get("name", ""),
                            "instance_id": h.get("instanceTuotempoId")
                            or Vithas.DEFAULTS["instance_id"],
                            "hospital_id": h["hospitalTuotempoId"],
                            "doctor_id": h["idTuotempoDoctor"]},
                })
    return options


def vithas_insurances(cfg):
    data = _vithas_get("/appointments/v1/filterdoctor/insurance", {
        "instanceTuotempoId": cfg["instance_id"], "hospitalTuotempoId": cfg["hospital_id"],
        "doctorId": cfg["doctor_id"]})
    return [{"label": i["insuranceName"],
             "cfg": {"insurance_id": i["insuranceId"], "insurance_name": i["insuranceName"],
                     "is_private": str(i.get("isPrivate") or "0")}}
            for i in data or [] if i.get("insuranceId")]


# -------------------------------------------------------------------- Quirónsalud

def _quiron_post(path, params):
    return Quiron({}).post(path, params)


def quiron_doctors(query):
    options = []
    for prof in _quiron_post("/catalog/professionals/search", {"searchQuery": query}) or []:
        name = nice(prof.get("nombre"))
        if not matches(query, name):
            continue
        for esp in prof.get("especialidades", []):
            for centro in esp.get("centros", []):
                for asoc in centro.get("centrosAsociados", []) or [{}]:
                    if not asoc.get("codCentroAsociado"):
                        continue
                    options.append({
                        "label": f"{name} · {esp.get('nombre', '')} · {centro.get('nombre', '')}",
                        "cfg": {"doctor_name": f"{name} ({esp.get('nombre', '')})",
                                "hospital_name": centro.get("nombre", ""),
                                "professional_id": str(prof["idProfesionalPdP"]),
                                "specialty_id": str(esp["idEspecialidadGlobal"]),
                                "province_id": str(centro["idProvincia"]),
                                "center_id": str(centro["idCentro"]),
                                "associated_center_id": str(asoc["codCentroAsociado"])},
                    })
    return options


def quiron_insurances(cfg):
    options = [{"label": "Sin seguro (pago privado)",
                "cfg": {"insurance_id": "", "insurance_name": "Privado"}}]
    for group in _quiron_post("/citas/aseguradoras", {"idCentro": cfg["center_id"]}) or []:
        for c in group.get("centrosAsistenciales", []):
            if not (c.get("portal", True) and c.get("citableCentro", True)):
                continue
            name = c.get("descripcionPortal") or c.get("descripcion") or ""
            parent = group.get("descripcion") or ""
            if parent and norm(parent) not in norm(name):
                name = f"{parent} · {name}"
            options.append({"label": name,
                            "cfg": {"insurance_id": str(c["idCentroAsistencial"]),
                                    "insurance_name": name}})
    return options


# ------------------------------------------------------------------------ Viamed

def _viamed_get(path):
    return Viamed({"db_name": Viamed.DEFAULTS["db_name"], "windows": 1}).get(path, {})


def viamed_doctors(query):
    resources = cached("viamed-resources", 6 * 3600, lambda: _viamed_get("resources"))
    options = []
    for r in resources:
        areas = r.get("areas") or [{}]
        hospital = areas[0].get("areaTitle") or ""
        if r.get("isHidden") == "1" or not matches(query, r.get("name")):
            continue
        options.append({
            "label": f"{nice(r['name'])} · {nice(hospital)}",
            "cfg": {"doctor_name": nice(r["name"]), "hospital_name": nice(hospital),
                    "resource_id": r["resourceid"],
                    "_areas": [a.get("areaid") for a in areas if a.get("areaid")]},
        })
    return options


def viamed_insurances(cfg):
    insurances = cached("viamed-insurances", 6 * 3600, lambda: _viamed_get("insurances"))
    areas = set(cfg.get("_areas") or [])
    options = []
    for i in insurances:
        tags = set(filter(None, str(i.get("tags") or "").split(",")))
        if i.get("IsHidden") == "1" or (areas and tags and not areas & tags):
            continue
        options.append({"label": i["insurance_title"],
                        "cfg": {"insurance_id": i["insuranceid"],
                                "insurance_name": i["insurance_title"]}})
    return options


# ------------------------------------------------------------------------ Común

DOCTORS = {"vithas": vithas_doctors, "quiron": quiron_doctors, "viamed": viamed_doctors}
INSURANCES = {"vithas": vithas_insurances, "quiron": quiron_insurances,
              "viamed": viamed_insurances}


def activities(provider, cfg):
    """Nombres de las consultas (primera visita, revisión...) que se pueden vigilar."""
    if provider == "quiron":
        return sorted({name for _, _, name in Quiron({**Quiron.DEFAULTS, **cfg}).get_services()})
    cls = Vithas if provider == "vithas" else Viamed
    return sorted({name for _, name in cls({**cls.DEFAULTS, **cfg}).get_activities()})
