#!/usr/bin/env python3
"""
Monitor sísmico Panamá — informe horario automático (en español).

Corre en GitHub Actions, solo con la biblioteca estándar de Python.
Fuentes: USGS (catálogo + pronóstico oficial de réplicas OAF), tsunami.gov (PTWC/NTWC),
Google News (titulares por tema: hospitales, agua, luz, aeropuerto, vías, metro, escuelas, Sinaproc).
Entrega: Telegram, e-mail (Gmail), WhatsApp personal (CallMeBot, opcional).

Variables de entorno (todas opcionales — lo que falte se omite):
  TELEGRAM_BOT_TOKEN [, TELEGRAM_CHAT_ID]       Telegram (sin CHAT_ID, descubre los chats que hablaron con el bot)
  GMAIL_USER, GMAIL_APP_PASSWORD, MAIL_TO       e-mail
  CALLMEBOT_RECIPIENTS                          "+507xxxxxxxx:APIKEY;+507yyyyyyyy:APIKEY"
  MAINSHOCK_ID                                  fuerza el id USGS del sismo principal
  DRY_RUN=1                                     solo imprime
  FIXTURES_DIR, FAKE_NOW                        pruebas sin red
  QUIET_AFTER_HOURS (72)                        modo "hora tranquila" tras N h sin M4.5+ en 6 h
"""
from __future__ import annotations

import json
import math
import os
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime

PANAMA_TZ = timezone(timedelta(hours=-5), name="America/Panama")
UA = "sismo-panama/2.0 (GitHub Actions; uso personal)"
USGS_QUERY = "https://earthquake.usgs.gov/fdsnws/event/1/query"
BBOX = {"minlatitude": 5.5, "maxlatitude": 10.5, "minlongitude": -84.5, "maxlongitude": -76.5}
TSUNAMI_FEEDS = {
    "PTWC": "https://www.tsunami.gov/events/xml/PHEBAtom.xml",
    "NTWC": "https://www.tsunami.gov/events/xml/PAAQAtom.xml",
}
WINDOWS_H = (1, 5, 10, 18, 24)
USGS_MAP = "https://earthquake.usgs.gov/earthquakes/map/?extent=5.5,-84.5&extent=10.5,-76.5&range=week&magnitude=all&listOnlyShown=true"
TSUNAMI_HOME = "https://www.tsunami.gov/"



# Teléfonos útiles (Panamá). ver = "oficial": tomado del sitio oficial de la entidad en la fecha indicada;
# "directorio": tomado de directorios/prensa recientes, no confirmado en sitio oficial. Nadie llamó para probarlos.
PHONES: list[tuple[str, str, str, str, str, str]] = [
    # (categoría, servicio, teléfono(s), nota, verificación, fuente)
    ("Emergencias", "Sistema Nacional de Emergencias (policía, bomberos, ambulancia, Sinaproc, Cruz Roja)", "911",
     "gratuito desde cualquier teléfono, 24 h", "oficial", "https://www.sinaproc.gob.pa/"),
    ("Emergencias", "Bomberos (línea histórica)", "103", "si no responde, marque 911", "directorio 2026",
     "https://detallesdepanama.com/%F0%9F%9A%A8-numeros-de-emergencia-principales-en-panama/"),
    ("Emergencias", "Policía Nacional (línea histórica)", "104", "si no responde, marque 911", "directorio 2026",
     "https://detallesdepanama.com/%F0%9F%9A%A8-numeros-de-emergencia-principales-en-panama/"),
    ("Emergencias", "Sinaproc — línea de emergencia 24 h", "520-4426 / 520-4427 / 520-4429", "WhatsApp 6998-4809; oficinas 504-4728 (8:00–16:00)",
     "oficial 09/10/2026", "https://www.sinaproc.gob.pa/"),
    ("Emergencias", "Cruz Roja Panameña", "315-1388", "también 315-1588 / 315-1589", "oficial 09/10/2026", "https://cruzroja.org.pa/"),
    ("Emergencias", "Centro de Atención Ciudadana (311)", "311", "reportes de agua (IDAAN) y servicios públicos", "directorio",
     "https://www.panamaamerica.com.pa/sociedad/sectores-de-panama-y-san-miguelito-llevan-4-dias-con-suministro-irregular-de-agua-situacion"),
    ("Salud — públicos", "Hospital Santo Tomás", "507-5600", "central; bloque 507-58xx confirmado en boletín oficial ene/2026", "directorio",
     "https://www.hospitalsantotomas.gob.pa/"),
    ("Salud — públicos", "Complejo Hospitalario Dr. Arnulfo Arias Madrid (CSS)", "503-6000", "también 503-2532", "directorio (2 fuentes)",
     "https://detallesdepanama.com/%F0%9F%9A%A8-numeros-de-emergencia-principales-en-panama/"),
    ("Salud — públicos", "Hospital del Niño", "512-9800", "", "directorio 2026",
     "https://detallesdepanama.com/%F0%9F%9A%A8-numeros-de-emergencia-principales-en-panama/"),
    ("Salud — públicos", "MINSA — línea de apoyo psicológico", "147", "", "directorio 2026",
     "https://detallesdepanama.com/%F0%9F%9A%A8-numeros-de-emergencia-principales-en-panama/"),
    ("Salud — red ASSA (privados)", "Pacífica Salud / Hospital Punta Pacífica", "204-8000", "", "oficial ASSA jun/2026",
     "https://www.assanet.com/servicio-al-cliente/red-de-hospitales-locales/"),
    ("Salud — red ASSA (privados)", "Centro Médico Paitilla", "265-8800", "", "oficial ASSA jun/2026",
     "https://www.assanet.com/servicio-al-cliente/red-de-hospitales-locales/"),
    ("Salud — red ASSA (privados)", "Hospital Nacional", "207-8100", "", "oficial ASSA jun/2026",
     "https://www.assanet.com/servicio-al-cliente/red-de-hospitales-locales/"),
    ("Salud — red ASSA (privados)", "Clínica Hospital San Fernando", "305-6300", "", "oficial ASSA jun/2026",
     "https://www.assanet.com/servicio-al-cliente/red-de-hospitales-locales/"),
    ("Salud — red ASSA (privados)", "The Panama Clinic", "310-1111", "", "oficial ASSA jun/2026",
     "https://www.assanet.com/servicio-al-cliente/red-de-hospitales-locales/"),
    ("Servicios", "Aeropuerto Internacional de Tocumen", "238-2700", "", "oficial 09/10/2026", "https://www.tocumenaero.com/"),
    ("Servicios", "IDAAN (agua) — reportes", "311", "Centro de Atención Ciudadana", "directorio", "https://www.idaan.gob.pa/"),
    ("Servicios", "ENSA (electricidad: Panamá Este, Colón, Darién)", "323-7100 / 800-9111", "800 gratuita", "oficial 09/10/2026",
     "https://www.ensa.com.pa/"),
    ("Servicios", "Naturgy (electricidad: Panamá Oeste, centro, Chiriquí)", "800-8346 / 315-7222", "averías por formulario web", "oficial 09/10/2026",
     "https://www.naturgy.com.pa/atencion-al-cliente"),
    ("Servicios", "Metro de Panamá", "504-7200", "", "oficial 09/10/2026", "https://www.elmetrodepanama.com/"),
    ("Consular", "Embajada de Brasil en Panamá", "263-5322", "Calle Elvira Méndez 24, Ed. El Dorado; horario de oficina", "directorio",
     "https://www.cidade-brasil.com.br/embaixada-do-brasil-no-panama-panama-city.html"),
]


def usgs_event_url(eid: str) -> str:
    return f"https://earthquake.usgs.gov/earthquakes/eventpage/{eid}"


def usgs_oaf_url(eid: str) -> str:
    return f"https://earthquake.usgs.gov/earthquakes/eventpage/{eid}/oaf/forecast"


def usgs_catalog_url(now: datetime, hours: int = 24, mmin: float = 3.5) -> str:
    q = {"format": "csv", "starttime": iso(now - timedelta(hours=hours)), "endtime": iso(now), "minmagnitude": mmin,
         "orderby": "time", **BBOX}
    return USGS_QUERY + "?" + urllib.parse.urlencode(q)

