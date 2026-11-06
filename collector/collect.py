"""Раз в час: берёт занятость мест клуба из API онлайн-бронирования Langame
и дописывает строку в data/occupancy.csv.

Занятым считается место, у которого есть сессия или бронь в ближайшем окне
длиной 60 минут (так же страница бронирования решает, можно ли его забронировать).

Запуск: python3 collector/collect.py
Переменные окружения:
  CLUB_HASH      — код клуба из ссылки booking.langame.ru/<код> (по умолчанию LORAN)
  COLLECT_UNTIL  — после этого момента (ISO, с часовым поясом) сбор не выполняется
"""
import csv
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = "https://booking.langame.ru"
CLUB_HASH = os.environ.get("CLUB_HASH", "1jelye8o")
COLLECT_UNTIL = os.environ.get("COLLECT_UNTIL", "")
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / CLUB_HASH
WINDOW_MIN = 60
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")


def api(method: str, path: str, token: str | None = None, body: dict | None = None, query: str = "") -> dict:
    headers = {"Accept": "application/json", "User-Agent": UA, "Referer": f"{BASE}/{CLUB_HASH}"}
    if token:
        headers["X-AUTH-TOKEN"] = token
    else:
        headers["X-CLUB-HASH"] = CLUB_HASH
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{BASE}{path}{query}", data=data, headers=headers, method=method)
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")[:2000]
            last = RuntimeError(f"{method} {path} -> HTTP {e.code}: {text}")
            if e.code < 500:
                break
        except (urllib.error.URLError, TimeoutError) as e:
            last = RuntimeError(f"{method} {path} -> {e!r}")
        time.sleep(5 * (attempt + 1))
    raise last


def as_list(x):
    if x is None:
        return []
    return x if isinstance(x, list) else list(x.values())


def main() -> int:
    now_utc = datetime.now(timezone.utc)
    if COLLECT_UNTIL and now_utc > datetime.fromisoformat(COLLECT_UNTIL):
        print(f"Сбор завершён ({COLLECT_UNTIL}), ничего не делаю")
        return 0

    token = api("GET", "/api/initialize")["data"]["token"]
    st = api("GET", "/api/server_time", token)["data"]
    sign = 1 if st["tz"].startswith("+") else -1
    hh, mm = st["tz"][1:].split(":")
    tz = timezone(sign * timedelta(hours=int(hh), minutes=int(mm)))
    server_now = datetime(int(st["full_year"]), int(st["month"]), int(st["day"]),
                          int(st["hour"]), int(st["minute"]), int(st["second"]), tzinfo=tz)

    config = api("GET", "/api/config", token)["data"]
    step = int(config.get("selection_interval_minutes") or 15)
    offset = int(config.get("minimum_minutes_for_starting_booking") or 15)

    # Ближайшее время начала, как его предлагает страница: не раньше чем через offset минут, кратно step
    earliest = server_now + timedelta(minutes=offset)
    minutes = earliest.hour * 60 + earliest.minute + (1 if earliest.second or earliest.microsecond else 0)
    slot = math.ceil(minutes / step) * step
    start = earliest.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=slot)
    stop = start + timedelta(minutes=WINDOW_MIN)
    fmt = lambda d: d.strftime("%Y-%m-%dT%H:%M:00") + st["tz"]  # noqa: E731
    window = {"start_at": fmt(start), "stop_at": fmt(stop)}

    raw = {"server_now": server_now.isoformat(), "window": window}
    if config.get("booking_calculator"):
        seats_resp = api("POST", "/api/booking/seats/calculator", token, window)
        raw["mode"] = "calculator"
    else:
        tariffs = api("GET", "/api/tariff", token, query=f"?start_at={urllib.parse.quote(window['start_at'])}")
        basic = next(t for t in as_list(tariffs["data"]["tariffs"]) if t.get("type") != "packet")
        seats_resp = api("POST", "/api/booking/seats/packet", token, {"start_at": window["start_at"], "tariff_id": basic["id"]})
        raw["mode"] = "packet"
    raw["seats_response"] = seats_resp

    DATA.mkdir(parents=True, exist_ok=True)
    scheme_path = DATA / "scheme.json"
    if not scheme_path.exists() or start.hour == 12 and start.minute < 15:
        scheme = api("GET", "/api/seats/scheme/full", token)
        scheme_path.write_text(json.dumps(scheme, ensure_ascii=False, indent=1), encoding="utf-8")
    scheme = json.loads(scheme_path.read_text(encoding="utf-8"))
    zone_of = load_zones(scheme)

    raw_dir = DATA / "raw" / start.strftime("%Y-%m-%d")
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / f"{start.strftime('%H%M')}.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    seats = as_list((seats_resp.get("data") or {}).get("seats"))
    by_zone: dict[str, dict] = {}
    for s in seats:
        uuid = s.get("device_uuid") or s.get("uuid")
        zone, name = zone_of.get(uuid, ("?", uuid))
        z = by_zone.setdefault(zone, {"total": 0, "occupied": 0, "names": []})
        z["total"] += 1
        if s.get("occupied"):
            z["occupied"] += 1
            z["names"].append(str(name))
    total = sum(z["total"] for z in by_zone.values())
    occupied = sum(z["occupied"] for z in by_zone.values())
    by_zone["ВСЕ"] = {"total": total, "occupied": occupied, "names": []}

    csv_path = DATA / "occupancy.csv"
    new = not csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["collected_utc", "window_start", "date", "weekday", "hour", "zone", "seats", "occupied", "occupied_pct", "occupied_seats"])
        for zone, z in sorted(by_zone.items(), key=lambda kv: (kv[0] == "ВСЕ", kv[0])):
            pct = round(100 * z["occupied"] / z["total"]) if z["total"] else ""
            w.writerow([now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), window["start_at"], start.strftime("%Y-%m-%d"),
                        WEEKDAYS[start.weekday()], start.hour, zone, z["total"], z["occupied"], pct, " ".join(sorted(z["names"], key=natural))])
    print(json.dumps({"window": window, "zones": {k: {kk: v for kk, v in z.items() if kk != "names"} for k, z in by_zone.items()}}, ensure_ascii=False))
    return 0


def natural(s: str):
    return (len(s), s)


def load_zones(scheme: dict) -> dict:
    """uuid места -> (зона, название места). Схема хранит места и подписи зон для разных экранов."""
    items = as_list((scheme.get("data") or scheme).get("items") if isinstance(scheme.get("data") or scheme, dict) else None)
    if not items:
        items = as_list(scheme.get("items"))
    out = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        uuid = it.get("device_uuid") or it.get("uuid")
        if not uuid:
            continue
        zone = it.get("zone_name") or it.get("group_name") or it.get("zone") or it.get("group") or "?"
        if isinstance(zone, dict):
            zone = zone.get("name") or zone.get("title") or "?"
        name = it.get("name") or it.get("title") or it.get("number") or it.get("label") or uuid
        out.setdefault(uuid, (str(zone), name))
    return out


if __name__ == "__main__":
    sys.exit(main())
