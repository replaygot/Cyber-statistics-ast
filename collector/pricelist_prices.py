"""Цены клуба из прайса-картинки, когда сервис брони их не отдал: data/<код>/pricelist.json -> prices.json.

Запуск: python3 collector/pricelist_prices.py <код> [<код> ...]

prices.json пишется в том же виде, что у collector/prices.py (почасовой и пакеты по зонам, будни/выходные),
чтобы collector/revenue.py считал выручку одинаково для всех клубов. Цены берутся только из pricelist.json:
- "zones": зона сервиса брони -> колонка прайса;
- "windows": пакет с окном -> "ЧЧ:ММ-ЧЧ:ММ" или список таких окон (например «Топ день»: старт 10–12, максимум 6 ч);
- "prices": {"все дни" | "пн-чт" | "пт-вс" | ...: {колонка: {тариф: цена}}};
  тарифы: «1 час», «3 часа» / «4 часа» / «5 часов» (или «2+1», «3+2») и названия окон из "windows".
"""
import json
import sys
from datetime import datetime

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import build  # noqa: E402
import prices as service  # noqa: E402

WEEKDAY, WEEKEND = "2026-11-16", "2026-11-14"  # понедельник и суббота: так же, как в prices.json из сервиса
DURATION = {"3 часа": 180, "4 часа": 240, "5 часов": 300, "2+1": 180, "3+2": 300}


def day_keys(prices: dict) -> dict:
    """Ключ прайса для будней и для выходных."""
    keys = list(prices)
    if len(keys) == 1:
        return {WEEKDAY: keys[0], WEEKEND: keys[0]}
    wd = next(k for k in keys if k.startswith("пн"))
    we = next(k for k in keys if not k.startswith("пн"))
    return {WEEKDAY: wd, WEEKEND: we}


def hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def main(club: str) -> None:
    base = build.ROOT / "data" / club
    pl = json.loads((base / "pricelist.json").read_text(encoding="utf-8"))
    occ = (base / "occupancy.csv").read_text(encoding="utf-8").splitlines()
    head = occ[0].split(",")
    rows = [dict(zip(head, r.split(","))) for r in occ[1:]]
    last = rows[-1]["measured_at"]
    seats = {r["zone"]: int(r["seats"]) for r in rows if r["measured_at"] == last and r["zone"] != "ВСЕ"}
    keys = day_keys(pl["prices"])
    zones = {z: n for z, n in seats.items() if z in pl["zones"]}
    missing = sorted(set(seats) - set(zones))
    if missing:
        raise SystemExit(f"{club}: нет колонки прайса для зон {missing}")

    def price(day: str, zone: str, tariff: str):
        return pl["prices"][keys[day]][pl["zones"][zone]].get(tariff)

    tariffs, hourly, packets = [], [], []
    tid = 0
    tariffs.append({"id": 0, "tariff_name": "1 час", "type": "basic", "minutes_default_duration": 60,
                    "default_price": price(WEEKDAY, max(zones, key=zones.get), "1 час"), "minutes_duration": None,
                    "available_from": [f"{d} {h:02d}:00" for d in (WEEKDAY, WEEKEND) for h in range(24)]})
    for day in (WEEKDAY, WEEKEND):
        for zone in zones:
            for h in range(24):
                hourly.append({"start": f"{day} {h:02d}:00", "zone": zone, "price": price(day, zone, "1 час"),
                               "service_fee": 0})
    names = [n for n in pl["prices"][keys[WEEKDAY]][next(iter(pl["prices"][keys[WEEKDAY]]))] if n != "1 час"]
    for name in names:
        spans = pl["windows"].get(name)
        variants = [spans] if isinstance(spans, str) else spans or [None]
        for span in variants:
            tid += 1
            if span:
                a, b = (hhmm(x) for x in span.split("-"))
                mins = ((b[0] * 60 + b[1]) - (a[0] * 60 + a[1])) % (24 * 60)
                title = f"{name} ({a[0]:02d}:{a[1]:02d}-{b[0]:02d}:{b[1]:02d})"
                first, last_h = (a[0] - 1) % 24, (b[0] - 1) % 24
                hours, h = [], first
                while True:
                    hours.append(h)
                    if h == last_h:
                        break
                    h = (h + 1) % 24
                start_h = a[0]
            else:
                mins, title, hours, start_h = DURATION[name], name, list(range(24)), 12
            tariffs.append({"id": tid, "tariff_name": title, "type": "packet", "minutes_duration": mins,
                            "minutes_default_duration": mins, "default_price": None, "discount_percent": None,
                            "available_from": [f"{d} {h:02d}:00" for d in (WEEKDAY, WEEKEND) for h in hours]})
            for day in (WEEKDAY, WEEKEND):
                for zone in zones:
                    p = price(day, zone, name)
                    if p is None:
                        continue
                    packets.append({"tariff_id": tid, "tariff_name": title, "start": f"{day} {start_h:02d}:00",
                                    "minutes": mins, "zone": zone, "price": p, "service_fee": 0})
    out = {"club": club, "club_name": pl["club_name"], "source": "pricelist.json (прайс с картинки от пользователя)",
           "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"), "days": [WEEKDAY, WEEKEND], "zones": zones,
           "booking_rules": "", "tariffs": tariffs, "tariffs_by_start": {}, "hourly": hourly, "packets": packets}
    (base / "prices.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    md = service.report(out).replace("Из сервиса бронирования Langame", "Из прайса клуба (картинка от пользователя)")
    (base / "PRICES.md").write_text(md, encoding="utf-8")
    print(f"{pl['club_name']}: зон {len(zones)}, тарифов {len(tariffs)}, почасовых {len(hourly)}, пакетов {len(packets)}")


if __name__ == "__main__":
    for c in sys.argv[1:]:
        main(c)
    service.summary()