# Temas de servicios: etiqueta → (consulta Google News, horas hacia atrás). Son TITULARES de prensa, no estado oficial.
SERVICE_TOPICS: list[tuple[str, str, int, tuple[str, ...]]] = [
    # (etiqueta, consulta Google News, horas, palabras que el titular DEBE contener — filtra el ruido del buscador)
    ("Hospitales privados (Pacífica Salud/Punta Pacífica, Paitilla, Nacional, San Fernando — confirme cuáles acepta su seguro ASSA)",
     '("Punta Pacífica" OR "Pacífica Salud" OR Paitilla OR "Hospital Nacional" OR "San Fernando") (sismo OR terremoto)', 24,
     ("punta pacífica", "pacífica salud", "paitilla", "hospital nacional", "hospital san fernando", "clínica san fernando", "hospital", "clínica")),
    ("Hospitales públicos y CSS (Santo Tomás, Arnulfo Arias, Hospital del Niño, MINSA)",
     '("Santo Tomás" OR "Arnulfo Arias" OR "Hospital del Niño" OR "Seguro Social" OR MINSA OR hospitales) (sismo OR terremoto) Panamá', 24,
     ("santo tomás", "arnulfo arias", "hospital del niño", "seguro social", "css", "minsa", "hospital", "urgencias", "salud")),
    ("Agua (IDAAN)", "IDAAN", 24, ("idaan", "agua", "acueducto", "potabilizadora")),
    ("Electricidad (ENSA, Naturgy, ETESA)", '(ENSA OR Naturgy OR ETESA OR "sin luz" OR electricidad) Panamá (sismo OR terremoto)', 24,
     ("ensa", "naturgy", "etesa", "luz", "eléctric", "electricidad", "energía", "apag")),
    ("Aeropuerto de Tocumen", "Tocumen (aeropuerto OR vuelos)", 24, ("tocumen", "aeropuerto", "vuelo", "copa")),
    ("Vías y puentes (MOP, Panamericana, Puente de las Américas, Centenario)",
     '(MOP OR "Puente de las Américas" OR "Puente Centenario" OR Panamericana OR carretera OR puente) (sismo OR terremoto) Panamá', 24,
     ("puente", "mop", "panamericana", "carretera", "vía", "vías", "tránsito", "autopista", "corredor")),
    ("Metro y transporte", '("Metro de Panamá" OR MiBus) (sismo OR terremoto)', 24, ("metro", "mibus", "transporte", "buses")),
    ("Escuelas (MEDUCA)", "MEDUCA (clases OR escuelas)", 24, ("meduca", "clases", "escuela", "colegio", "universidad")),
    ("Sinaproc / autoridades", "Sinaproc", 12, ("sinaproc", "gobierno", "mulino", "alerta", "protección civil", "bomberos")),
    ("Daños y víctimas", "sismo Panamá heridos daños", 12,
     ("herido", "daño", "víctima", "fallecid", "muert", "colaps", "derrumb", "lesion", "evacua")),
]

FIXTURES_DIR = os.environ.get("FIXTURES_DIR")
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
QUIET_AFTER_HOURS = float(os.environ.get("QUIET_AFTER_HOURS", "72"))
SEND_EVERY_HOURS = max(1, int(os.environ.get("SEND_EVERY_HOURS", "3")))   # informe regular cada N horas (00, 03, 06…)
ALERT_MIN_MAG = float(os.environ.get("ALERT_MIN_MAG", "5.5"))            # réplica ≥ esto en la última hora → alerta inmediata

LOG: list[str] = []


def log(msg: str) -> None:
    print(msg, flush=True)
    LOG.append(msg)


# --------------------------------------------------------------------------- HTTP

def http_get(url: str, key: str, timeout: int = 30, retries: int = 2) -> bytes | None:
    """GET con reintentos. En modo FIXTURES lee tests/fixtures/<key>."""
    if FIXTURES_DIR:
        path = os.path.join(FIXTURES_DIR, key)
        if not os.path.exists(path):
            log(f"[fixture ausente] {key}")
            return None
        with open(path, "rb") as fh:
            return fh.read()
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            last_err = exc
            time.sleep(2 * (attempt + 1))
    log(f"[falla HTTP] {key}: {last_err}")
    return None


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def local(dt: datetime) -> datetime:
    return dt.astimezone(PANAMA_TZ)


def hhmm(dt: datetime) -> str:
    return local(dt).strftime("%H:%M")


def dhm(dt: datetime) -> str:
    return local(dt).strftime("%d/%m %H:%M")


def fmt_mag(m: float) -> str:
    return f"{m:.1f}"  # Panamá usa punto decimal (M7.7)


def pct(p: float | None) -> str:
    return "n/d" if p is None else f"{round(p * 100):d} %"


# --------------------------------------------------------------------------- USGS

@dataclass
class Quake:
    id: str
    time: datetime
    mag: float
    place: str
    lat: float
    lon: float
    depth: float

    @property
    def place_short(self) -> str:
        p = self.place or "ubicación no informada"
        for suffix, tag in ((", Panama", ""), (", Panamá", ""), (", Costa Rica", " (CR)"), (", Colombia", " (CO)")):
            if p.endswith(suffix):
                p = p[: -len(suffix)] + tag
        return p.replace(" of ", " de ")


def parse_geojson(raw: bytes | None) -> list[Quake]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log(f"[USGS] JSON inválido: {exc}")
        return []
    out: list[Quake] = []
    for f in data.get("features", []):
        p = f.get("properties", {}) or {}
        g = (f.get("geometry", {}) or {}).get("coordinates") or [None, None, None]
        if p.get("mag") is None or p.get("time") is None:
            continue
        out.append(Quake(
            id=str(f.get("id") or p.get("code") or ""),
            time=datetime.fromtimestamp(p["time"] / 1000, tz=timezone.utc),
            mag=float(p["mag"]),
            place=str(p.get("place") or ""),
            lat=float(g[1]) if g[1] is not None else float("nan"),
            lon=float(g[0]) if g[0] is not None else float("nan"),
            depth=float(g[2]) if g[2] is not None else float("nan"),
        ))
    out.sort(key=lambda q: q.time, reverse=True)
    return out


def usgs_query(key: str, **params) -> list[Quake]:
    q = {"format": "geojson", "orderby": "time", **BBOX, **params}
    return parse_geojson(http_get(USGS_QUERY + "?" + urllib.parse.urlencode(q), key))


def find_mainshock(now: datetime) -> Quake | None:
    forced = os.environ.get("MAINSHOCK_ID", "").strip()
    if forced:
        raw = http_get(USGS_QUERY + "?" + urllib.parse.urlencode({"format": "geojson", "eventid": forced}), "usgs_main.json")
        if raw:
            try:
                qs = parse_geojson(json.dumps({"features": [json.loads(raw)]}).encode())
                if qs:
                    return qs[0]
            except Exception as exc:  # noqa: BLE001
                log(f"[USGS] MAINSHOCK_ID inválido: {exc}")
    qs = usgs_query("usgs_main.json", starttime=iso(now - timedelta(days=30)), minmagnitude=6.5,
                    orderby="magnitude", limit=1)
    return qs[0] if qs else None


def fetch_oaf(mainshock_id: str) -> dict | None:
    """Pronóstico oficial de réplicas del USGS (Operational Aftershock Forecast)."""
    raw = http_get(USGS_QUERY + "?" + urllib.parse.urlencode({"format": "geojson", "eventid": mainshock_id}),
                   "event_detail.json")
    if not raw:
        return None
    try:
        oaf = (json.loads(raw)["properties"]["products"].get("oaf") or [])
        if not oaf:
            log("[OAF] el USGS aún no publicó pronóstico oficial para este evento")
            return None
        fc_url = oaf[0].get("contents", {}).get("forecast.json", {}).get("url")
        if not fc_url:
            log("[OAF] forecast.json ausente")
            return None
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        log(f"[OAF] detalle del evento inesperado: {exc}")
        return None
    raw_fc = http_get(fc_url, "forecast.json")
    if not raw_fc:
        return None
    try:
        return json.loads(raw_fc)
    except json.JSONDecodeError as exc:
        log(f"[OAF] forecast.json inválido: {exc}")
        return None


@dataclass
class Forecast:
    official: bool
    label: str
    # probabilidad de ≥1 evento de M≥x en la ventana; n = número esperado (centro del intervalo 95 %)
    p: dict[tuple[str, int], float | None] = field(default_factory=dict)
    n: dict[tuple[str, int], float | None] = field(default_factory=dict)


def oaf_forecast(fc: dict | None) -> Forecast | None:
    if not fc:
        return None
    by_label = {}
    for w in fc.get("forecast") or []:
        by_label[(w.get("label") or "").lower()] = w
    alias = {"1d": ("1 day",), "7d": ("1 week", "7 days"), "30d": ("1 month", "30 days")}
    out = Forecast(True, "pronóstico oficial USGS")
    issued = fc.get("creationTime") or fc.get("issueTime")
    if issued:
        try:
            out.label += " emitido " + dhm(datetime.fromtimestamp(float(issued) / 1000, tz=timezone.utc))
        except (TypeError, ValueError):
            pass
    found = False
    for win, labels in alias.items():
        w = next((by_label[l] for l in labels if l in by_label), None)
        if not w:
            continue
        for b in w.get("bins", []):
            try:
                mag = int(round(float(b.get("magnitude"))))
            except (TypeError, ValueError):
                continue
            if mag in (4, 5, 6, 7):
                pr = b.get("probability")
                lo, hi = b.get("p95minimum"), b.get("p95maximum")
                out.p[(win, mag)] = None if pr is None else float(pr)
                out.n[(win, mag)] = None if lo is None or hi is None else (float(lo) + float(hi)) / 2
                found = True
    return out if found else None


def generic_rj(mainshock_mag: float, t1_days: float, t2_days: float, mag: float) -> float:
    """Número esperado de réplicas M≥mag entre t1 y t2 (días tras el principal), modelo genérico
    Reasenberg-Jones (a=-1.67, b=0.91, p=1.08, c=0.05)."""
    a, b, p, c = -1.67, 0.91, 1.08, 0.05
    k = 10 ** (a + b * (mainshock_mag - mag))
    return k * (((t2_days + c) ** (1 - p) - (t1_days + c) ** (1 - p)) / (1 - p))


def generic_forecast(mainshock_mag: float, days_since: float) -> Forecast:
    t = max(days_since, 0.01)
    out = Forecast(False, "MODELO GENÉRICO (sin pronóstico oficial del USGS todavía)")
    for win, days in (("1d", 1), ("7d", 7), ("30d", 30)):
        for mag in (4, 5, 6, 7):
            n = generic_rj(mainshock_mag, t, t + days, float(mag))
            out.n[(win, mag)] = n
            out.p[(win, mag)] = 1 - math.exp(-n)
    return out


# --------------------------------------------------------------------------- Tsunami

