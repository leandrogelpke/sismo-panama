#!/usr/bin/env python3
"""
Monitor sísmico Panamá — relatório horário automático.

Roda no GitHub Actions (sem dependências além da biblioteca padrão do Python).
Fontes: USGS (catálogo + previsão oficial de réplicas), tsunami.gov (PTWC/NTWC),
Google News (manchetes de imprensa). Entrega: e-mail (Gmail SMTP), WhatsApp
(CallMeBot, pessoal), Telegram (opcional).

Variáveis de ambiente (todas opcionais — o que faltar é pulado):
  GMAIL_USER, GMAIL_APP_PASSWORD, MAIL_TO      e-mail (MAIL_TO: lista separada por vírgula)
  CALLMEBOT_RECIPIENTS                        "+507xxxxxxxx:APIKEY;+507yyyyyyyy:APIKEY"
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID        Telegram
  MAINSHOCK_ID                                força o id USGS do sismo principal
  DRY_RUN=1                                   só imprime, não envia nada
  FIXTURES_DIR                                lê respostas de arquivos (testes)
  QUIET_AFTER_HOURS (72)                      modo "hora tranquila" após N h sem M4.5+ em 6 h
"""
from __future__ import annotations

import io
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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime

PANAMA_TZ = timezone(timedelta(hours=-5), name="America/Panama")
UA = "sismo-panama/1.0 (GitHub Actions; uso pessoal)"
USGS_QUERY = "https://earthquake.usgs.gov/fdsnws/event/1/query"
BBOX = {"minlatitude": 5.5, "maxlatitude": 10.5, "minlongitude": -84.5, "maxlongitude": -76.5}
TSUNAMI_FEEDS = {
    "PTWC": "https://www.tsunami.gov/events/xml/PHEBAtom.xml",
    "NTWC": "https://www.tsunami.gov/events/xml/PAAQAtom.xml",
}
NEWS_QUERIES = {
    "sismo": "sismo Panamá when:6h",
    "sinaproc": "Sinaproc when:12h",
}

FIXTURES_DIR = os.environ.get("FIXTURES_DIR")
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
QUIET_AFTER_HOURS = float(os.environ.get("QUIET_AFTER_HOURS", "72"))

LOG: list[str] = []


def log(msg: str) -> None:
    print(msg, flush=True)
    LOG.append(msg)


# --------------------------------------------------------------------------- HTTP

def http_get(url: str, key: str, timeout: int = 30, retries: int = 2) -> bytes | None:
    """GET com retries. Em modo FIXTURES, lê tests/fixtures/<key>."""
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
    log(f"[falha HTTP] {key}: {last_err}")
    return None


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def local(dt: datetime) -> datetime:
    return dt.astimezone(PANAMA_TZ)


def hhmm(dt: datetime) -> str:
    return local(dt).strftime("%H:%M")


def fmt_mag(m: float) -> str:
    return f"{m:.1f}".replace(".", ",")


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
        p = self.place or "local não informado"
        for suffix in (", Panama", ", Panamá", ", Costa Rica", ", Colombia"):
            if p.endswith(suffix):
                p = p[: -len(suffix)] + (" (CR)" if "Costa" in suffix else " (CO)" if "Colomb" in suffix else "")
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
        out.append(
            Quake(
                id=str(f.get("id") or p.get("code") or ""),
                time=datetime.fromtimestamp(p["time"] / 1000, tz=timezone.utc),
                mag=float(p["mag"]),
                place=str(p.get("place") or ""),
                lat=float(g[1]) if g[1] is not None else float("nan"),
                lon=float(g[0]) if g[0] is not None else float("nan"),
                depth=float(g[2]) if g[2] is not None else float("nan"),
            )
        )
    out.sort(key=lambda q: q.time, reverse=True)
    return out


