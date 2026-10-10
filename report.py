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
        relevant: list[tuple[datetime, str, bool]] = []
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
            relevant.append((when, f"{name} {dhm(when)}: {title[:60]}" + (f" — {snippet}" if snippet else ""), is_threat and not is_over))
        relevant.sort(key=lambda r: r[0], reverse=True)
        result["lines"].extend(r[1] for r in relevant)
        if relevant and relevant[0][2] and now - relevant[0][0] <= timedelta(hours=6):
            result["active"] = True
    return result


# --------------------------------------------------------------------------- Prensa por tema

def fetch_news_topic(now: datetime, key: str, query: str, hours: int, limit: int = 2,
                     must: tuple[str, ...] = ()) -> list[str]:
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
    items: list[tuple[datetime, str]] = []
    seen: set[str] = set()
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        source = (item.findtext("source") or "").strip()
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
        items.append((when, f"{dhm(when)} {source}: {title}"[:170]))
    items.sort(key=lambda x: x[0], reverse=True)
    return [t for _, t in items[:limit]]


def fetch_services(now: datetime) -> list[tuple[str, list[str]]]:
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
    services: list[tuple[str, list[str]]]
    quiet: bool


def analyse(now: datetime, main: Quake, day: list[Quake], cum: list[Quake], fc: Forecast,
            tsunami: dict, services: list[tuple[str, list[str]]]) -> Analysis:
    day = [q for q in day if q.id != main.id and q.time > main.time]
    after = [q for q in cum if q.id != main.id and q.time > main.time]
    hours_since = (now - main.time).total_seconds() / 3600

    def in_win(h_from: float, h_to: float, mmin: float) -> int:
        return sum(1 for q in day if q.mag >= mmin and h_to <= (now - q.time).total_seconds() / 3600 < h_from)

    win45 = {h: in_win(h, 0, 4.5) for h in WINDOWS_H}
    win35 = {h: in_win(h, 0, 3.5) for h in WINDOWS_H}
    last = [q for q in day if now - q.time <= timedelta(hours=1)]
    biggest_last = max(last, key=lambda q: q.mag) if last else None
    rate_last1 = float(win45[1])
    rate_h2to5 = in_win(5, 1, 4.5) / 4.0
    rate_h6to10 = in_win(10, 5, 4.5) / 5.0
    cum45 = sum(1 for q in after if q.mag >= 4.5)
    cum5 = sum(1 for q in after if q.mag >= 5.0)
    cum6 = sum(1 for q in after if q.mag >= 6.0)
    biggest_after = max(after, key=lambda q: q.mag) if after else None
    m6_recent = any(q.mag >= 6.0 and now - q.time <= timedelta(hours=3) for q in after + day)

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
    return Analysis(now, main, hours_since, day, after, win45, win35, rate_last1, rate_h2to5, rate_h6to10,
                    biggest_last, cum45, cum5, cum6, biggest_after, m6_recent, trend, note, fc, tsunami, services, quiet)


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
    return " | ".join(f"{h} h: {a.win45[h]} M4.5+ ({a.win35[h]} M3.5+)" for h in WINDOWS_H)