def fetch_tsunami(now: datetime) -> dict:
    result = {"active": False, "lines": [], "ok": False}
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for name, url in TSUNAMI_FEEDS.items():
        raw = http_get(url, f"tsunami_{name.lower()}.xml")
        if not raw:
            continue
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            log(f"[tsunami] {name}: XML inválido: {exc}")
            continue
        result["ok"] = True
        relevant: list[tuple[datetime, str, bool, str]] = []
        for entry in root.findall("a:entry", ns):
            title = (entry.findtext("a:title", default="", namespaces=ns) or "").strip()
            updated = entry.findtext("a:updated", default="", namespaces=ns) or ""
            summary = (entry.findtext("a:summary", default="", namespaces=ns)
                       or entry.findtext("a:content", default="", namespaces=ns) or "")
            try:
                when = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            except ValueError:
                continue
            if now - when > timedelta(hours=24):
                continue
            text = f"{title} {summary}".lower()
            if not ("panama" in text or "panamá" in text or "pacific" in text or "central america" in text):
                continue
            is_threat = any(k in text for k in ("threat", "warning", "advisory", "watch", "hazardous"))
            is_over = any(k in text for k in ("has now passed", "has passed", "no longer", "cancel", "no tsunami",
                                               "no threat", "ended", "final message", "is over", "not expected"))
            snippet = " ".join(summary.split())[:110]
            link_el = entry.find("a:link", ns)
            href = (link_el.get("href") if link_el is not None else "") or TSUNAMI_HOME
            relevant.append((when, f"{name} {dhm(when)}: {title[:60]}" + (f" — {snippet}" if snippet else ""),
                             is_threat and not is_over, href))
        relevant.sort(key=lambda r: r[0], reverse=True)
        result["lines"].extend((r[1], r[3]) for r in relevant)
        if relevant and relevant[0][2] and now - relevant[0][0] <= timedelta(hours=6):
            result["active"] = True
    return result


# --------------------------------------------------------------------------- Prensa por tema

