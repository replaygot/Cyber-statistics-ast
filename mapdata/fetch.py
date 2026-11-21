"""Разовый сбор данных для карты клубов Астаны (только чтение).

1. club_info (адрес и координаты) 22 клубов с онлайн-бронью — из API booking.langame.ru.
2. Адрес и координаты остальных клубов Астаны из каталога langame.ru (страница каждого клуба).
3. Подложка карты из OpenStreetMap через Overpass: дороги, вода, парки, ж/д, районы, кварталы, здания.

Результат — в папке, переданной первым аргументом.
"""
import gzip, json, re, sys, time, urllib.parse, urllib.request
from pathlib import Path

OUT = Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)
HERE = Path(__file__).resolve().parent
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
CLUBS = ("1jelye8o 1JB6NB4A 16Xp6Rly bKqXNKEG 1JB6N7pB kywedwxo krKEyOPv kgGamr9N YQlqXEaP bVJnDE0Q "
         "bLxgN4B5 1JB6Nawe YwQ7AOma YNo8l6DA kO5VxXZy 1avrAZ8r bAaJQgP9 YZPKeGgN 12O49yj2 b56KpWW7 "
         "16Xp6WAL b7g5XPAd").split()
log = open(OUT / "LOG.txt", "w", encoding="utf-8")


def say(*a):
    print(*a, flush=True); print(*a, file=log, flush=True)


def get(url, headers=None, data=None, timeout=60, tries=4):
    h = {"User-Agent": UA, **(headers or {})}
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            say(f"  ошибка {url[:90]}: {e!r}, попытка {i + 1}")
            time.sleep(5 * (i + 1))
    return None


# 1. club_info 22 клубов
info = {}
for code in CLUBS:
    base = "https://booking.langame.ru"
    hdr = {"Accept": "application/json", "Referer": f"{base}/{code}", "X-CLUB-HASH": code}
    raw = get(f"{base}/api/initialize", hdr)
    try:
        token = json.loads(raw)["data"]["token"]
        cfg = json.loads(get(f"{base}/api/config", {"Accept": "application/json", "Referer": f"{base}/{code}",
                                                     "X-AUTH-TOKEN": token}))["data"]
        info[code] = cfg.get("club_info")
        ci = info[code] or {}
        say(f"{code} {ci.get('name')} | {ci.get('full_address')} | {ci.get('latitude')},{ci.get('longitude')}")
    except Exception as e:  # noqa: BLE001
        say(f"{code}: не удалось {e!r}"); info[code] = None
    time.sleep(2)
json.dump(info, open(OUT / "clubs22.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)

# 2. каталог langame.ru: страница каждого клуба
COORD = re.compile(r"(5[01]\.\d{3,})[\"'\s,;:\]\[a-zA-Z_=]{1,40}?(7[01]\.\d{3,})|(7[01]\.\d{3,})[\"'\s,;:\]\[a-zA-Z_=]{1,40}?(5[01]\.\d{3,})")
ADDR = re.compile(r"(?:улица|проспект|ул\.|пр\.|пр-т|шоссе|бульвар|переулок|микрорайон|мкр|жилой массив|көшесі|даңғылы)[^<>\"]{2,90}")
cat = [c for c in json.load(open(HERE / "catalog_urls.json", encoding="utf-8")) if c.get("status") == 200]
out = []
for i, c in enumerate(cat):
    raw = get(c["url"], timeout=40, tries=3)
    rec = {"url": c["url"], "title": c.get("title"), "codes": c.get("codes")}
    if raw:
        h = raw.decode("utf-8", "replace")
        if i < 3:
            (OUT / f"sample_{i}.html").write_text(h, encoding="utf-8")
        pairs = []
        for m in COORD.finditer(h):
            lat, lon = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
            ctx = h[max(0, m.start() - 60):m.end() + 20].replace("\n", " ")
            pairs.append({"lat": lat, "lon": lon, "ctx": ctx})
        rec["coords"] = pairs[:6]
        rec["addr"] = list(dict.fromkeys(a.strip() for a in ADDR.findall(re.sub(r"<[^>]+>", " ", h))))[:6]
        t = re.search(r"<title>(.*?)</title>", h, re.S)
        rec["page_title"] = t.group(1).strip() if t else None
    out.append(rec)
    if i % 25 == 0:
        say(f"каталог {i + 1}/{len(cat)}: {rec.get('title')} {rec.get('coords', [None])[:1]} {rec.get('addr', [])[:1]}")
    time.sleep(1)
json.dump(out, open(OUT / "catalog.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
say(f"каталог: {len(out)} страниц, с координатами {sum(1 for r in out if r.get('coords'))}")

# 3. OpenStreetMap через Overpass
MIRRORS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
           "https://maps.mail.ru/osm/tools/overpass/api/interpreter", "https://overpass.private.coffee/api/interpreter"]
BB = "50.98,71.15,51.33,71.75"
Q = {
    "base": f"""[out:json][timeout:600];
(
 way["highway"~"^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street|pedestrian|motorway_link|trunk_link|primary_link|secondary_link|tertiary_link|service)$"]({BB});
 way["waterway"~"^(river|canal|stream)$"]({BB});
 way["natural"="water"]({BB}); relation["natural"="water"]({BB});
 way["water"]({BB}); relation["water"]({BB});
 way["leisure"~"^(park|garden|stadium)$"]({BB}); relation["leisure"~"^(park|garden)$"]({BB});
 way["landuse"~"^(forest|grass|recreation_ground|cemetery|industrial|railway)$"]({BB});
 way["natural"~"^(wood|scrub|wetland)$"]({BB});
 way["railway"="rail"]({BB});
 relation["boundary"="administrative"]["admin_level"~"^(4|6|7|8|9)$"]({BB});
 node["place"~"^(suburb|neighbourhood|quarter|city_block)$"]({BB});
 way["place"~"^(suburb|neighbourhood|quarter)$"]({BB});
);
out geom;""",
    "buildings": f"""[out:json][timeout:600];
(way["building"]({BB}););
out geom;""",
}
for name, q in Q.items():
    for url in MIRRORS:
        say(f"overpass {name}: {url}")
        raw = get(url, {"Content-Type": "application/x-www-form-urlencoded"},
                  data=urllib.parse.urlencode({"data": q}).encode(), timeout=900, tries=2)
        if raw and raw.lstrip().startswith(b"{"):
            try:
                n = len(json.loads(raw)["elements"])
            except Exception:  # noqa: BLE001
                n = -1
            if n > 0:
                gzip.open(OUT / f"osm_{name}.json.gz", "wb").write(raw)
                say(f"  {name}: {n} объектов, {len(raw) / 1e6:.1f} МБ")
                break
        say(f"  {name}: не получилось с {url}: {raw[:200] if raw else None}")
say("готово")
