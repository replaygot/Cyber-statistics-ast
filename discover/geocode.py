"""Разовое геокодирование адресов клубов из каталога (OpenStreetMap Nominatim).

Запуск: python discover/geocode.py <папка_для_результата> [файл_с_адресами.json] [имя_результата.json]
Один поток, не чаще одного запроса в 1,1 с (правила Nominatim). Берутся только клубы с ПК.
"""
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "out")
SRC = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).with_name("clubs_astana.json")
NAME = sys.argv[3] if len(sys.argv) > 3 else "clubs_geo.json"
UA = "astana-club-map/1.0 (one-off address lookup)"
BOX = "71.15,51.35,71.80,50.95"  # Астана: left, top, right, bottom
PAUSE = 1.1
_last = 0.0


def ask(q: str):
    global _last
    time.sleep(max(0.0, PAUSE - (time.monotonic() - _last)))
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({
        "q": q, "format": "jsonv2", "limit": 1, "countrycodes": "kz", "viewbox": BOX, "bounded": 1,
        "addressdetails": 1, "accept-language": "ru"})
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"error": repr(e)}
    finally:
        _last = time.monotonic()


def split(address: str):
    street, _, house = address.partition(",")
    m = re.match(r"\s*(\d+[0-9A-Za-zА-Яа-я/\-]*)", house)
    return street.strip(), (m.group(1) if m else "")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    items = [c for c in json.loads(SRC.read_text(encoding="utf-8")) if c.get("address") and c.get("pcs", 1)]
    out, errors = [], 0
    for n, c in enumerate(items):
        street, house = split(c["address"])
        tries = [(f"{street} {house}, Астана", "house")] if house else []
        tries.append((f"{street}, Астана", "street"))
        res = {"id": c["id"], "name": c["name"], "address": c["address"], "lat": None, "lon": None, "precision": None}
        for q, kind in tries:
            data = ask(q)
            if isinstance(data, dict):  # ошибка запроса
                errors += 1
                res["error"] = data.get("error")
                break
            errors = 0
            if data:
                d = data[0]
                has_house = bool((d.get("address") or {}).get("house_number"))
                res.update(lat=float(d["lat"]), lon=float(d["lon"]), q=q, found=d.get("display_name"),
                           precision="house" if (kind == "house" and has_house) else "street")
                break
        out.append(res)
        if errors >= 3:
            print(f"Три ошибки подряд — останавливаюсь на {n + 1} из {len(items)}: {res.get('error')}")
            break
        if (n + 1) % 25 == 0:
            (OUT / NAME).write_text(json.dumps(out, ensure_ascii=False, indent=0), encoding="utf-8")
            print(f"{n + 1}/{len(items)}", flush=True)
    (OUT / NAME).write_text(json.dumps(out, ensure_ascii=False, indent=0), encoding="utf-8")
    ok = [r for r in out if r["lat"]]
    print(f"Адресов: {len(items)}, обработано {len(out)}, найдено {len(ok)}, "
          f"из них с домом {sum(1 for r in ok if r['precision'] == 'house')}")


if __name__ == "__main__":
    main()
