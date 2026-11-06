"""Пересобирает occupancy.csv и SUMMARY.md из всех сохранённых ответов API (data/<клуб>/raw).

Каждый ответ — снимок: на каких местах в момент замера идёт игра или идёт оплаченная бронь
(место продано, даже если человек не пришёл). Замер относится к часу,
в котором он сделан (замер в 15:35 — это час 15:00–16:00). Если в один час попало
несколько замеров, берётся последний. Так любые правки в разборе сразу применяются ко всей истории.
Запуск: python3 collector/build.py [код_клуба]
"""
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
ALL = "ВСЕ"


def as_list(x):
    if x is None:
        return []
    return x if isinstance(x, list) else list(x.values())


def seat_uuids(scheme: dict) -> set:
    """UUID мест, которые нарисованы на схеме зала (без сервера и прочих устройств)."""
    items = as_list((scheme.get("data") or scheme).get("items"))
    return {i.get("UUID") for i in items if isinstance(i, dict) and i.get("scheme_type") == "seat" and i.get("UUID")}


def num(name: str):
    return (0, int(name)) if str(name).isdigit() else (1, str(name))


KINDS = ("busy", "paid", "booked", "unavailable", "free")


def booked_at(seat: dict, moment: datetime) -> bool:
    """Есть ли у места бронь, которая идёт в этот момент. В Langame бронь оплачивается
    заранее на 100%, так что это время продано, даже если человек ещё не пришёл."""
    for b in seat.get("nearest_bookings") or []:
        try:
            if datetime.fromisoformat(b["start_at"]) <= moment < datetime.fromisoformat(b["stop_at"]):
                return True
        except (KeyError, TypeError, ValueError):
            continue
    return False


def parse(raw: dict, on_map: set) -> tuple[datetime, dict]:
    """Время замера (время клуба) и места по зонам: busy — идёт игра; paid — игры нет, но время
    оплачено бронью; booked — свободно, но забронировано на ближайший час; unavailable — выключено;
    free — свободно."""
    measured = datetime.fromisoformat(raw["server_now"]).replace(tzinfo=None)
    zones: dict[str, dict] = defaultdict(lambda: {"seats": 0, **{k: [] for k in KINDS}})
    for s in as_list((raw["seats_response"].get("data") or {}).get("seats")):
        if on_map and s.get("device_uuid") not in on_map:
            continue
        z = zones[s.get("zone_name") or "?"]
        z["seats"] += 1
        state, name = s.get("seat_state"), str(s.get("name"))
        if state == "UNAVAILABLE":
            z["unavailable"].append(name)
        elif state == "BUSY":
            z["busy"].append(name)
        elif booked_at(s, measured):
            z["paid"].append(name)
        elif s.get("occupied"):
            z["booked"].append(name)
        else:
            z["free"].append(name)
    total = {"seats": 0, **{k: [] for k in KINDS}}
    for z in zones.values():
        total["seats"] += z["seats"]
        for k in KINDS:
            total[k] += z[k]
    zones = dict(zones)
    zones[ALL] = total
    return measured, zones


def build(club: str) -> None:
    base = ROOT / "data" / club
    scheme_path = base / "scheme.json"
    on_map = seat_uuids(json.loads(scheme_path.read_text(encoding="utf-8"))) if scheme_path.exists() else set()
    latest, club_name = {}, club
    for f in sorted((base / "raw").glob("*/*.json")):
        raw = json.loads(f.read_text(encoding="utf-8"))
        club_name = raw.get("club_name") or club_name
        measured, zones = parse(raw, on_map)
        key = measured.strftime("%Y-%m-%d %H")
        if key not in latest or measured >= latest[key][0]:
            latest[key] = (measured, zones)
    rows = []
    for measured, zones in latest.values():
        for zone, z in zones.items():
            occ = len(z["busy"]) + len(z["paid"])
            seats = lambda k: " ".join(sorted(z[k], key=num))  # noqa: E731
            rows.append({
                "measured_at": measured.strftime("%Y-%m-%d %H:%M"), "date": measured.strftime("%Y-%m-%d"),
                "weekday": WEEKDAYS[measured.weekday()], "hour": measured.hour, "zone": zone, "seats": z["seats"],
                "occupied": occ, "occupied_pct": round(100 * occ / z["seats"]) if z["seats"] else "",
                "playing": len(z["busy"]), "paid_not_playing": len(z["paid"]),
                "free": len(z["free"]) + len(z["booked"]), "unavailable": len(z["unavailable"]),
                "booked_next_hour": len(z["booked"]),
                "playing_seats": seats("busy"), "paid_not_playing_seats": seats("paid"),
                "booked_next_hour_seats": seats("booked"), "unavailable_seats": seats("unavailable"),
            })
    rows.sort(key=lambda r: (r["measured_at"], r["zone"] == ALL, r["zone"]))
    with (base / "occupancy.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["measured_at"])
        w.writeheader()
        w.writerows(rows)
    (base / "SUMMARY.md").write_text(summary(rows, club_name), encoding="utf-8")


def summary(rows: list, club_name: str) -> str:
    total = [r for r in rows if r["zone"] == ALL]
    zones = sorted({r["zone"] for r in rows} - {ALL})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out = [
        f"# Загрузка {club_name} по часам",
        "",
        f"Обновлено: {now}. Замеров: {len(total)}. Значение — % мест, занятых в момент замера: идёт игра "
        "или время оплачено бронью (в Langame бронь оплачивается заранее, поэтому время продано, даже если "
        "человек не пришёл). Замер раз в час, около :35 по времени клуба (UTC+5): строка «15:00» — замер "
        "между 15:00 и 15:59. Пусто — замера не было.",
        "",
    ]
    days = sorted({r["date"] for r in total})
    if not days:
        return "\n".join(out + ["Данных пока нет."]) + "\n"
    grid = {(r["date"], r["hour"]): r for r in total}
    head = "| Час | " + " | ".join(f"{WEEKDAYS[datetime.strptime(d, '%Y-%m-%d').weekday()]} {d[8:]}.{d[5:7]}" for d in days) + " |"
    out += [head, "|---" * (len(days) + 1) + "|"]
    for h in range(24):
        cells = []
        for d in days:
            r = grid.get((d, h))
            cells.append("" if r is None else f"{r['occupied_pct']}%")
        out.append(f"| {h:02d}:00 | " + " | ".join(cells) + " |")
    out += ["", "## Среднее по зонам", "", "| Зона | Мест | Средняя загрузка | Замеров |", "|---|---|---|---|"]
    for zone in zones + [ALL]:
        zr = [r for r in rows if r["zone"] == zone and r["seats"]]
        if zr:
            avg = sum(r["occupied"] / r["seats"] for r in zr) / len(zr)
            out.append(f"| {zone} | {zr[-1]['seats']} | {avg:.0%} | {len(zr)} |")
    unavailable = [r for r in total if r["unavailable"]]
    if unavailable:
        out += ["", f"Замеров, где были места со статусом «недоступно» (выключены или на обслуживании): "
                f"{len(unavailable)} из {len(total)}. Занятыми они не считаются, а процент загрузки считается "
                "от всех мест зала, включая выключенные."]
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "1jelye8o")
