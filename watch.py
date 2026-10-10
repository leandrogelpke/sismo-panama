#!/usr/bin/env python3
"""
Vigía de alertas críticas — corre cada 10 minutos dentro de un job largo de GitHub Actions.

Criterios de alerta (cualquiera):
  - réplica M≥ALERT_MIN_MAG (5.5) en la caja Panamá en los últimos 30 min, no alertada antes
  - sismo M≥NEAR_MIN_MAG (4.8) a ≤NEAR_KM (120 km) de Ciudad de Panamá, no alertado antes
  - boletín de tsunami activo (PTWC/NTWC) no alertado antes
Envía solo un mensaje corto por Telegram (sin PDF) y guarda lo alertado en reports/alerts_sent.json
para no repetir. Reutiliza las funciones de report.py.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import report as R

ALERT_MIN_MAG = float(os.environ.get("ALERT_MIN_MAG", "5.5"))
NEAR_MIN_MAG = float(os.environ.get("NEAR_MIN_MAG", "4.8"))
NEAR_KM = float(os.environ.get("NEAR_KM", "120"))
PANAMA_CITY = (8.98, -79.52)
LOOKBACK_MIN = int(os.environ.get("LOOKBACK_MIN", "30"))
STATE = os.path.join(os.environ.get("REPORTS_DIR", "reports"), "alerts_sent.json")


def km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p = math.pi / 180
    a = 0.5 - math.cos((lat2 - lat1) * p) / 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * (1 - math.cos((lon2 - lon1) * p)) / 2
    return 12742 * math.asin(math.sqrt(a))


def load_state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as fh:
            d = json.load(fh)
            d.setdefault("events", {}); d.setdefault("tsunami", [])
            return d
    except (OSError, json.JSONDecodeError):
        return {"events": {}, "tsunami": []}


def save_state(d: dict) -> None:
    os.makedirs(os.path.dirname(STATE) or ".", exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=1)


def git_commit_state(msg: str, paths: list[str] | None = None) -> None:
    if R.DRY_RUN or not os.environ.get("GITHUB_ACTIONS"):
        return
    cmds = [
        ["git", "config", "user.name", "sismo-bot"],
        ["git", "config", "user.email", "actions@users.noreply.github.com"],
        ["git", "add", *(paths or [STATE])],
        ["git", "commit", "-m", msg],
        ["git", "pull", "--rebase", "origin", "main"],
        ["git", "push"],
    ]
    for c in cmds:
        r = subprocess.run(c, capture_output=True, text=True)
        if r.returncode != 0 and c[1] not in ("commit",):
            print(f"[git] {' '.join(c[:2])}: {r.stderr.strip()[:200]}")


def main() -> int:
    now = datetime.now(timezone.utc)
    print(f"vigía {R.local(now):%d/%m %H:%M} (Panamá)")
    # sincroniza el estado con lo que otros jobs hayan commiteado
    if os.environ.get("GITHUB_ACTIONS") and not R.DRY_RUN:
        subprocess.run(["git", "pull", "-q", "--rebase", "origin", "main"], capture_output=True)
    state = load_state()

    quakes = R.usgs_query("watch_usgs.json", starttime=R.iso(now - timedelta(minutes=LOOKBACK_MIN)), endtime=R.iso(now),
                          minmagnitude=min(NEAR_MIN_MAG, ALERT_MIN_MAG), limit=200)
    tsunami = R.fetch_tsunami(now)

    alerts: list[str] = []
    new_ids: dict[str, str] = {}
    for q in quakes:
        if q.id in state["events"]:
            continue
        d = km(q.lat, q.lon, *PANAMA_CITY) if not math.isnan(q.lat) else float("nan")
        near = (not math.isnan(d)) and d <= NEAR_KM and q.mag >= NEAR_MIN_MAG
        big = q.mag >= ALERT_MIN_MAG
        if big or near:
            dist = f", a {d:.0f} km de Ciudad de Panamá" if not math.isnan(d) else ""
            alerts.append(f"Sismo M{R.fmt_mag(q.mag)} a las {R.hhmm(q.time)}, {q.place_short}, prof. {q.depth:.0f} km{dist}. "
                          f"Fuente USGS: {R.usgs_event_url(q.id)}")
            new_ids[q.id] = now.isoformat()
    tsu_new = []
    if tsunami.get("active"):
        for line, url in tsunami.get("lines", [])[:1]:
            if url not in state["tsunami"]:
                alerts.append(f"TSUNAMI — boletín activo: {line}. Alejarse de la costa. Fuente: {url}")
                tsu_new.append(url)

    # Informe regular: si pasaron ≥ SEND_EVERY_HOURS desde el último envío, lo genera aquí mismo
    # (no depende del cron de GitHub, que se salta horas).
    if os.environ.get("WATCH_RUNS_REPORT", "1") == "1":
        sent_path = os.path.join(os.environ.get("REPORTS_DIR", "reports"), "last_sent.json")
        try:
            with open(sent_path, encoding="utf-8") as fh:
                last_at = datetime.fromisoformat(json.load(fh)["at"])
            elapsed_h = (now - last_at).total_seconds() / 3600
        except Exception:  # noqa: BLE001
            elapsed_h = 1e9
        if elapsed_h >= R.SEND_EVERY_HOURS - 10 / 60:
            print(f"informe regular vencido ({elapsed_h:.1f} h) — generando")
            r = subprocess.run([sys.executable, "report.py"], capture_output=True, text=True)
            tail = "\n".join(r.stdout.splitlines()[-6:])
            print(tail)
            git_commit_state(f"informe {R.local(now):%d/%m %H:%M} (Panamá) desde el vigía",
                             paths=[os.environ.get("REPORTS_DIR", "reports")])

    if not alerts:
        print("sin alerta crítica")
        return 0

    text = (f"⚠️ ALERTA CRÍTICA sismo PA {R.hhmm(now)}\n" + "\n".join("• " + a for a in alerts) +
            "\nQué hacer: si tiembla fuerte, proteger la cabeza, alejarse de vidrios y esperar a que pase antes de bajar por escaleras "
            "(nunca ascensor). Emergencias: 911 · Sinaproc 520-4426/27/29 · WhatsApp Sinaproc 6998-4809.\n"
            "El informe completo sale en el próximo corte regular.")
    print(text)
    print(R.send_telegram(text))
    print(R.send_callmebot(text))
    state["events"].update(new_ids)
    state["tsunami"].extend(tsu_new)
    save_state(state)
    git_commit_state(f"alerta crítica {R.local(now):%d/%m %H:%M} (Panamá)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"ERROR vigía: {exc}")
        sys.exit(0)  # nunca tumbar el bucle