def usgs_query(key: str, **params) -> list[Quake]:
    q = {"format": "geojson", "orderby": "time", **BBOX, **params}
    url = USGS_QUERY + "?" + urllib.parse.urlencode(q)
    return parse_geojson(http_get(url, key))


def find_mainshock(now: datetime) -> Quake | None:
    forced = os.environ.get("MAINSHOCK_ID")
    if forced:
        url = USGS_QUERY + "?" + urllib.parse.urlencode({"format": "geojson", "eventid": forced})
        raw = http_get(url, "usgs_main.json")
        if raw:
            try:
                d = json.loads(raw)
                qs = parse_geojson(json.dumps({"features": [d]}).encode())
                if qs:
                    return qs[0]
            except Exception as exc:  # noqa: BLE001
                log(f"[USGS] mainshock forçado inválido: {exc}")
    qs = usgs_query(
        "usgs_main.json",
        starttime=iso(now - timedelta(days=30)),
        minmagnitude=6.5,
        orderby="magnitude",
        limit=1,
    )
    return qs[0] if qs else None


def fetch_oaf(mainshock_id: str) -> dict | None:
    """Previsão oficial de réplicas (USGS Operational Aftershock Forecast)."""
    url = USGS_QUERY + "?" + urllib.parse.urlencode({"format": "geojson", "eventid": mainshock_id})
    raw = http_get(url, "event_detail.json")
    if not raw:
        return None
    try:
        detail = json.loads(raw)
        products = detail["properties"]["products"]
        oaf = products.get("oaf") or []
        if not oaf:
            log("[OAF] evento sem produto 'oaf' ainda")
            return None
        contents = oaf[0].get("contents", {})
        fc_url = contents.get("forecast.json", {}).get("url")
        if not fc_url:
            log("[OAF] forecast.json ausente")
            return None
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        log(f"[OAF] detalhe do evento inesperado: {exc}")
        return None
    raw_fc = http_get(fc_url, "forecast.json")
    if not raw_fc:
        return None
    try:
        return json.loads(raw_fc)
    except json.JSONDecodeError as exc:
        log(f"[OAF] forecast.json inválido: {exc}")
        return None


def oaf_probabilities(fc: dict | None) -> dict[str, float | None]:
    """Extrai P(M≥5, 1 dia), P(M≥6, 7 dias), P(M≥7, 7 dias) do forecast.json."""
    res: dict[str, float | None] = {"p5_1d": None, "p6_7d": None, "p7_7d": None, "n5_1d": None, "label": None}
    if not fc:
        return res
    windows = fc.get("forecast") or []
    by_label = {}
    for w in windows:
        lbl = (w.get("label") or "").lower()
        by_label[lbl] = w

    def pick(lbl_opts: tuple[str, ...], mag: int) -> tuple[float | None, float | None]:
        for lbl in lbl_opts:
            w = by_label.get(lbl)
            if not w:
                continue
            for b in w.get("bins", []):
                try:
                    if int(round(float(b.get("magnitude")))) == mag:
                        p = b.get("probability")
                        n_lo, n_hi = b.get("p95minimum"), b.get("p95maximum")
                        n_mid = None if n_lo is None or n_hi is None else (float(n_lo) + float(n_hi)) / 2
                        return (None if p is None else float(p)), n_mid
                except (TypeError, ValueError):
                    continue
        return None, None

    res["p5_1d"], res["n5_1d"] = pick(("1 day", "1 dia"), 5)
    res["p6_7d"], _ = pick(("1 week", "7 days", "1 semana"), 6)
    res["p7_7d"], _ = pick(("1 week", "7 days", "1 semana"), 7)
    issued = fc.get("creationTime") or fc.get("issueTime")
    if issued:
        try:
            res["label"] = "USGS OAF emitida " + hhmm(datetime.fromtimestamp(float(issued) / 1000, tz=timezone.utc))
        except (TypeError, ValueError):
            res["label"] = "USGS OAF"
    else:
        res["label"] = "USGS OAF"
    return res