def forecast_lines(a: Analysis) -> list[str]:
    f = a.fc
    L = [f"PRONÓSTICO [{f.label}]:"]
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
                f"Tsunami: sin alerta. Próxima actualización en 1 h.")
    if a.biggest_last:
        b = a.biggest_last
        last_line = (f"Última hora: {a.win35[1]} temblores M3.5+ ({a.win45[1]} de M4.5+). "
                     f"Mayor: M{fmt_mag(b.mag)} a las {hhmm(b.time)}, {b.place_short}.")
    else:
        last_line = "Última hora: ningún temblor M3.5+ registrado."
    wins = " · ".join(f"{h} h: {a.win45[h]}" for h in WINDOWS_H)
    big = (f" Mayor réplica hasta ahora: M{fmt_mag(a.biggest_after.mag)} a las {hhmm(a.biggest_after.time)}."
           if a.biggest_after else "")
    tsu = "ALERTA ACTIVA en el Pacífico — alejarse de la costa" if a.tsunami.get("active") else "sin alerta"
    p6 = a.fc.p.get(("7d", 6))
    tag = "USGS" if a.fc.official else "modelo genérico, sin pronóstico oficial aún"
    top = actions_for(a, short=True)[:2]
    lines = [
        f"🔔 Sismo PA {t} (automático)",
        last_line,
        f"Réplicas M4.5+ por ventana — {wins}. Tendencia: {a.trend.lower()} ({a.trend_note}).{big}",
        f"Desde el principal (M{fmt_mag(a.main.mag)}, {hhmm(a.main.time)}): {a.cum5} réplicas M5+, {a.cum6} M6+.",
        f"Tsunami: {tsu}. P(M6+ en 7 días): {pct(p6)} ({tag}).",
        "Ahora: " + " ".join(top),
        "Próxima actualización en 1 h.",
    ]
    msg = "\n".join(lines)
    return msg if len(msg) <= 1200 else msg[:1180] + "…"


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
    L.append("RÉPLICAS POR VENTANA (catálogo USGS, caja Panamá):")
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
        for ln in a.tsunami["lines"][:2]:
            L.append("    - " + ln)
    else:
        L.append("  Tsunami: feed de tsunami.gov no disponible en esta ronda.")
    for label, lines in a.services:
        if lines:
            L.append(f"  {label}:")
            for ln in lines:
                L.append("    - " + ln)
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
    L.append("FUENTES:")
    L.append("  https://earthquake.usgs.gov/earthquakes/eventpage/" + a.main.id)
    L.append("  https://earthquake.usgs.gov/fdsnws/event/1/ (catálogo, caja Panamá)")
    L.append("  https://www.tsunami.gov/")
    L.append("  https://news.google.com/ (titulares por tema, es-419/PA)")
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
                chat = upd["message"].get("chat")
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


def send_telegram(text: str) -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return "telegram: omitido (sin TELEGRAM_BOT_TOKEN)"
    forced = [x.strip() for x in os.environ.get("TELEGRAM_CHAT_ID", "").replace(";", ",").split(",") if x.strip()]
    store = os.path.join(os.environ.get("REPORTS_DIR", "reports"), "telegram_chats.json")
    chats = {c: "TELEGRAM_CHAT_ID" for c in forced} if forced else telegram_discover_chats(token, store)
    if not chats:
        return "telegram: ningún chat todavía — envíe /start al bot (o agréguelo a un grupo) y espere la próxima ronda"
    if DRY_RUN:
        return "telegram: DRY_RUN para " + ", ".join(chats.values())
    results = []
    for chat_id, label in chats.items():
        try:
            res = _telegram_api(token, "sendMessage", {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"})
            results.append(f"{label}: {'ok' if res.get('ok') else 'FALLÓ ' + str(res.get('description'))[:60]}")
        except Exception as exc:  # noqa: BLE001
            results.append(f"{label}: FALLÓ {str(exc)[:80]}")
    return "telegram: " + " | ".join(results)


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

    repo = os.environ.get("GITHUB_REPOSITORY")
    tg_text = short + (f"\n\nInforme completo: https://github.com/{repo}/blob/main/reports/latest.txt" if repo else "")
    if a.quiet and local(now).hour % 6 != 0:
        status = [send_email(subject, body), "whatsapp: omitido (hora tranquila — solo a las 00/06/12/18 h)",
                  "telegram: omitido (hora tranquila — solo a las 00/06/12/18 h)"]
    else:
        status = [send_email(subject, body), send_callmebot(short), send_telegram(tg_text)]
    for s in status:
        print(s)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(f"## {subject}\n\n```\n{body}\n```\n\n" + "\n".join(f"- {s}" for s in status) + "\n")

    out_dir = os.environ.get("REPORTS_DIR", "reports")
    try:
        os.makedirs(out_dir, exist_ok=True)
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