def fetch_news_topic(now: datetime, key: str, query: str, hours: int, limit: int = 2,
                     must: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": f"{query} when:{hours}h", "hl": "es-419", "gl": "PA", "ceid": "PA:es-419"})
    raw = http_get(url, f"news_{key}.xml")
    if not raw:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        log(f"[prensa] {key}: XML inválido: {exc}")
        return []
    items: list[tuple[datetime, str, str]] = []
    seen: set[str] = set()
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        source = (item.findtext("source") or "").strip()
        link = (item.findtext("link") or "").strip()
        if source.lower() in ("facebook", "youtube", "x", "instagram", "tiktok", "threads"):
            continue
        try:
            when = parsedate_to_datetime(item.findtext("pubDate") or "")
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if now - when > timedelta(hours=hours):
            continue
        if must and not any(k in title.lower() for k in must):
            continue
        norm = title.lower()[:60]
        if norm in seen:
            continue
        seen.add(norm)
        if source and title.endswith(" - " + source):
            title = title[: -(len(source) + 3)]
        items.append((when, f"{dhm(when)} {source}: {title}"[:170], link))
    items.sort(key=lambda x: x[0], reverse=True)
    return [(t, u) for _, t, u in items[:limit]]


def fetch_services(now: datetime) -> list[tuple[str, list[tuple[str, str]]]]:
    out = []
    for i, (label, query, hours, must) in enumerate(SERVICE_TOPICS):
        out.append((label, fetch_news_topic(now, f"svc{i}", query, hours, must=must)))
    return out


# --------------------------------------------------------------------------- Análisis

@dataclass
class Analysis:
    now: datetime
    main: Quake
    hours_since: float
    day: list[Quake]                     # últimas 24 h, M≥3.0 (sin el principal)
    cum: list[Quake]                     # desde el principal, M≥4.5 (sin el principal)
    win45: dict[int, int]                # ventana h → nº M≥4.5
    win35: dict[int, int]
    rate_last1: float
    rate_h2to5: float
    rate_h6to10: float
    biggest_last: Quake | None
    cum45: int
    cum5: int
    cum6: int
    biggest_after: Quake | None
    m6_recent: bool
    trend: str
    trend_note: str
    fc: Forecast
    tsunami: dict
    services: list[tuple[str, list[tuple[str, str]]]]
    quiet: bool
    alert: bool = False
    alert_reason: str = ""


def analyse(now: datetime, main: Quake, day: list[Quake], cum: list[Quake], fc: Forecast,
            tsunami: dict, services: list[tuple[str, list[tuple[str, str]]]]) -> Analysis:
    day = [q for q in day if q.id != main.id and q.time > main.time]
    after = [q for q in cum if q.id != main.id and q.time > main.time]
    hours_since = (now - main.time).total_seconds() / 3600

    def in_win(h_from: float, h_to: float, mmin: float) -> int:
        return sum(1 for q in day if q.mag >= mmin and h_to <= (now - q.time).total_seconds() / 3600 < h_from)

    win45 = {h: in_win(h, 0, 4.5) for h in WINDOWS_H}
    win35 = {h: in_win(h, 0, 3.5) for h in WINDOWS_H}
    last = [q for q in day if timedelta(0) <= now - q.time <= timedelta(hours=1)]
    biggest_last = max(last, key=lambda q: q.mag) if last else None
    rate_last1 = float(win45[1])
    rate_h2to5 = in_win(5, 1, 4.5) / 4.0
    rate_h6to10 = in_win(10, 5, 4.5) / 5.0
    cum45 = sum(1 for q in after if q.mag >= 4.5)
    cum5 = sum(1 for q in after if q.mag >= 5.0)
    cum6 = sum(1 for q in after if q.mag >= 6.0)
    biggest_after = max(after, key=lambda q: q.mag) if after else None
    m6_recent = any(q.mag >= 6.0 and timedelta(0) <= now - q.time <= timedelta(hours=3) for q in after + day)

    # Tendencia: tasa de M≥4.5 por hora, última hora vs promedio de las horas 2–5 (y 6–10 como contexto).
    if m6_recent:
        trend, note = "REACTIVADA", "hubo una réplica M6+ en las últimas 3 h"
    elif rate_last1 > max(rate_h2to5, 0.5) * 1.5 and rate_last1 >= 2:
        trend, note = "REACTIVADA", f"{win45[1]} M4.5+ en la última hora vs {rate_h2to5:.1f}/h en las 4 anteriores"
    elif rate_last1 <= rate_h2to5 * 0.75 or (rate_last1 == 0 and rate_h2to5 > 0):
        trend, note = "EN DESCENSO", f"{win45[1]} M4.5+ en la última hora vs {rate_h2to5:.1f}/h en las 4 anteriores"
    elif rate_last1 == 0 and rate_h2to5 == 0:
        trend, note = "TRANQUILA", "sin M4.5+ en las últimas 5 h"
    else:
        trend, note = "ESTABLE", f"{win45[1]} M4.5+ en la última hora vs {rate_h2to5:.1f}/h en las 4 anteriores"
    if rate_h6to10 > 0 and trend != "REACTIVADA":
        note += f"; horas 6–10: {rate_h6to10:.1f}/h"
    recent6h_45 = win45[5] > 0 or in_win(6, 5, 4.5) > 0
    quiet = hours_since > QUIET_AFTER_HOURS and not recent6h_45 and not tsunami.get("active")
    reasons = []
    # lo que el vigía (watch.py) ya alertó no se vuelve a alertar aquí; sí entra en el informe regular
    try:
        with open(os.path.join(os.environ.get("REPORTS_DIR", "reports"), "alerts_sent.json"), encoding="utf-8") as fh:
            already = json.load(fh)
    except (OSError, json.JSONDecodeError):
        already = {"events": {}, "tsunami": []}
    if tsunami.get("active") and not any(u in already.get("tsunami", []) for _, u in tsunami.get("lines", [])[:1]):
        reasons.append("boletín de tsunami activo")
    if biggest_last and biggest_last.mag >= ALERT_MIN_MAG and biggest_last.id not in already.get("events", {}):
        reasons.append(f"réplica M{fmt_mag(biggest_last.mag)} a las {hhmm(biggest_last.time)}")
    if m6_recent and not (biggest_last and biggest_last.mag >= 6.0):
        reasons.append("M6+ en las últimas 3 h")
    if trend == "REACTIVADA" and not reasons:
        reasons.append("secuencia reactivada")
    return Analysis(now, main, hours_since, day, after, win45, win35, rate_last1, rate_h2to5, rate_h6to10,
                    biggest_last, cum45, cum5, cum6, biggest_after, m6_recent, trend, note, fc, tsunami, services, quiet,
                    bool(reasons), "; ".join(reasons))


def actions_for(a: Analysis, short: bool = False) -> list[str]:
    hour = local(a.now).hour
    night = hour >= 21 or hour < 6
    acts: list[tuple[str, str]] = []
    if a.tsunami.get("active"):
        acts.append(("[Seguro] Boletín de tsunami ACTIVO en el Pacífico: quien esté en la costa (Cinta Costera, Costa del Este, "
                     "Punta Pacífica, Amador) se aleja del mar ahora y sigue al Sinaproc.",
                     "Alerta de tsunami activa: alejarse de la costa."))
    if a.m6_recent:
        acts.append(("[Probable] Hubo M6+ en las últimas 3 h: antes de dormir, revisar columnas, vigas y paredes buscando grietas "
                     "nuevas (fotografiar para comparar). Grieta diagonal o en X en una columna = no dormir en el apartamento.",
                     "Réplica fuerte (M6+) hace menos de 3 h: revisar paredes y columnas antes de dormir."))
    elif a.trend == "REACTIVADA":
        acts.append(("[Probable] Secuencia reactivada en esta hora: posponer salidas no esenciales y mantener la ruta de salida libre.",
                     "Réplicas en aumento: posponer salidas no esenciales, ruta de salida libre."))
    if night:
        acts.append(("[Seguro] Para la noche: zapatos cerrados, linterna, llaves y celular cargado junto a la cama; puerta del cuarto "
                     "sin seguro; si tiembla fuerte, proteger la cabeza junto a la cama y esperar a que pase antes de bajar.",
                     "Noche: zapatos, linterna y celular junto a la cama; si tiembla fuerte, proteger la cabeza y esperar."))
    else:
        acts.append(("[Seguro] Nada de ascensor — escaleras, hasta que el edificio sea inspeccionado.",
                     "Nada de ascensor, escaleras."))
    if a.hours_since < 48:
        acts.append(("[Probable] Evitar viajes largos por la Panamericana hacia Coclé/Azuero en las próximas 24–48 h: "
                     "puentes y viaductos siguen en inspección.",
                     "Evitar viajes largos por la Panamericana (Coclé/Azuero) por 24–48 h."))
    p6 = a.fc.p.get(("7d", 6))
    if p6 is not None:
        tag = "pronóstico USGS" if a.fc.official else "modelo genérico"
        acts.append((f"[Estimación] Réplica M6+ en los próximos 7 días: {pct(p6)} ({tag}) — se sentiría en edificios altos "
                     "de la capital; es lo esperado, no señal de un nuevo terremoto.",
                     f"Réplica M6+ esta semana: {pct(p6)} ({tag}) — esperado, no es un nuevo terremoto."))
    if a.quiet or (a.win45[1] == 0 and a.hours_since > 24):
        acts.append(("[Probable] Hora tranquila: rutina normal con las precauciones básicas; nada nuevo que hacer.",
                     "Hora tranquila: rutina normal con precauciones básicas."))
    return [s if short else f for f, s in acts[:6]]


# --------------------------------------------------------------------------- Textos

def windows_line(a: Analysis) -> str:
    return " | ".join(f"{h} h: {a.win45[h]} de M4.5+ y {a.win35[h] - a.win45[h]} de M3.5–4.4" for h in WINDOWS_H)


def forecast_lines(a: Analysis) -> list[str]:
    f = a.fc
    L = [f"PRONÓSTICO [{f.label}] — fuente: " + (usgs_oaf_url(a.main.id) if f.official else "modelo Reasenberg-Jones (1989) con parámetros genéricos; no es un dato oficial") + ":"]
    if not f.official:
        L.append("  AVISO: el USGS todavía no publicó el pronóstico oficial de este evento; los números abajo salen de un "
                 "modelo genérico mundial y suelen ser más altos que la realidad. Úselos solo como orden de magnitud; "
                 "la TENDENCIA observada (arriba) pesa más que estos porcentajes.")
    for win, name in (("1d", "próximas 24 h"), ("7d", "próximos 7 días"), ("30d", "próximos 30 días")):
        parts = []
        for mag in (5, 6, 7):
            p = f.p.get((win, mag))
            if p is None:
                continue
            n = f.n.get((win, mag))
            parts.append(f"M{mag}+: {pct(p)}" + (f" (~{n:.0f} esperadas)" if n is not None and n >= 1 else ""))
        if parts:
            L.append(f"  {name}: " + " · ".join(parts))
    L.append("  Lectura: la cantidad de réplicas cae aproximadamente con 1/tiempo desde el principal; la mayor réplica "
             f"típica para un M{fmt_mag(a.main.mag)} ronda M{fmt_mag(a.main.mag - 1.2)}, y queda una probabilidad pequeña "
             "pero no nula de un evento igual o mayor.")
    return L


def family_message(a: Analysis) -> str:
    t = hhmm(a.now)
    if a.quiet:
        return (f"🔔 Sismo PA {t} (automático)\n"
                f"Hora tranquila: sin réplicas relevantes en las últimas 6 h. Tendencia: {a.trend.lower()}.\n"
                f"Tsunami: sin alerta. Próxima actualización en 1 h.\n"
                f"Fuente: USGS {usgs_event_url(a.main.id)}")
    if a.biggest_last:
        b = a.biggest_last
        last_line = (f"Última hora: {a.win45[1]} réplicas de M4.5 o más. "
                     f"Mayor: M{fmt_mag(b.mag)} a las {hhmm(b.time)}, {b.place_short}.")
    else:
        last_line = "Última hora: ningún temblor M3.5+ registrado."
    wins = " · ".join(f"{h} h → {a.win45[h]}" for h in WINDOWS_H)
    big = (f" Mayor réplica hasta ahora: M{fmt_mag(a.biggest_after.mag)} a las {hhmm(a.biggest_after.time)}."
           if a.biggest_after else "")
    tsu = "ALERTA ACTIVA en el Pacífico — alejarse de la costa" if a.tsunami.get("active") else "sin alerta"
    p6 = a.fc.p.get(("7d", 6))
    tag = "USGS" if a.fc.official else "modelo genérico, sin pronóstico oficial aún"
    top = actions_for(a, short=True)[:2]
    head = f"⚠️ ALERTA sismo PA {t} — {a.alert_reason}" if a.alert else f"🔔 Sismo PA {t} (automático)"
    lines = [
        head,
        last_line,
        f"Réplicas M4.5+ en las últimas: {wins}. Tendencia: {a.trend.lower()} ({a.trend_note}).{big}",
        f"Desde el principal (M{fmt_mag(a.main.mag)}, {hhmm(a.main.time)}): {a.cum5} réplicas M5+, {a.cum6} M6+.",
        f"Tsunami: {tsu}. P(M6+ en 7 días): {pct(p6)} ({tag}).",
        "Ahora: " + " ".join(top),
        f"Próximo informe regular en ~{SEND_EVERY_HOURS} h; vigía cada 10 min con alerta inmediata si hay réplica M{ALERT_MIN_MAG:.1f}+, sismo cercano o tsunami.",
        "Emergencias: 911 · Sinaproc 520-4426/27/29 (WhatsApp 6998-4809) · lista completa en el PDF.",
        f"Fuentes: USGS {usgs_event_url(a.main.id)} · tsunami.gov · prensa (enlaces en el PDF).",
    ]
    msg = "\n".join(lines)
    return msg if len(msg) <= 1400 else msg[:1380] + "…"


def email_report(a: Analysis) -> tuple[str, str]:
    t = local(a.now)
    tsu_s = "ALERTA ACTIVA" if a.tsunami.get("active") else "sin alerta"
    head = (f"Sismo PA {t:%H:%M}: {a.win45[1]} réplicas M4.5+ en la última hora"
            + (f", mayor M{fmt_mag(a.biggest_last.mag)} a las {hhmm(a.biggest_last.time)}" if a.biggest_last else "")
            + f" — tendencia {a.trend.lower()}, tsunami {tsu_s}.")
    subject = f"[Sismo PA] {t:%d/%m %H:%M} — {a.win45[1]} M4.5+ en 1 h, tendencia {a.trend.lower()}"
    L: list[str] = [head, "", f"SISMO PANAMÁ — informe de las {t:%H:%M} ({t:%d/%m/%Y}), generado por GitHub Actions"]
    L.append(f"Sismo principal: M{fmt_mag(a.main.mag)} el {dhm(a.main.time)}, {a.main.place_short}, "
             f"prof. {a.main.depth:.0f} km — hace {a.hours_since:.1f} h.")
    L.append("")
    L.append("RÉPLICAS POR VENTANA (fuente: catálogo USGS ComCat, caja Panamá — mapa: " + USGS_MAP + "):")
    L.append("  " + windows_line(a))
    if a.biggest_last:
        b = a.biggest_last
        L.append(f"  Mayor de la última hora: M{fmt_mag(b.mag)} a las {hhmm(b.time)}, {b.place_short}, prof. {b.depth:.0f} km.")
    L.append(f"  Acumulado desde el principal: {a.cum45} M4.5+ | {a.cum5} M5+ | {a.cum6} M6+."
             + (f" Mayor réplica: M{fmt_mag(a.biggest_after.mag)} a las {dhm(a.biggest_after.time)}, "
                f"{a.biggest_after.place_short}." if a.biggest_after else ""))
    L.append(f"TENDENCIA: {a.trend} — {a.trend_note}.")
    L.append("")
    L.extend(forecast_lines(a))
    L.append("")
    L.append("SITUACIÓN OFICIAL Y SERVICIOS (titulares de prensa de las últimas 12–24 h; la ausencia de noticias NO confirma "
             "que un servicio esté normal):")
    if a.tsunami.get("ok"):
        L.append("  Tsunami (tsunami.gov): " + ("ALERTA ACTIVA" if a.tsunami["active"] else "sin alerta activa"))
        for ln, url in a.tsunami["lines"][:2]:
            L.append("    - " + ln)
            L.append("      " + url)
    else:
        L.append("  Tsunami: feed de tsunami.gov no disponible en esta ronda.")
    for label, lines in a.services:
        if lines:
            L.append(f"  {label}:")
            for ln, url in lines:
                L.append("    - " + ln)
                L.append("      " + url)
        else:
            L.append(f"  {label}: sin titulares específicos en las últimas horas.")
    L.append("")
    L.append("ACCIONES AHORA:")
    for i, act in enumerate(actions_for(a), 1):
        L.append(f"  {i}. {act}")
    L.append("")
    L.append("ÚLTIMAS 24 H — eventos M4.5+ (hora local, M, lugar, prof.):")
    big24 = [q for q in a.day if q.mag >= 4.5]
    if big24:
        for q in big24[:25]:
            L.append(f"  {dhm(q.time)}  M{fmt_mag(q.mag)}  {q.place_short}  {q.depth:.0f} km")
        if len(big24) > 25:
            L.append(f"  … y {len(big24) - 25} más")
    else:
        L.append("  (ninguno)")
    L.append("")
    L.append("MENSAJE CORTO (Telegram/WhatsApp):")
    L.append(family_message(a))
    L.append("")
    L.append("TELÉFONOS ÚTILES (Panamá; 'oficial' = tomado del sitio de la entidad, 'directorio' = no confirmado en sitio oficial):")
    cat = ""
    for c, name, tel, note, ver, url in PHONES:
        if c != cat:
            L.append(f"  {c}:")
            cat = c
        L.append(f"    {name}: {tel}" + (f" — {note}" if note else "") + f" [{ver}]")
    L.append("")
    L.append("FUENTES:")
    L.append("  Sismo principal (USGS): " + usgs_event_url(a.main.id))
    L.append("  Pronóstico oficial de réplicas (USGS OAF): " + usgs_oaf_url(a.main.id))
    L.append("  Mapa de réplicas (USGS): " + USGS_MAP)
    L.append("  Datos crudos 24 h, M3.5+ (USGS ComCat, CSV): " + usgs_catalog_url(a.now))
    L.append("  Boletines de tsunami (PTWC/NTWC): " + TSUNAMI_HOME)
    L.append("  Titulares: Google News (es-419, PA) — cada titular lleva su enlace arriba")
    if a.quiet:
        L.append("")
        L.append("Sugerencia: ya pasaron más de 72 h sin réplicas relevantes — reducir la cadencia a cada 6 h "
                 "(editar el cron en .github/workflows/sismo.yml).")
    warnings = [x for x in LOG if x.startswith("[")]
    if warnings:
        L.append("")
        L.append("Avisos de esta ronda: " + " | ".join(warnings[-6:]))
    return subject, "\n".join(L)


# --------------------------------------------------------------------------- Envío

def send_email(subject: str, body: str) -> str:
    user = os.environ.get("GMAIL_USER")
    pwd = os.environ.get("GMAIL_APP_PASSWORD")
    to = [x.strip() for x in (os.environ.get("MAIL_TO") or user or "").split(",") if x.strip()]
    if not (user and pwd and to):
        return "e-mail: omitido (faltan GMAIL_USER/GMAIL_APP_PASSWORD/MAIL_TO)"
    msg = EmailMessage()
    msg["From"] = f"Monitor sísmico <{user}>"
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)
    if DRY_RUN:
        return f"e-mail: DRY_RUN para {to}"
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=30) as s:
        s.login(user, pwd.replace(" ", ""))
        s.send_message(msg)
    return f"e-mail: enviado a {', '.join(to)}"