def generic_rj(mainshock_mag: float, t1_days: float, t2_days: float, mag: float) -> float:
    """Número esperado de réplicas M≥mag entre t1 e t2 (dias após o principal),
    modelo genérico Reasenberg-Jones (a=-1.67, b=0.91, p=1.08, c=0.05)."""
    a, b, p, c = -1.67, 0.91, 1.08, 0.05
    k = 10 ** (a + b * (mainshock_mag - mag))
    integral = ((t2_days + c) ** (1 - p) - (t1_days + c) ** (1 - p)) / (1 - p)
    return k * integral


def generic_probabilities(mainshock_mag: float, days_since: float) -> dict[str, float | None]:
    t = max(days_since, 0.01)
    n5 = generic_rj(mainshock_mag, t, t + 1, 5.0)
    n6 = generic_rj(mainshock_mag, t, t + 7, 6.0)
    n7 = generic_rj(mainshock_mag, t, t + 7, 7.0)
    return {
        "p5_1d": 1 - math.exp(-n5),
        "p6_7d": 1 - math.exp(-n6),
        "p7_7d": 1 - math.exp(-n7),
        "n5_1d": n5,
        "label": "estimativa, modelo genérico — USGS OAF indisponível",
    }


# --------------------------------------------------------------------------- Tsunami

def fetch_tsunami(now: datetime) -> dict:
    """Lê os feeds Atom do PTWC/NTWC e devolve boletins recentes que citem Panamá/Pacífico."""
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
            summary = (entry.findtext("a:summary", default="", namespaces=ns) or "")
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
            relevant.append((when, f"{name} {hhmm(when)}: {title[:140]}", is_threat and not is_over))
        relevant.sort(key=lambda r: r[0], reverse=True)
        result["lines"].extend(r[1] for r in relevant)
        # só o boletim MAIS RECENTE decide: se ele é cancelamento/"passed", não há alerta ativo
        if relevant and relevant[0][2] and now - relevant[0][0] <= timedelta(hours=6):
            result["active"] = True
    return result


# --------------------------------------------------------------------------- Imprensa

def fetch_news(now: datetime) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for key, q in NEWS_QUERIES.items():
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
            {"q": q, "hl": "es-419", "gl": "PA", "ceid": "PA:es-419"}
        )
        raw = http_get(url, f"news_{key}.xml")
        if not raw:
            continue
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            log(f"[news] {key}: XML inválido: {exc}")
            continue
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            pub = item.findtext("pubDate") or ""
            source = (item.findtext("source") or "").strip()
            try:
                when = parsedate_to_datetime(pub)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
            if now - when > timedelta(hours=12):
                continue
            norm = title.lower()[:60]
            if norm in seen:
                continue
            seen.add(norm)
            if source and title.endswith(" - " + source):
                title = title[: -(len(source) + 3)]
            lines.append(f"{hhmm(when)} {source}: {title}"[:170])
    lines.sort(reverse=True)
    return lines[:6]


# --------------------------------------------------------------------------- Análise

@dataclass
class Analysis:
    now: datetime
    main: Quake
    hours_since: float
    last: list[Quake]
    prev: list[Quake]
    cum: list[Quake]
    n35: int
    n45: int
    n45_prev: int
    biggest_last: Quake | None
    cum45: int
    cum5: int
    cum6: int
    biggest_after: Quake | None
    m6_recent: bool
    trend: str
    probs: dict
    tsunami: dict
    news: list[str]
    quiet: bool


