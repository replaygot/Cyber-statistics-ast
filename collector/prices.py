"""Цены всех тарифов и пакетов клуба по зонам — из сервиса бронирования Langame.

Делает то же, что страница бронирования, когда гость выбирает место и тариф: спрашивает список
тарифов на нужное время и считает стоимость для одного места каждой зоны. Ничего не бронирует
и не оплачивает. Между запросами пауза (REQUEST_PAUSE_S в collect.py).

Запуск:
  CLUB_HASH=<код> python3 collector/prices.py   — цены клуба в data/<код>/prices.json и PRICES.md
  python3 collector/prices.py --summary         — общая таблица data/PRICES.md по всем клубам
"""
import json
import sys
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402
import collect  # noqa: E402

api = collect.api
FMT = "%Y-%m-%d %H:%M"
DAY_NAMES = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
HOURLY_AT = (11, 19, 23)  # когда считать почасовой тариф: день, вечер, ночь
TRIES_PER_ZONE = 3


def next_weekday(today: datetime, weekday: int) -> datetime:
    days = (weekday - today.weekday()) % 7 or 7
    return (today + timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)


def money(x) -> str:
    return "—" if x is None else f"{x:,.0f}".replace(",", " ")


def main() -> int:
    club = collect.CLUB_HASH
    token = api("GET", "/api/initialize")["data"]["token"]
    st = api("GET", "/api/server_time", token)["data"]
    now = datetime(int(st["full_year"]), int(st["month"]), int(st["day"]), int(st["hour"]), int(st["minute"]))
    config = api("GET", "/api/config", token)["data"]
    on_map = build.seat_uuids(api("GET", "/api/seats/scheme/full", token))
    days = [next_weekday(now, 0), next_weekday(now, 5)]  # будний день и выходной

    # 1. Какие тарифы и пакеты есть и когда их можно взять: список на каждый час двух дней
    by_start, tariffs = {}, {}
    for day in days:
        for hour in range(24):
            start = day.replace(hour=hour).strftime(FMT)
            resp = api("GET", "/api/tariff", token, query="?start_at=" + urllib.parse.quote(start))
            items = build.as_list((resp.get("data") or {}).get("tariffs"))
            by_start[start] = items
            for t in items:
                tariffs.setdefault(t["id"], {**t, "available_from": []})["available_from"].append(start)

    # 2. Места по зонам (порядок — по номеру места)
    probe = days[0].replace(hour=11)
    seats = build.as_list((api("POST", "/api/booking/seats/calculator", token, {
        "start_at": probe.strftime(FMT), "stop_at": (probe + timedelta(hours=1)).strftime(FMT)}).get("data") or {}).get("seats"))
    zones = {}
    for s in sorted(seats, key=lambda s: build.num(s.get("name"))):
        if on_map and s.get("device_uuid") not in on_map:
            continue
        zones.setdefault(s.get("zone_name") or "?", []).append(s)
    for z in zones.values():
        z.sort(key=lambda s: bool(s.get("occupied")))  # сначала свободные

    def calc(path: str, body: dict, zone: str) -> dict:
        last = {"error": "нет мест в зоне"}
        for s in zones[zone][:TRIES_PER_ZONE]:
            try:
                d = (api("POST", path, token, {**body, "device_uuids": [s["device_uuid"]]}).get("data") or {})
            except RuntimeError as e:
                last = {"seat": s.get("name"), "error": str(e)[:300]}
                continue
            if d.get("not_reserved") or d.get("not_reserved_errors"):
                last = {"seat": s.get("name"), "error": "место занято на это время"}
                continue
            fee = (d.get("write_off_data") or {}).get("service_fee") or 0
            return {"seat": s.get("name"), "sum": d.get("sum"), "service_fee": fee,
                    "price": (d.get("sum") or 0) - fee, "sum_without_discount": d.get("sum_without_discount"),
                    "discount_percent": d.get("discount_percent"), "name": d.get("global_name"),
                    "start_at": d.get("start_at"), "stop_at": d.get("stop_at")}
        return last

    # 3. Почасовой тариф: 1 час днём, вечером и ночью, в будни и в выходной, по каждой зоне
    hourly = []
    for day in days:
        for hour in HOURLY_AT:
            start = day.replace(hour=hour)
            body = {"start_at": start.strftime(FMT), "stop_at": (start + timedelta(hours=1)).strftime(FMT)}
            for zone in zones:
                hourly.append({"start": body["start_at"], "zone": zone,
                               **calc("/api/booking/calculator/calculate", body, zone)})

    # 4. Пакеты: в первый час, когда пакет доступен (сначала в будни, потом в выходной), по каждой зоне
    packets = []
    for t in tariffs.values():
        if t.get("type") != "packet":
            continue
        for day in days:
            starts = [s for s in t["available_from"] if s.startswith(day.strftime("%Y-%m-%d"))]
            if not starts:
                continue
            for zone in zones:
                packets.append({"tariff_id": t["id"], "tariff_name": t.get("tariff_name"), "start": starts[0],
                                "minutes": next((x.get("minutes_duration") for x in by_start[starts[0]]
                                                 if x["id"] == t["id"]), None), "zone": zone,
                                **calc("/api/booking/packet/calculate",
                                       {"start_at": starts[0], "tariff_id": t["id"]}, zone)})

    out = {"club": club, "club_name": (config.get("club_info") or {}).get("name") or club,
           "fetched_at": now.strftime(FMT), "days": [d.strftime("%Y-%m-%d") for d in days],
           "zones": {z: len(v) for z, v in zones.items()}, "booking_rules": config.get("booking_right"),
           "tariffs": list(tariffs.values()), "tariffs_by_start": by_start, "hourly": hourly, "packets": packets}
    base = build.ROOT / "data" / club
    base.mkdir(parents=True, exist_ok=True)
    (base / "prices.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    (base / "PRICES.md").write_text(report(out), encoding="utf-8")
    errors = [x for x in hourly + packets if "error" in x]
    print(f"{out['club_name']}: зон {len(zones)}, тарифов {len(tariffs)}, цен {len(hourly) + len(packets) - len(errors)}, "
          f"ошибок {len(errors)}")
    return 0


def windows(t: dict) -> str:
    """Когда пакет можно взять: часы старта по дням (по спискам тарифов на каждый час)."""
    parts = []
    for day in sorted({s[:10] for s in t["available_from"]}):
        hours = sorted(int(s[11:13]) for s in t["available_from"] if s.startswith(day))
        ranges, first = [], hours[0]
        for a, b in zip(hours, hours[1:] + [None]):
            if b != a + 1:
                ranges.append(f"{first:02d}" if first == a else f"{first:02d}–{a:02d}")
                first = b
        wd = DAY_NAMES[datetime.strptime(day, "%Y-%m-%d").weekday()]
        parts.append(f"{wd} {', '.join(ranges)} ч")
    return "; ".join(parts)


def report(p: dict) -> str:
    zones = list(p["zones"])
    out = [f"# Цены {p['club_name']}", "",
           f"Из сервиса бронирования Langame, {p['fetched_at']}. Цена — за одно место, как её считает страница брони "
           f"(дни: {', '.join(p['days'])}). Мест по зонам: "
           + ", ".join(f"{z} — {n}" for z, n in p["zones"].items()) + ".", ""]
    fees = {x.get("service_fee") for x in p["hourly"] + p["packets"] if x.get("service_fee")}
    if fees:
        out += [f"В цену онлайн-брони входит сервисный сбор: {', '.join(money(f) for f in sorted(fees))} ₸; "
                "в таблицах — цена без сбора.", ""]
    out += ["## Почасовой тариф, 1 час", "", "| Время | " + " | ".join(zones) + " |", "|---" * (len(zones) + 1) + "|"]
    for start in sorted({x["start"] for x in p["hourly"]}):
        wd = DAY_NAMES[datetime.strptime(start[:10], "%Y-%m-%d").weekday()]
        cells = []
        for z in zones:
            x = next((x for x in p["hourly"] if x["start"] == start and x["zone"] == z), {})
            cells.append(money(x.get("price")) if "error" not in x else "нет данных")
        out.append(f"| {wd} {start[11:]} | " + " | ".join(cells) + " |")
    out += ["", "## Пакеты", "", "| Пакет | Когда можно взять | Старт в расчёте | Длит. | " + " | ".join(zones) + " |",
            "|---" * (len(zones) + 4) + "|"]
    for t in sorted((t for t in p["tariffs"] if t.get("type") == "packet"), key=lambda t: t.get("tariff_name") or ""):
        for start in sorted({x["start"] for x in p["packets"] if x["tariff_id"] == t["id"]}):
            rows = [x for x in p["packets"] if x["tariff_id"] == t["id"] and x["start"] == start]
            mins = rows[0].get("minutes") if rows else None
            wd = DAY_NAMES[datetime.strptime(start[:10], "%Y-%m-%d").weekday()]
            cells = []
            for z in zones:
                x = next((x for x in rows if x["zone"] == z), {})
                cells.append(money(x.get("price")) if "error" not in x else "нет данных")
            out.append(f"| {t.get('tariff_name')} | {windows(t)} | {wd} {start[11:]} | "
                       f"{'' if not mins else f'{mins / 60:.2g} ч'} | " + " | ".join(cells) + " |")
    basic = [t for t in p["tariffs"] if t.get("type") != "packet"]
    if basic:
        out += ["", "Почасовой тариф в списке: " + "; ".join(
            f"{t.get('tariff_name')} — {money(t.get('default_price'))} ₸ за {t.get('minutes_default_duration') or 60} мин"
            for t in basic) + "."]
    if p.get("booking_rules"):
        out += ["", "## Правила брони клуба", "", p["booking_rules"].strip()]
    return "\n".join(out) + "\n"


def summary() -> None:
    clubs = []
    for f in sorted((build.ROOT / "data").glob("*/prices.json")):
        clubs.append(json.loads(f.read_text(encoding="utf-8")))
    out = ["# Цены клубов", "", "Почасовой тариф (1 час, без сервисного сбора) и пакеты по зонам. Подробно — "
           "в PRICES.md каждого клуба.", ""]
    for p in clubs:
        out += [f"## [{p['club_name']}]({p['club']}/PRICES.md)", ""]
        for z in p["zones"]:
            hp = sorted({x["price"] for x in p["hourly"] if x["zone"] == z and "error" not in x})
            pk = sorted({(x["tariff_name"], x["price"]) for x in p["packets"] if x["zone"] == z and "error" not in x})
            line = f"- **{z}** (мест: {p['zones'][z]}): час " + ("–".join(money(v) for v in (hp[0], hp[-1])) if len(hp) > 1
                                                                else money(hp[0]) if hp else "—") + " ₸"
            if pk:
                line += "; пакеты: " + ", ".join(f"{n} — {money(v)} ₸" for n, v in pk)
            out.append(line)
        out.append("")
    (build.ROOT / "data" / "PRICES.md").write_text("\n".join(out), encoding="utf-8")


if __name__ == "__main__":
    if "--summary" in sys.argv:
        summary()
    else:
        sys.exit(main())