def send_callmebot(text: str) -> str:
    spec = os.environ.get("CALLMEBOT_RECIPIENTS", "").strip()
    if not spec:
        return "whatsapp: omitido (sin CALLMEBOT_RECIPIENTS)"
    results = []
    for item in [x.strip() for x in spec.replace(",", ";").split(";") if x.strip()]:
        if ":" not in item:
            results.append(f"{item[:6]}…: formato inválido (use +507xxxx:APIKEY)")
            continue
        phone, key = item.split(":", 1)
        url = "https://api.callmebot.com/whatsapp.php?" + urllib.parse.urlencode(
            {"phone": phone.strip(), "text": text, "apikey": key.strip()})
        masked = phone.strip()[:5] + "…" + phone.strip()[-2:]
        if DRY_RUN:
            results.append(f"{masked}: DRY_RUN")
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=40) as resp:
                body = resp.read(300).decode("utf-8", "ignore").replace("\n", " ")
                results.append(f"{masked}: HTTP {resp.status} {body[:80]}")
        except Exception as exc:  # noqa: BLE001
            results.append(f"{masked}: FALLÓ {exc}")
    return "whatsapp: " + " | ".join(results)


def strip_emoji(text: str) -> str:
    return "".join(ch for ch in text if ord(ch) < 0x1F000 and ch not in "\u2b50\u26a0\ufe0f")


BLUE = (42, 120, 214)       # serie 1 (M≥4.5)
BLUE_LIGHT = (169, 200, 239)  # paso claro de la misma rampa (M3.5–4.4)
ORANGE = (235, 104, 52)     # acento: sismo principal
INK = (11, 11, 11)
INK2 = (82, 81, 78)
GRID = (222, 222, 218)


def summary_bullets(a: Analysis) -> list[str]:
    b: list[str] = []
    if a.biggest_last:
        b.append(f"Última hora: {a.win45[1]} réplicas de M4.5 o más; la mayor M{fmt_mag(a.biggest_last.mag)} "
                 f"a las {hhmm(a.biggest_last.time)} en {a.biggest_last.place_short}.")
    else:
        b.append("Última hora: sin réplicas M3.5+ en el catálogo USGS.")
    b.append(f"Tendencia {a.trend.lower()}: {a.trend_note}. En 24 h: {a.win45[24]} M4.5+; desde el principal: "
             f"{a.cum5} M5+ y {a.cum6} M6+" + (f" (mayor M{fmt_mag(a.biggest_after.mag)})." if a.biggest_after else "."))
    p6 = a.fc.p.get(("7d", 6)); p5 = a.fc.p.get(("1d", 5))
    b.append(f"Pronóstico ({'USGS oficial' if a.fc.official else 'modelo genérico, sin pronóstico oficial aún'}): "
             f"M5+ en 24 h {pct(p5)}; M6+ en 7 días {pct(p6)}.")
    b.append("Tsunami: " + ("ALERTA ACTIVA en el Pacífico." if a.tsunami.get("active") else "sin alerta activa."))
    svc = [(label.split(" (")[0], lines[0][0]) for label, lines in a.services if lines]
    for label, line in svc[:3]:
        txt = line.split(": ", 1)[1] if ": " in line else line
        b.append(f"{label}: {txt[:120]}")
    acts = actions_for(a, short=True)
    if acts:
        b.append("Acción principal: " + acts[0])
    b.append("Fuentes: USGS (catálogo y pronóstico), tsunami.gov, prensa vía Google News — enlaces en cada sección y en la 7.")
    return b[:8]


def _axes_box(pdf, x: float, y: float, w: float, h: float, title: str) -> None:
    pdf.set_font("DV", "B", 9)
    pdf.set_text_color(*INK)
    pdf.set_xy(x, y)
    pdf.cell(w, 5, title, new_x="LMARGIN", new_y="NEXT")