def analyse(now: datetime, main: Quake, last: list[Quake], prev: list[Quake], cum: list[Quake],
            probs: dict, tsunami: dict, news: list[str]) -> Analysis:
    last = [q for q in last if q.id != main.id]
    prev = [q for q in prev if q.id != main.id]
    after = [q for q in cum if q.id != main.id and q.time > main.time]
    n35 = sum(1 for q in last if q.mag >= 3.5)
    n45 = sum(1 for q in last if q.mag >= 4.5)
    n45_prev = sum(1 for q in prev if q.mag >= 4.5)
    biggest_last = max(last, key=lambda q: q.mag) if last else None
    cum45 = sum(1 for q in after if q.mag >= 4.5)
    cum5 = sum(1 for q in after if q.mag >= 5.0)
    cum6 = sum(1 for q in after if q.mag >= 6.0)
    biggest_after = max(after, key=lambda q: q.mag) if after else None
    m6_recent = any(q.mag >= 6.0 and now - q.time <= timedelta(hours=3) for q in after + last)
    hours_since = (now - main.time).total_seconds() / 3600
    if n45 > n45_prev:
        trend = "REATIVADA"
    elif n45 == n45_prev and n45 > 0:
        trend = "ESTÁVEL"
    else:
        trend = "DECAINDO"
    recent6h_45 = any(q.mag >= 4.5 and now - q.time <= timedelta(hours=6) for q in after + last)
    quiet = hours_since > QUIET_AFTER_HOURS and not recent6h_45 and not tsunami.get("active")
    return Analysis(now, main, hours_since, last, prev, after, n35, n45, n45_prev, biggest_last,
                    cum45, cum5, cum6, biggest_after, m6_recent, trend, probs, tsunami, news, quiet)


def pct(p: float | None) -> str:
    return "n/d" if p is None else f"{round(p * 100):d}%"


def actions_for(a: Analysis, short: bool = False) -> list[str]:
    """Ações derivadas dos dados. short=True devolve a versão curta (WhatsApp)."""
    hour = local(a.now).hour
    night = hour >= 21 or hour < 6
    acts: list[tuple[str, str]] = []  # (versão completa, versão curta)
    if a.tsunami.get("active"):
        acts.append(("[Certo] Boletim de tsunami ATIVO no Pacífico: quem estiver na orla (Cinta Costera, Costa del Este, "
                     "Punta Pacífica, Amador) afasta-se da costa agora e segue o Sinaproc.",
                     "Alerta de tsunami ativo: quem estiver na orla, afastar-se da costa."))
    if a.m6_recent:
        acts.append(("[Provável] Houve M6+ nas últimas 3 h: antes de dormir, olhar colunas, vigas e paredes por fissuras "
                     "novas (fotografar para comparar). Rachadura diagonal ou em X em coluna = não dormir no apartamento.",
                     "Houve réplica forte (M6+) há menos de 3 h: olhar paredes e colunas por fissuras novas antes de dormir."))
    elif a.trend == "REATIVADA":
        acts.append(("[Provável] Sequência reativada nesta hora: adiar deslocamentos não essenciais e manter a rota de saída livre.",
                     "Réplicas aumentaram nesta hora: adiar saídas não essenciais, rota de saída livre."))
    if night:
        acts.append(("[Certo] Para a noite: sapatos fechados, lanterna, chaves e celular carregado ao lado da cama; "
                     "porta do quarto destrancada; em tremor forte, proteger a cabeça junto à cama e esperar passar antes de descer.",
                     "Noite: sapatos, lanterna e celular ao lado da cama; em tremor forte, proteger a cabeça e esperar passar."))
    else:
        acts.append(("[Certo] Elevador não — escada sempre, até o prédio ser inspecionado.",
                     "Elevador não, escada sempre."))
    if a.hours_since < 48:
        acts.append(("[Provável] Evitar viagens longas pela Panamericana rumo a Coclé/Azuero nas próximas 24–48 h: "
                     "pontes e viadutos ainda estão sendo inspecionados.",
                     "Evitar viagens longas pela Panamericana (Coclé/Azuero) nas próximas 24–48 h."))
    if a.probs.get("p6_7d") is not None:
        acts.append((f"[Estimativa] Réplica M6+ nos próximos 7 dias: {pct(a.probs['p6_7d'])} — sentida em prédios altos "
                     "da capital; isso é esperado, não sinal de novo terremoto.",
                     f"Réplica M6+ esta semana: {pct(a.probs['p6_7d'])} de chance — esperado, não é novo terremoto."))
    if a.quiet or (a.n45 == 0 and a.hours_since > 24):
        acts.append(("[Provável] Hora tranquila: manter a rotina normal com as precauções básicas; nada novo a fazer.",
                     "Hora tranquila: rotina normal com as precauções básicas."))
    return [s if short else f for f, s in acts[:6]]


