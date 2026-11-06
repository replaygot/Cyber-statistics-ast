"""Разовая проверка, как API бронирования Langame показывает места: один и тот же момент,
разные окна брони; тарифы и пакеты; ближайшие брони по каждому месту.
Только чтение: ничего не бронирует и не оплачивает.
Запуск: python3 explore/explore.py <папка для результатов>
"""
import json
import sys
import time
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))
import build  # noqa: E402
import collect  # noqa: E402

api = collect.api
FMT = "%Y-%m-%d %H:%M"


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)

    def save(name: str, data) -> None:
        (out / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    token = api("GET", "/api/initialize")["data"]["token"]
    st = api("GET", "/api/server_time", token)["data"]
    now = datetime(int(st["full_year"]), int(st["month"]), int(st["day"]), int(st["hour"]), int(st["minute"]))
    save("server_time", st)
    save("config", api("GET", "/api/config", token))
    scheme = api("GET", "/api/seats/scheme/full", token)
    on_map = build.seat_uuids(scheme)

    slot = now.replace(second=0, microsecond=0) + timedelta(minutes=15)
    slot += timedelta(minutes=(-slot.minute) % 15)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0)
    windows = {
        "a_next": slot,
        "b_plus1h": slot + timedelta(hours=1),
        "c_plus2h": slot + timedelta(hours=2),
        "d_plus3h": slot + timedelta(hours=3),
        "e_tomorrow_12": tomorrow.replace(hour=12),
        "f_tomorrow_23": tomorrow.replace(hour=23),
    }
    calc = {}
    for name, start in windows.items():
        body = {"start_at": start.strftime(FMT), "stop_at": (start + timedelta(minutes=60)).strftime(FMT)}
        calc[name] = {"window": body, "response": api("POST", "/api/booking/seats/calculator", token, body)}
        time.sleep(0.5)
    save("calculator_windows", {"server_now": now.strftime(FMT), "windows": calc})

    tariffs = {}
    for start in [slot, now.replace(hour=23, minute=0) if now.hour < 23 else slot,
                  tomorrow.replace(hour=8), tomorrow.replace(hour=12), tomorrow.replace(hour=18),
                  tomorrow.replace(hour=23)]:
        key = start.strftime(FMT)
        tariffs[key] = api("GET", "/api/tariff", token, query="?start_at=" + urllib.parse.quote(key))
        time.sleep(0.5)
    save("tariffs", tariffs)

    nearest = {}
    first = calc["a_next"]["window"]
    seats = build.as_list((calc["a_next"]["response"].get("data") or {}).get("seats"))
    for s in seats:
        if on_map and s.get("device_uuid") not in on_map:
            continue
        body = {**first, "for_nearest_start_at": first["start_at"], "device_uuid": s["device_uuid"]}
        try:
            nearest[f"{s.get('zone_name')} #{s.get('name')}"] = api("POST", "/api/booking/next-available", token, body)
        except RuntimeError as e:
            nearest[f"{s.get('zone_name')} #{s.get('name')}"] = {"error": str(e)}
        time.sleep(0.3)
    save("next_available", nearest)
    print("готово:", sorted(p.name for p in out.iterdir()))


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "out"))