def draw_hourly_bars(pdf, a: Analysis) -> None:
    """Barras apiladas por hora (últimas 24 h): M≥4.5 (azul) y M3.5–4.4 (azul claro)."""
    x0, w = 14, pdf.w - 28
    y0 = pdf.get_y()
    if y0 + 62 > pdf.h - 16:
        pdf.add_page(); y0 = pdf.get_y()
    _axes_box(pdf, x0, y0, w, 0, "Réplicas por hora — últimas 24 h")
    top, h = y0 + 7, 40
    left, right = x0 + 8, x0 + w
    plot_w = right - left
    buckets_45 = [0] * 24
    buckets_35 = [0] * 24
    for q in a.day:
        age = (a.now - q.time).total_seconds() / 3600
        if 0 <= age < 24:
            i = 23 - int(age)
            if q.mag >= 4.5:
                buckets_45[i] += 1
            elif q.mag >= 3.5:
                buckets_35[i] += 1
    totals = [buckets_45[i] + buckets_35[i] for i in range(24)]
    ymax = max(max(totals), 1)
    step = 1 if ymax <= 5 else 2 if ymax <= 10 else 5
    ymax = ((ymax + step - 1) // step) * step
    # rejilla
    pdf.set_draw_color(*GRID); pdf.set_line_width(0.2)
    pdf.set_font("DV", "", 7); pdf.set_text_color(*INK2)
    for v in range(0, ymax + 1, step):
        yy = top + h - h * v / ymax
        pdf.line(left, yy, right, yy)
        pdf.set_xy(x0, yy - 2); pdf.cell(7, 4, str(v), align="R")
    bw = plot_w / 24
    gap = 0.5
    for i in range(24):
        bx = left + i * bw + gap / 2
        h45 = h * buckets_45[i] / ymax
        h35 = h * buckets_35[i] / ymax
        if h45 > 0:
            pdf.set_fill_color(*BLUE); pdf.rect(bx, top + h - h45, bw - gap, h45, style="F")
        if h35 > 0:
            pdf.set_fill_color(*BLUE_LIGHT); pdf.rect(bx, top + h - h45 - h35 - (0.4 if h45 > 0 else 0), bw - gap, h35, style="F")
    # etiquetas x cada 4 h + etiqueta de la barra máxima y de la última hora
    pdf.set_text_color(*INK2)
    for i in range(0, 24, 4):
        t = local(a.now - timedelta(hours=23 - i))
        pdf.set_xy(left + i * bw - 4, top + h + 1); pdf.cell(bw + 8, 4, f"{t:%H}h", align="C")
    imax = max(range(24), key=lambda k: totals[k])
    pdf.set_text_color(*INK)
    for i in {imax, 23}:
        if totals[i] > 0:
            yy = top + h - h * totals[i] / ymax - 4
            pdf.set_xy(left + i * bw - 3, yy); pdf.cell(bw + 6, 4, str(totals[i]), align="C")
    # leyenda
    ly = top + h + 6
    pdf.set_fill_color(*BLUE); pdf.rect(left, ly + 0.8, 3, 3, style="F")
    pdf.set_xy(left + 4, ly); pdf.set_text_color(*INK2); pdf.cell(28, 5, "M4.5 o más")
    pdf.set_fill_color(*BLUE_LIGHT); pdf.rect(left + 34, ly + 0.8, 3, 3, style="F")
    pdf.set_xy(left + 38, ly); pdf.cell(40, 5, "M3.5 a 4.4")
    pdf.set_xy(left + 80, ly); pdf.cell(plot_w - 80, 5, f"eje x: hora local (Panamá); barra derecha = última hora", align="R")
    pdf.set_text_color(*INK)
    pdf.set_y(ly + 8)


def draw_magnitude_dots(pdf, a: Analysis) -> None:
    """Magnitud de cada réplica M≥4.5 vs horas desde el principal (muestra el decaimiento)."""
    events = [q for q in a.cum if q.mag >= 4.5]
    x0, w = 14, pdf.w - 28
    y0 = pdf.get_y()
    if y0 + 62 > pdf.h - 16:
        pdf.add_page(); y0 = pdf.get_y()
    _axes_box(pdf, x0, y0, w, 0, "Magnitud de cada réplica (M4.5+) vs horas desde el sismo principal")
    top, h = y0 + 7, 40
    left, right = x0 + 8, x0 + w
    plot_w = right - left
    xmax = max(a.hours_since, 6.0)
    xmax = math.ceil(xmax / 6) * 6
    ymin, ymax = 4.0, max(7.5, a.main.mag + 0.4)
    pdf.set_draw_color(*GRID); pdf.set_line_width(0.2)
    pdf.set_font("DV", "", 7); pdf.set_text_color(*INK2)
    for v in range(int(ymin), int(ymax) + 1):
        yy = top + h - h * (v - ymin) / (ymax - ymin)
        pdf.line(left, yy, right, yy)
        pdf.set_xy(x0, yy - 2); pdf.cell(7, 4, f"M{v}", align="R")
    xstep = 6 if xmax <= 48 else 12 if xmax <= 120 else 24
    for v in range(0, int(xmax) + 1, xstep):
        xx = left + plot_w * v / xmax
        pdf.line(xx, top, xx, top + h)
        pdf.set_xy(xx - 6, top + h + 1); pdf.cell(12, 4, f"{v} h", align="C")
    r = 1.1
    pdf.set_fill_color(*BLUE); pdf.set_draw_color(255, 255, 255); pdf.set_line_width(0.3)
    for q in events:
        hx = (q.time - a.main.time).total_seconds() / 3600
        xx = left + plot_w * min(hx, xmax) / xmax
        yy = top + h - h * (min(max(q.mag, ymin), ymax) - ymin) / (ymax - ymin)
        pdf.ellipse(xx - r, yy - r, 2 * r, 2 * r, style="FD")
    # principal
    pdf.set_fill_color(*ORANGE)
    yy = top + h - h * (min(a.main.mag, ymax) - ymin) / (ymax - ymin)
    pdf.ellipse(left - 1.6, yy - 1.6, 3.2, 3.2, style="FD")
    pdf.set_text_color(*INK); pdf.set_font("DV", "", 7)
    pdf.set_xy(left + 3, yy - 2); pdf.cell(40, 4, f"principal M{fmt_mag(a.main.mag)}")
    if a.biggest_after:
        hx = (a.biggest_after.time - a.main.time).total_seconds() / 3600
        xx = left + plot_w * min(hx, xmax) / xmax
        yb = top + h - h * (min(a.biggest_after.mag, ymax) - ymin) / (ymax - ymin)
        pdf.set_xy(min(xx + 2, right - 30), yb - 2); pdf.cell(30, 4, f"mayor réplica M{fmt_mag(a.biggest_after.mag)}")
    pdf.set_text_color(*INK2)
    pdf.set_xy(left, top + h + 6); pdf.cell(plot_w, 5, f"{len(events)} réplicas M4.5+ desde el principal · eje x: horas transcurridas", align="R")
    pdf.set_text_color(*INK)
    pdf.set_y(top + h + 13)


def make_pdf(a: Analysis, body: str, path: str) -> bool:
    """Informe completo en PDF (fpdf2 + DejaVu). Devuelve False si no hay librería/fuente."""
    try:
        from fpdf import FPDF  # type: ignore
    except ImportError:
        log("[pdf] fpdf2 no instalado — sin PDF")
        return False
    font_dir = "/usr/share/fonts/truetype/dejavu"
    reg, bold = os.path.join(font_dir, "DejaVuSans.ttf"), os.path.join(font_dir, "DejaVuSans-Bold.ttf")
    if not (os.path.exists(reg) and os.path.exists(bold)):
        log("[pdf] fuente DejaVu no encontrada — sin PDF")
        return False
    try:
        return _build_pdf(a, body, path, reg, bold)
    except Exception as exc:  # noqa: BLE001
        log(f"[pdf] falló la generación: {exc}")
        return False


def _build_pdf(a: Analysis, body: str, path: str, reg: str, bold: str) -> bool:
    from fpdf import FPDF  # type: ignore

    class Doc(FPDF):
        def footer(self):
            self.set_y(-11)
            self.set_font("DV", "", 7)
            self.set_text_color(*INK2)
            self.cell(0, 5, f"Monitor sísmico Panamá · fuentes: USGS, tsunami.gov, Google News (titulares sin verificar) · "
                            f"página {self.page_no()}/{{nb}}", align="C")
            self.set_text_color(*INK)

    t = local(a.now)
    pdf = Doc(format="A4")
    pdf.alias_nb_pages()
    pdf.set_margins(14, 14, 14)
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.add_font("DV", "", reg)
    pdf.add_font("DV", "B", bold)
    pdf.add_page()

    def section(num: int, title: str, min_space: float = 30) -> None:
        if pdf.get_y() + min_space > pdf.h - 18:
            pdf.add_page()
        pdf.ln(2)
        y = pdf.get_y()
        pdf.set_fill_color(*BLUE); pdf.rect(14, y, 1.6, 7, style="F")
        pdf.set_fill_color(236, 241, 250); pdf.rect(15.6, y, pdf.w - 29.6, 7, style="F")
        pdf.set_xy(18, y); pdf.set_font("DV", "B", 10.5); pdf.set_text_color(*INK)
        pdf.cell(0, 7, f"{num}. {title}", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1.5)

    def para(text: str, size: float = 9, bold_: bool = False, indent: float = 0, color=INK) -> None:
        pdf.set_font("DV", "B" if bold_ else "", size); pdf.set_text_color(*color)
        pdf.set_x(14 + indent)
        pdf.multi_cell(pdf.w - 28 - indent, 4.8, strip_emoji(text), new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(*INK)

    def linkline(label: str, url: str, size: float = 7.5, indent: float = 4) -> None:
        """Línea azul con enlace clicable (y la URL visible, para quien imprime)."""
        pdf.set_font("DV", "", size); pdf.set_text_color(*BLUE)
        pdf.set_x(14 + indent)
        shown = url if len(url) <= 95 else url[:92] + "…"
        pdf.multi_cell(pdf.w - 28 - indent, 4, f"{label} {shown}", link=url, align="L", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(*INK)

    # Encabezado
    pdf.set_font("DV", "B", 15)
    pdf.cell(0, 8, f"Sismo Panamá — informe de las {t:%H:%M} ({t:%d/%m/%Y})", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("DV", "", 9); pdf.set_text_color(*INK2)
    pdf.cell(0, 5, f"Principal M{fmt_mag(a.main.mag)} · {dhm(a.main.time)} · {a.main.place_short} · prof. {a.main.depth:.0f} km · "
                   f"hace {a.hours_since:.1f} h · generado automáticamente", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*INK); pdf.ln(2)
    pdf.set_fill_color(240, 244, 250); pdf.set_font("DV", "B", 10)
    pdf.multi_cell(0, 6, strip_emoji(body.split("\n", 1)[0]), fill=True, new_x="LMARGIN", new_y="NEXT")

    # 1. Resumen
    section(1, "Resumen")
    for ln in summary_bullets(a):
        para("• " + ln, indent=2)

    # 2. Réplicas
    section(2, "Réplicas — ventanas, por hora y decaimiento", min_space=60)
    pdf.set_font("DV", "", 9)
    lab_w = 34
    w = (pdf.w - 28 - lab_w) / len(WINDOWS_H)
    pdf.set_fill_color(246, 246, 244)
    pdf.cell(lab_w, 6, "réplicas en las…", border=1, fill=True)
    for h in WINDOWS_H:
        pdf.cell(w, 6, f"últimas {h} h", border=1, align="C", fill=True)
    pdf.ln(6)
    rows = (("M4.5 o más", {h: a.win45[h] for h in WINDOWS_H}, True),
            ("M3.5 a 4.4", {h: a.win35[h] - a.win45[h] for h in WINDOWS_H}, False),
            ("Total M3.5+", {h: a.win35[h] for h in WINDOWS_H}, True))
    for label, vals, bold_ in rows:
        pdf.set_font("DV", "B" if bold_ else "", 9)
        pdf.cell(lab_w, 6, label, border=1)
        for h in WINDOWS_H:
            pdf.cell(w, 6, str(vals[h]), border=1, align="C")
        pdf.ln(6)
    para("Cuenta de réplicas registradas en cada ventana hacia atrás desde ahora, sin contar el sismo principal. "
         "Fuente: catálogo USGS ComCat, caja Panamá. El USGS casi no incluye sismos menores de M4.5 en Panamá (los detecta la red "
         "local del Instituto de Geociencias, que no publica un feed), por eso la fila M3.5–4.4 suele ser 0.", size=7.5, color=INK2)
    linkline("Mapa interactivo USGS:", USGS_MAP, indent=0)
    linkline("Datos crudos 24 h (CSV):", usgs_catalog_url(a.now), indent=0)
    if a.biggest_last:
        para(f"Mayor de la última hora: M{fmt_mag(a.biggest_last.mag)} a las {hhmm(a.biggest_last.time)}, "
             f"{a.biggest_last.place_short}, prof. {a.biggest_last.depth:.0f} km.", indent=2)
    para(f"Acumulado desde el principal: {a.cum45} M4.5+ · {a.cum5} M5+ · {a.cum6} M6+"
         + (f" · mayor réplica M{fmt_mag(a.biggest_after.mag)} a las {dhm(a.biggest_after.time)}, {a.biggest_after.place_short}."
            if a.biggest_after else "."), indent=2)
    pdf.ln(2)
    draw_hourly_bars(pdf, a)
    draw_magnitude_dots(pdf, a)

    # 3. Tendencia y pronóstico
    section(3, "Tendencia y pronóstico", min_space=45)
    para(f"TENDENCIA: {a.trend} — {a.trend_note}.", bold_=True)
    para("Calculada por este monitor a partir del catálogo USGS (tasa de M4.5+/h, última hora vs horas 2–5 y 6–10).", size=7.5, color=INK2)
    fl = forecast_lines(a)
    para(fl[0].split(" — fuente:")[0] + ":", bold_=True)
    if a.fc.official:
        linkline("Pronóstico oficial USGS:", usgs_oaf_url(a.main.id), indent=0)
    else:
        linkline("Cuando el USGS lo publique aparecerá aquí:", usgs_oaf_url(a.main.id), indent=0)
    for ln in fl[1:]:
        if ln.strip().startswith("AVISO"):
            y = pdf.get_y(); pdf.set_fill_color(255, 244, 229)
            pdf.set_x(14); pdf.set_font("DV", "", 8.5)
            pdf.multi_cell(pdf.w - 28, 4.6, ln.strip(), fill=True, new_x="LMARGIN", new_y="NEXT")
        else:
            para(ln.strip(), indent=2)

    # 4. Situación oficial y servicios
    section(4, "Situación oficial y servicios", min_space=40)
    para("Titulares de prensa de las últimas 12–24 h, con hora y medio. La ausencia de noticias NO confirma que un servicio esté normal.",
         size=7.5, color=INK2)
    para("Tsunami (tsunami.gov): " + ("ALERTA ACTIVA" if a.tsunami.get("active") else "sin alerta activa" if a.tsunami.get("ok") else "feed no disponible"),
         bold_=True, size=9)
    for ln, url in a.tsunami.get("lines", [])[:2]:
        para("– " + ln, size=8.5, indent=4)
        linkline("↗", url, indent=7)
    if not a.tsunami.get("lines"):
        linkline("Boletines:", TSUNAMI_HOME, indent=4)
    for label, lines in a.services:
        para(label, bold_=True, size=9)
        if lines:
            for ln, url in lines:
                para("– " + ln, size=8.5, indent=4)
                linkline("↗", url, indent=7)
        else:
            para("– sin titulares específicos en las últimas horas", size=8.5, indent=4, color=INK2)

    # 5. Acciones
    section(5, "Acciones ahora", min_space=35)
    for i, act in enumerate(actions_for(a), 1):
        para(f"{i}. {act}", indent=2)

    # 6. Lista 24 h
    section(6, "Réplicas M4.5+ en las últimas 24 h", min_space=30)
    big24 = [q for q in a.day if q.mag >= 4.5]
    if big24:
        cols = (26, 14, pdf.w - 28 - 26 - 14 - 18, 18)
        pdf.set_font("DV", "B", 8); pdf.set_fill_color(246, 246, 244)
        for txt, cw in zip(("hora local", "M", "lugar", "prof."), cols):
            pdf.cell(cw, 5.5, txt, border="B", fill=True)
        pdf.ln(5.5)
        pdf.set_font("DV", "", 8)
        for q in big24[:40]:
            if pdf.get_y() + 6 > pdf.h - 18:
                pdf.add_page()
            pdf.cell(cols[0], 5, dhm(q.time)); pdf.cell(cols[1], 5, fmt_mag(q.mag))
            pdf.cell(cols[2], 5, q.place_short[:70]); pdf.cell(cols[3], 5, f"{q.depth:.0f} km")
            pdf.ln(5)
        if len(big24) > 40:
            para(f"… y {len(big24) - 40} más", size=8, color=INK2)
    else:
        para("Ninguna en las últimas 24 h.", indent=2)

    # 7. Teléfonos útiles
    section(7, "Teléfonos útiles — Panamá", min_space=60)
    para("Marque 911 para cualquier emergencia. 'oficial' = número tomado del sitio web de la entidad en la fecha indicada; "
         "'directorio' = tomado de directorios o prensa recientes, sin confirmar en sitio oficial. Ningún número fue probado por llamada.",
         size=7.5, color=INK2)
    cols = (60, 40, pdf.w - 28 - 60 - 40 - 32, 32)
    fonts = (("", 8), ("B", 8.5), ("", 7.5), ("", 7.5))
    lh = 4.3
    cat = ""

    def n_lines(text: str, width: float, style: str, size: float) -> int:
        pdf.set_font("DV", style, size)
        try:
            return max(1, len(pdf.multi_cell(width, lh, text, dry_run=True, output="LINES")))
        except Exception:  # noqa: BLE001
            return max(1, math.ceil(pdf.get_string_width(text) / max(width - 2, 1)) + 1)

    for c, name, tel, note, ver, url in PHONES:
        cells = (name, tel, note or "—", ver)
        row_h = lh * max(n_lines(t, w_, st, sz) for t, w_, (st, sz) in zip(cells, cols, fonts))
        if pdf.get_y() + row_h + 8 > pdf.h - 18:
            pdf.add_page()
        if c != cat:
            cat = c
            pdf.set_font("DV", "B", 8.5); pdf.set_fill_color(246, 246, 244); pdf.set_text_color(*INK)
            pdf.cell(sum(cols), 5.5, c, border="B", fill=True, new_x="LMARGIN", new_y="NEXT")
        y = pdf.get_y(); x = 14
        for i, (text, w_, (st, sz)) in enumerate(zip(cells, cols, fonts)):
            pdf.set_font("DV", st, sz)
            pdf.set_text_color(*(BLUE if i == 3 else INK2 if i == 2 else INK))
            pdf.set_xy(x, y)
            pdf.multi_cell(w_, lh, text, link=(url if i == 3 else ""), new_x="RIGHT", new_y="TOP", max_line_height=lh)
            x += w_
        pdf.set_text_color(*INK)
        pdf.set_draw_color(*GRID); pdf.set_line_width(0.1)
        pdf.line(14, y + row_h + 0.8, 14 + sum(cols), y + row_h + 0.8)
        pdf.set_y(y + row_h + 1.2)
    pdf.ln(1)

    # 8. Fuentes
    section(8, "Fuentes y avisos", min_space=40)
    para("Datos sísmicos: USGS (Servicio Geológico de EE. UU.), catálogo ComCat y pronóstico operativo de réplicas (OAF).", size=8.5, indent=2)
    linkline("Sismo principal:", usgs_event_url(a.main.id), size=8, indent=4)
    linkline("Pronóstico de réplicas:", usgs_oaf_url(a.main.id), size=8, indent=4)
    linkline("Mapa de réplicas:", USGS_MAP, size=8, indent=4)
    linkline("Datos crudos (CSV):", usgs_catalog_url(a.now), size=8, indent=4)
    para("Tsunami: Pacific Tsunami Warning Center / National Tsunami Warning Center (NOAA).", size=8.5, indent=2)
    linkline("Boletines:", TSUNAMI_HOME, size=8, indent=4)
    para("Servicios y situación oficial: titulares de prensa vía Google News (es-419, Panamá); cada titular lleva su enlace en la sección 4. "
         "No se verifican; para confirmar un servicio consulte directamente a Sinaproc, IDAAN, ENSA/Naturgy, Tocumen, MOP, Metro de Panamá o MEDUCA.",
         size=8.5, indent=2)
    para("Tendencia y conteos por ventana: cálculo propio de este monitor sobre el catálogo USGS. Sin pronóstico oficial, el pronóstico usa el "
         "modelo Reasenberg-Jones (1989) con parámetros genéricos, que suele sobreestimar.", size=8.5, indent=2)
    warnings = [x for x in LOG if x.startswith("[")]
    if warnings:
        para("Avisos de esta ronda: " + " | ".join(warnings[-6:]), size=8, indent=2, color=INK2)
    pdf.output(path)
    return True


def _multipart(fields: dict[str, str], file_field: str, filename: str, content: bytes, mime: str) -> tuple[bytes, str]:
    boundary = "----sismo" + str(int(time.time() * 1000))
    out = bytearray()
    for k, v in fields.items():
        out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {mime}\r\n\r\n").encode() + content + f"\r\n--{boundary}--\r\n".encode()
    return bytes(out), boundary


def _telegram_api(token: str, method: str, data: dict | None = None, timeout: int = 30) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def telegram_discover_chats(token: str, store_path: str) -> dict[str, str]:
    """Descubre los chats que hablaron con el bot (o lo agregaron a un grupo) y los guarda en reports/."""
    chats: dict[str, str] = {}
    try:
        if os.path.exists(store_path):
            with open(store_path, encoding="utf-8") as fh:
                chats = {str(k): str(v) for k, v in json.load(fh).items()}
    except (OSError, json.JSONDecodeError):
        chats = {}
    try:
        res = _telegram_api(token, "getUpdates", {"limit": 100, "allowed_updates": '["message","my_chat_member"]'})
        for upd in res.get("result", []):
            chat = None
            if "message" in upd:
                msg = upd["message"]
                # un grupo que pasa a supergrupo cambia de id: migrar
                if msg.get("migrate_to_chat_id") is not None:
                    chats.pop(str(msg.get("chat", {}).get("id")), None)
                    chats[str(msg["migrate_to_chat_id"])] = str(msg.get("chat", {}).get("title") or "grupo")
                    continue
                if msg.get("left_chat_member", {}).get("is_bot") if isinstance(msg.get("left_chat_member"), dict) else False:
                    continue
                chat = msg.get("chat")
            elif "my_chat_member" in upd:
                mcm = upd["my_chat_member"]
                if (mcm.get("new_chat_member") or {}).get("status") in ("left", "kicked"):
                    chats.pop(str(mcm.get("chat", {}).get("id")), None)
                    continue
                chat = mcm.get("chat")
            if chat and chat.get("id") is not None:
                label = chat.get("title") or chat.get("first_name") or chat.get("username") or chat.get("type")
                chats[str(chat["id"])] = str(label)
    except Exception as exc:  # noqa: BLE001
        log(f"[telegram] getUpdates falló: {exc}")
    try:
        os.makedirs(os.path.dirname(store_path) or ".", exist_ok=True)
        with open(store_path, "w", encoding="utf-8") as fh:
            json.dump(chats, fh, ensure_ascii=False, indent=1)
    except OSError as exc:
        log(f"[telegram] no se guardó {store_path}: {exc}")
    return chats


def send_telegram(text: str, pdf_path: str | None = None, pdf_caption: str = "") -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return "telegram: omitido (sin TELEGRAM_BOT_TOKEN)"
    forced = [x.strip() for x in os.environ.get("TELEGRAM_CHAT_ID", "").replace(";", ",").split(",") if x.strip()]
    store = os.path.join(os.environ.get("REPORTS_DIR", "reports"), "telegram_chats.json")
    chats = {c: "TELEGRAM_CHAT_ID" for c in forced} if forced else telegram_discover_chats(token, store)
    if not chats:
        return "telegram: ningún chat todavía — envíe /start al bot (o agréguelo a un grupo) y espere la próxima ronda"
    groups = {c: l for c, l in chats.items() if c.startswith("-")}
    skipped = ""
    if groups and os.environ.get("TELEGRAM_ALSO_PRIVATE", "0") != "1" and not forced:
        skipped = " | chats privados omitidos (hay grupo; TELEGRAM_ALSO_PRIVATE=1 para enviar a ambos)"
        chats = groups
    if DRY_RUN:
        return "telegram: DRY_RUN para " + ", ".join(chats.values())
    results = []
    pdf_bytes = None
    if pdf_path and os.path.exists(pdf_path):
        with open(pdf_path, "rb") as fh:
            pdf_bytes = fh.read()
    for chat_id, label in chats.items():
        try:
            res = _telegram_api(token, "sendMessage", {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"})
            ok_msg = bool(res.get("ok"))
            ok_pdf = None
            if pdf_bytes:
                body, boundary = _multipart({"chat_id": chat_id, "caption": pdf_caption[:1000]}, "document",
                                            os.path.basename(pdf_path), pdf_bytes, "application/pdf")
                req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendDocument", data=body,
                                             headers={"User-Agent": UA, "Content-Type": f"multipart/form-data; boundary={boundary}"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    ok_pdf = bool(json.loads(resp.read().decode("utf-8", "ignore")).get("ok"))
            status = "ok" if ok_msg else "FALLÓ " + str(res.get("description"))[:60]
            if ok_pdf is not None:
                status += " + pdf" if ok_pdf else " (pdf FALLÓ)"
            results.append(f"{label}: {status}")
        except Exception as exc:  # noqa: BLE001
            results.append(f"{label}: FALLÓ {str(exc)[:80]}")
    return "telegram: " + " | ".join(results) + skipped


# --------------------------------------------------------------------------- Main

def main() -> int:
    now = datetime.now(timezone.utc)
    if FIXTURES_DIR and os.environ.get("FAKE_NOW"):
        now = datetime.fromisoformat(os.environ["FAKE_NOW"]).astimezone(timezone.utc)
    log(f"Ronda {local(now):%d/%m/%Y %H:%M} (Panamá) / {now:%H:%M} UTC")

    main_q = find_mainshock(now)
    if not main_q:
        log("Ningún sismo M6.5+ en la región en los últimos 30 días — nada que informar.")
        return 0
    log(f"Principal: {main_q.id} M{main_q.mag} {main_q.time:%Y-%m-%d %H:%M} UTC {main_q.place}")

    day = usgs_query("usgs_day.json", starttime=iso(now - timedelta(hours=24, minutes=5)), endtime=iso(now),
                     minmagnitude=3.0, limit=2000)
    cum = usgs_query("usgs_cum.json", starttime=iso(main_q.time), endtime=iso(now), minmagnitude=4.5, limit=2000)

    fc = oaf_forecast(fetch_oaf(main_q.id)) or generic_forecast(main_q.mag, (now - main_q.time).total_seconds() / 86400)
    tsunami = fetch_tsunami(now)
    services = fetch_services(now)

    a = analyse(now, main_q, day, cum, fc, tsunami, services)
    subject, body = email_report(a)
    short = family_message(a)

    print("\n" + "=" * 78)
    print(body)
    print("=" * 78 + "\n")

    out_dir = os.environ.get("REPORTS_DIR", "reports")
    os.makedirs(out_dir, exist_ok=True)
    pdf_path = os.path.join(out_dir, f"sismo-panama_{local(now):%Y-%m-%d_%H%M}.pdf")
    has_pdf = make_pdf(a, body, pdf_path)
    caption = f"Informe completo {local(now):%d/%m %H:%M} — réplicas, tendencia, pronóstico, servicios, acciones y teléfonos."
    every = 6 if a.quiet else SEND_EVERY_HOURS
    force = os.environ.get("FORCE_SEND", "0") == "1"
    # El cron de GitHub es poco fiable: en vez de "hora exacta", se envía cuando pasaron ≥ N horas desde el último
    # informe regular (con 10 min de tolerancia). Así, la primera corrida que ocurra después del plazo envía.
    sent_path = os.path.join(out_dir, "last_sent.json")
    last_at = None
    try:
        with open(sent_path, encoding="utf-8") as fh:
            last_at = datetime.fromisoformat(json.load(fh).get("at"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        last_at = None
    elapsed_h = (now - last_at).total_seconds() / 3600 if last_at else 1e9
    regular_due = elapsed_h >= every - 10 / 60
    recently = elapsed_h < 50 / 60  # anti-duplicado: nada dos veces en menos de 50 min
    if force or (regular_due and not recently) or (a.alert and not recently):
        why = ("envío forzado" if force else "informe regular" if regular_due else "alerta: " + a.alert_reason)
        status = [send_email(subject, body), send_callmebot(short),
                  send_telegram(short, pdf_path if has_pdf else None, caption)]
        status.append("motivo: " + why)
        if not DRY_RUN:
            try:
                with open(sent_path, "w", encoding="utf-8") as fh:
                    json.dump({"at": now.isoformat(), "why": why, "local": f"{local(now):%d/%m %H:%M}"}, fh)
            except OSError:
                pass
    elif recently:
        status = [f"no enviado — ya se envió hace {elapsed_h * 60:.0f} min"]
    else:
        status = [f"no enviado — próximo informe regular en {max(every - elapsed_h, 0):.1f} h; alerta solo con "
                  f"M{ALERT_MIN_MAG:.1f}+, tsunami o reactivación"]
    if has_pdf:
        try:
            os.remove(pdf_path)  # no se versiona el PDF (solo se envía)
        except OSError:
            pass
    for s in status:
        print(s)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(f"## {subject}\n\n```\n{body}\n```\n\n" + "\n".join(f"- {s}" for s in status) + "\n")

    try:
        with open(os.path.join(out_dir, "latest.txt"), "w", encoding="utf-8") as fh:
            fh.write(body + "\n\nENVÍO: " + " | ".join(status) + "\n")
        with open(os.path.join(out_dir, "latest_short.txt"), "w", encoding="utf-8") as fh:
            fh.write(short + "\n")
        hist = os.path.join(out_dir, "historico.md")
        new_file = not os.path.exists(hist)
        with open(hist, "a", encoding="utf-8") as fh:
            if new_file:
                fh.write("| fecha/hora (Panamá) | 1 h | 5 h | 10 h | 18 h | 24 h (M4.5+) | mayor 1 h | acum. M5+/M6+ | tendencia | tsunami | P(M6+ 7d) | envío |\n"
                         "|---|---|---|---|---|---|---|---|---|---|---|---|\n")
            big = f"M{fmt_mag(a.biggest_last.mag)} {hhmm(a.biggest_last.time)}" if a.biggest_last else "—"
            env_short = "; ".join(s.split(":")[0] + (":ok" if ("enviado" in s or ": ok" in s or "HTTP 200" in s) else ":-")
                                  for s in status)
            p6 = a.fc.p.get(("7d", 6))
            fh.write(f"| {local(now):%d/%m %H:%M} | " + " | ".join(str(a.win45[h]) for h in WINDOWS_H)
                     + f" | {big} | {a.cum5}/{a.cum6} | {a.trend.lower()} | {'ALERTA' if a.tsunami.get('active') else 'ok'} | "
                       f"{pct(p6)}{'' if a.fc.official else ' (gen.)'} | {env_short} |\n")
    except OSError as exc:
        print(f"[reports] no se guardó: {exc}")

    failed = [s for s in status if "FALLÓ" in s]
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"ERROR FATAL: {exc}")
        sys.exit(2)