# --------------------------------------------------------------------------- Textos

def family_message(a: Analysis) -> str:
    t = hhmm(a.now)
    if a.quiet:
        return (f"🔔 Sismo PA {t} (automático)\n"
                f"Hora tranquila: sem réplicas relevantes nas últimas 6 h. Tendência {a.trend.lower()}.\n"
                f"Tsunami: sem alerta. Próxima atualização em 1 h.")
    if a.biggest_last:
        b = a.biggest_last
        last_line = (f"Última hora: {a.n35} tremores M3,5+ ({a.n45} de M4,5+). "
                     f"Maior: M{fmt_mag(b.mag)} às {hhmm(b.time)}, {b.place_short}.")
    else:
        last_line = "Última hora: nenhum tremor M3,5+ registrado."
    big = (f"Maior réplica até agora: M{fmt_mag(a.biggest_after.mag)} às {hhmm(a.biggest_after.time)}."
           if a.biggest_after else "")
    tsu = "ALERTA ATIVO no Pacífico — afastar-se da orla" if a.tsunami.get("active") else "sem alerta"
    top = actions_for(a, short=True)[:2]
    lines = [
        f"🔔 Sismo PA {t} (automático)",
        last_line,
        f"Desde o principal (M{fmt_mag(a.main.mag)}, {hhmm(a.main.time)}): {a.cum5} réplicas M5+, {a.cum6} M6+. "
        f"Tendência: {a.trend.lower()}. {big}".strip(),
        f"Tsunami: {tsu}. P(M6+ em 7 dias): {pct(a.probs.get('p6_7d'))}.",
        "Agora: " + " ".join(top),
        "Próxima atualização em 1 h.",
    ]
    msg = "\n".join(lines)
    return msg if len(msg) <= 900 else msg[:880] + "…"


