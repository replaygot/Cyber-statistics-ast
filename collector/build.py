"""Пересобирает occupancy.csv и SUMMARY.md из всех сохранённых ответов API (data/<клуб>/raw).

Как сервис бронирования Langame показывает места (проверено, см. README):
- статус места дан не на «сейчас», а на окно брони — ближайший час, начиная через 15–30 минут;
- «занято» в этом окне — это либо идущая сессия, либо бронь, которая начнётся в ближайшие ~1,5 часа;
- «занято до» = конец сессии или брони + 30 минут.
Поэтому каждое место раскладывается по времени замера и броням из ответа:
  playing — идёт сессия (кто-то играет);
  paid    — идёт оплаченная бронь, но сессию по ней ещё не начали (место продано, за ПК никого);
  soon    — место свободно, но придержано под бронь, которая ещё не началась;
  free    — свободно; unavailable — выключено или на обслуживании.
Замер относится к часу, в котором он сделан (замер в 15:35 — час 15:00–16:00). Если в один час
попало несколько замеров, берётся последний. Так любые правки разбора применяются ко всей истории.
Запуск: python3 collector/build.py [код_клуба]
"""
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
ALL = "ВСЕ"
KINDS = ("playing", "paid", "soon", "free", "unavailable")
# Сервис показывает место занятым ещё некоторое время после конца сессии или брони (у LORAN — 30 минут;
# у других клубов настройка может отличаться, поэтому точное значение в разборе не используется).
HOLD_MAX = timedelta(minutes=90)
ENDED_BUFFER_MAX = timedelta(minutes=32)


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


def when(value):
    try:
        return datetime.fromisoformat(value).replace(tzinfo=None) if value else None
    except (TypeError, ValueError):
        return None


def bookings(seat: dict) -> list:
    out = []
    for b in as_list(seat.get("nearest_bookings")):
        start, stop = when(b.get("start_at")), when(b.get("stop_at"))
        if start and stop:
            out.append((start, stop))
    return out


def classify(seat: dict, now: datetime) -> str:
    if seat.get("seat_state") == "UNAVAILABLE":
        return "unavailable"
    books = bookings(seat)
    if any(start <= now < stop for start, stop in books):
        # Бронь идёт, а в списке она есть только пока гость не запустил по ней сессию кодом брони
        return "paid"
    if seat.get("seat_state") == "BUSY":
        busy_until = when(seat.get("stop_at"))
        for start, stop in books if busy_until else []:
            if start > now and stop <= busy_until <= stop + HOLD_MAX:
                # «Занято до» накрывает будущую бронь целиком: сессия гостя без брони не может идти поверх
                # чужой брони, значит место придержано под бронь (у LORAN «занято до» = конец брони + 30 мин)
                return "soon"
            if stop <= now and stop <= busy_until <= stop + ENDED_BUFFER_MAX:
                return "free"  # бронь уже кончилась, идёт запас после неё
        return "playing"
    return "soon" if seat.get("occupied") and any(start > now for start, _ in books) else "free"


def parse(raw: dict, on_map: set) -> tuple[datetime, dict]:
    """Время замера (время клуба) и номера мест по зонам и категориям (см. KINDS)."""
    measured = datetime.fromisoformat(raw["server_now"]).replace(tzinfo=None)
    zones: dict[str, dict] = defaultdict(lambda: {"seats": 0, **{k: [] for k in KINDS}})
    for s in as_list((raw["seats_response"].get("data") or {}).get("seats")):
        if on_map and s.get("device_uuid") not in on_map:
            continue
        z = zones[s.get("zone_name") or "?"]
        z["seats"] += 1
        z[classify(s, measured)].append(str(s.get("name")))
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
            occ = len(z["playing"]) + len(z["paid"])
            seats = lambda k: " ".join(sorted(z[k], key=num))  # noqa: E731
            rows.append({
                "measured_at": measured.strftime("%Y-%m-%d %H:%M"), "date": measured.strftime("%Y-%m-%d"),
                "weekday": WEEKDAYS[measured.weekday()], "hour": measured.hour, "zone": zone, "seats": z["seats"],
                "occupied": occ, "occupied_pct": round(100 * occ / z["seats"]) if z["seats"] else "",
                "playing": len(z["playing"]), "paid_not_playing": len(z["paid"]),
                "booked_soon": len(z["soon"]), "free": len(z["free"]), "unavailable": len(z["unavailable"]),
                "playing_seats": seats("playing"), "paid_not_playing_seats": seats("paid"),
                "booked_soon_seats": seats("soon"), "unavailable_seats": seats("unavailable"),
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
        "или идёт оплаченная бронь, по которой сессию ещё не начали (в Langame бронь оплачивается заранее, "
        "время продано, даже если гость не пришёл). Места, придержанные под бронь, которая ещё не началась, "
        "занятыми не считаются. Замер раз в час, около :35 по времени клуба (UTC+5): строка «15:00» — "
        "замер между 15:00 и 15:59. Пусто — замера не было.",
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
    paid = [r for r in total if r["paid_not_playing"]]
    if paid:
        out += ["", f"Замеров, где бронь уже шла, а за ПК никого не было: {len(paid)} из {len(total)} "
                f"(всего {sum(r['paid_not_playing'] for r in paid)} мест). Такие места считаются занятыми."]
    unavailable = [r for r in total if r["unavailable"]]
    if unavailable:
        out += ["", f"Замеров, где были места со статусом «недоступно» (выключены или на обслуживании): "
                f"{len(unavailable)} из {len(total)}. Занятыми они не считаются, а процент загрузки считается "
                "от всех мест зала, включая выключенные."]
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "1jelye8o")
