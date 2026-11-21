"""Второй проход: по странице каждого клуба каталога langame.ru — тип, услуги (сколько ПК и консолей),
залы с числом ПК, адрес, координаты. Плюс повтор club_info для клубов, где первый проход не удался."""
import json, re, sys, time, html, urllib.request
from pathlib import Path
OUT = Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)
HERE = Path(__file__).resolve().parent
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

def get(url, headers=None, tries=4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})}), timeout=40) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            print("ошибка", url, repr(e), flush=True); time.sleep(8 * (i + 1))
    return None

info = {}
for code in "YwQ7AOma YNo8l6DA bAaJQgP9".split():
    b = "https://booking.langame.ru"
    try:
        tok = json.loads(get(f"{b}/api/initialize", {"Accept": "application/json", "Referer": f"{b}/{code}", "X-CLUB-HASH": code}))["data"]["token"]
        time.sleep(2)
        info[code] = json.loads(get(f"{b}/api/config", {"Accept": "application/json", "Referer": f"{b}/{code}", "X-AUTH-TOKEN": tok}))["data"].get("club_info")
    except Exception as e:  # noqa: BLE001
        info[code] = {"error": repr(e)}
    print(code, info[code] and {k: info[code].get(k) for k in ("name", "full_address", "latitude", "longitude")}, flush=True)
    time.sleep(3)
json.dump(info, open(OUT / "clubs22_retry.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)

def text(h):
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", h, flags=re.S)
    t = html.unescape(re.sub(r"<[^>]+>", "\n", t))
    return [s.strip() for s in t.split("\n") if s.strip()]

out = []
for c in [c for c in json.load(open(HERE / "catalog_urls.json", encoding="utf-8")) if c.get("status") == 200]:
    raw = get(c["url"], tries=3); rec = {"url": c["url"], "title": c.get("title"), "codes": c.get("codes")}
    if raw:
        h = raw.decode("utf-8", "replace"); L = text(h)
        t = re.search(r"<title>(.*?)</title>", h, re.S); rec["page_title"] = html.unescape(t.group(1).strip()) if t else None
        m = re.search(r'data-coords="([\d.]+),\s*([\d.]+)"', h); rec["coords"] = [float(m.group(1)), float(m.group(2))] if m else None
        def block(start, stops):
            if start not in L: return []
            i = L.index(start) + 1; res = []
            while i < len(L) and L[i] not in stops and len(res) < 40: res.append(L[i]); i += 1
            return res
        rec["services"] = block("Услуги", {"Цены", "Отзывы", "Конфигурация компьютеров", "Описание"})
        rec["config"] = block("Оборудование", {"Процессор", "Видеокарта", "Цены", "Услуги"}) or block("Конфигурация компьютеров", {"Процессор", "Видеокарта", "Цены", "Услуги"})
        gi = L.index("Видеокарта") if "Видеокарта" in L else -1
        rec["gpu"] = L[gi + 1:gi + 4] if gi >= 0 else []
        di = L.index("Описание") if "Описание" in L else -1
        rec["descr"] = " ".join(L[di + 1:di + 6])[:600] if di >= 0 else None
    out.append(rec); print(rec.get("title"), rec.get("services", [])[:3], rec.get("coords"), flush=True); time.sleep(1)
json.dump(out, open(OUT / "catalog2.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