def email_report(a: Analysis) -> tuple[str, str]:
    t = local(a.now)
    head = (f"Sismo PA {t:%H:%M}: {a.n45} réplicas M4,5+ na última hora"
            + (f", maior M{fmt_mag(a.biggest_last.mag)} às {hhmm(a.biggest_last.time)}" if a.biggest_last else "")
            + f" — tendência {a.trend.lower()}, tsunami {'ALERTA ATIVO' if a.tsunami.get('active') else 'sem alerta'}.")
    subject = f"[Sismo PA] {t:%d/%m %H:%M} — {a.n45} M4,5+ na última hora, tendência {a.trend.lower()}"
    L: list[str] = [head, "", f"SISMO PANAMÁ — relatório das {t:%H:%M} ({t:%d/%m/%Y}), gerado pelo GitHub Actions"]
    L.append(f"Sismo principal: M{fmt_mag(a.main.mag)} em {local(a.main.time):%d/%m %H:%M}, {a.main.place_short}, "
             f"prof. {a.main.depth:.0f} km — há {str(round(a.hours_since, 1)).replace('.', ',')} h.")
    L.append("")
    if a.biggest_last:
        b = a.biggest_last
        L.append(f"ÚLTIMA HORA: {a.n35} eventos M3,5+, {a.n45} M4,5+. Maior: M{fmt_mag(b.mag)} às {hhmm(b.time)}, "
                 f"{b.place_short}, prof. {b.depth:.0f} km.")
    else:
        L.append("ÚLTIMA HORA: nenhum evento M3,5+ no catálogo USGS.")
    if a.biggest_after:
        L.append(f"ACUMULADO desde o principal: {a.cum45} M4,5+ | {a.cum5} M5+ | {a.cum6} M6+. "
                 f"Maior réplica: M{fmt_mag(a.biggest_after.mag)} às {hhmm(a.biggest_after.time)}, {a.biggest_after.place_short}.")
    L.append(f"TENDÊNCIA: {a.trend} — {a.n45} M4,5+ nesta hora vs {a.n45_prev} na hora anterior"
             + ("; ATENÇÃO: houve M6+ nas últimas 3 h." if a.m6_recent else "."))
    p = a.probs
    n5 = f" (~{p['n5_1d']:.0f} esperadas)" if p.get("n5_1d") else ""
    L.append(f"PREVISÃO [{p.get('label')}]: P(M5+ em 24 h) {pct(p.get('p5_1d'))}{n5} / "
             f"P(M6+ em 7 d) {pct(p.get('p6_7d'))} / P(M7+ em 7 d) {pct(p.get('p7_7d'))}.")
    L.append("")
    L.append("OFICIAL:")
    if a.tsunami.get("ok"):
        if a.tsunami["lines"]:
            L.append("  Tsunami (tsunami.gov): " + ("ALERTA ATIVO" if a.tsunami["active"] else "sem alerta ativo"))
            for ln in a.tsunami["lines"][:4]:
                L.append("    - " + ln)
        else:
            L.append("  Tsunami (tsunami.gov): nenhum boletim para Panamá/Pacífico nas últimas 24 h.")
    else:
        L.append("  Tsunami: feed do tsunami.gov indisponível nesta rodada.")
    if a.news:
        L.append("  Imprensa (Google News, últimas 12 h — manchetes, não verificadas):")
        for ln in a.news:
            L.append("    - " + ln)
    else:
        L.append("  Imprensa: nenhuma manchete recente encontrada (ou feed indisponível).")
    L.append("")
    L.append("AÇÕES AGORA:")
    for i, act in enumerate(actions_for(a), 1):
        L.append(f"  {i}. {act}")
    L.append("")
    L.append("ÚLTIMA HORA — lista (hora local, M, local, prof.):")
    if a.last:
        for q in a.last[:15]:
            L.append(f"  {hhmm(q.time)}  M{fmt_mag(q.mag)}  {q.place_short}  {q.depth:.0f} km")
    else:
        L.append("  (nenhum)")
    L.append("")
    L.append("MENSAGEM ENVIADA AO WHATSAPP:")
    L.append(family_message(a))
    L.append("")
    L.append("FONTES:")
    L.append("  https://earthquake.usgs.gov/earthquakes/eventpage/" + a.main.id)
    L.append("  https://earthquake.usgs.gov/fdsnws/event/1/ (catálogo, caixa Panamá)")
    L.append("  https://www.tsunami.gov/")
    L.append("  https://news.google.com/ (busca: sismo Panamá / Sinaproc)")
    if a.quiet:
        L.append("")
        L.append("Sugestão: já passaram mais de 72 h sem réplicas relevantes — reduzir a cadência para a cada 6 h "
                 "(editar o cron em .github/workflows/sismo.yml).")
    warnings = [x for x in LOG if x.startswith("[")]
    if warnings:
        L.append("")
        L.append("Avisos desta rodada: " + " | ".join(warnings[-6:]))
    return subject, "\n".join(L)


# --------------------------------------------------------------------------- Envio

def send_email(subject: str, body: str) -> str:
    user = os.environ.get("GMAIL_USER")
    pwd = os.environ.get("GMAIL_APP_PASSWORD")
    to = [x.strip() for x in (os.environ.get("MAIL_TO") or user or "").split(",") if x.strip()]
    if not (user and pwd and to):
        return "e-mail: pulado (GMAIL_USER/GMAIL_APP_PASSWORD/MAIL_TO ausentes)"
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
    return f"e-mail: enviado para {', '.join(to)}"


def send_callmebot(text: str) -> str:
    spec = os.environ.get("CALLMEBOT_RECIPIENTS", "").strip()
    if not spec:
        return "whatsapp: pulado (CALLMEBOT_RECIPIENTS ausente)"
    results = []
    for item in [x.strip() for x in spec.replace(",", ";").split(";") if x.strip()]:
        if ":" not in item:
            results.append(f"{item[:6]}…: formato inválido (use +507xxxx:APIKEY)")
            continue
        phone, key = item.split(":", 1)
        url = "https://api.callmebot.com/whatsapp.php?" + urllib.parse.urlencode(
            {"phone": phone.strip(), "text": text, "apikey": key.strip()}
        )
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
            results.append(f"{masked}: FALHOU {exc}")
    return "whatsapp: " + " | ".join(results)


def send_telegram(text: str) -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return "telegram: pulado"
    if DRY_RUN:
        return "telegram: DRY_RUN"
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    try:
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data,
                                     headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return f"telegram: HTTP {resp.status}"
    except Exception as exc:  # noqa: BLE001
        return f"telegram: FALHOU {exc}"


# --------------------------------------------------------------------------- Main

def main() -> int:
    now = datetime.now(timezone.utc)
    if FIXTURES_DIR and os.environ.get("FAKE_NOW"):
        now = datetime.fromisoformat(os.environ["FAKE_NOW"]).astimezone(timezone.utc)
    log(f"Rodada {local(now):%d/%m/%Y %H:%M} (Panamá) / {now:%H:%M} UTC")

    main_q = find_mainshock(now)
    if not main_q:
        msg = "Nenhum sismo M6,5+ na região nos últimos 30 dias e o USGS respondeu — nada a reportar."
        log(msg)
        if not DRY_RUN and os.environ.get("CALLMEBOT_RECIPIENTS"):
            pass  # silêncio proposital: sem evento, sem mensagem
        return 0
    log(f"Principal: {main_q.id} M{main_q.mag} {main_q.time:%Y-%m-%d %H:%M} UTC {main_q.place}")

    last = usgs_query("usgs_last.json", starttime=iso(now - timedelta(minutes=65)), endtime=iso(now), minmagnitude=3.0)
    prev = usgs_query("usgs_prev.json", starttime=iso(now - timedelta(minutes=125)),
                      endtime=iso(now - timedelta(minutes=65)), minmagnitude=3.0)
    cum = usgs_query("usgs_cum.json", starttime=iso(main_q.time), endtime=iso(now), minmagnitude=4.5, limit=2000)

    fc = fetch_oaf(main_q.id)
    probs = oaf_probabilities(fc)
    if probs.get("p6_7d") is None:
        probs = generic_probabilities(main_q.mag, (now - main_q.time).total_seconds() / 86400)

    tsunami = fetch_tsunami(now)
    news = fetch_news(now)

    a = analyse(now, main_q, last, prev, cum, probs, tsunami, news)
    subject, body = email_report(a)
    wa = family_message(a)

    print("\n" + "=" * 78)
    print(body)
    print("=" * 78 + "\n")

    # Em modo "hora tranquila" (72 h+ sem M4,5+), o WhatsApp sai só às 00/06/12/18 h; o e-mail segue de hora em hora.
    if a.quiet and local(now).hour % 6 != 0:
        status = [send_email(subject, body), "whatsapp: pulado (hora tranquila — só às 00/06/12/18 h)", send_telegram(wa)]
    else:
        status = [send_email(subject, body), send_callmebot(wa), send_telegram(wa)]
    for s in status:
        print(s)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(f"## {subject}\n\n```\n{body}\n```\n\n" + "\n".join(f"- {s}" for s in status) + "\n")

    failed = [s for s in status if "FALHOU" in s or "Traceback" in s]
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"ERRO FATAL: {exc}")
        sys.exit(2)
