"""Сессии и выручка клуба по часовым замерам. Правила — README, «Как считаем выручку».

Запуск: python3 collector/revenue.py <код клуба> [--json]
Печатает разбор (или JSON для отчёта). Ничего не записывает.
"""
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402

HOUR = timedelta(hours=1)
WINDOW_TOL = timedelta(minutes=8)       # «конец сессии» совпал с концом окна пакета
SAME_END = timedelta(minutes=5)         # та же сессия, если конец сдвинулся не больше
EARLY_MIN = timedelta(minutes=15)       # ушёл раньше: оплачено было дольше следующего замера хотя бы на 15 мин
LEFTOVER_SHARE = (0.5, 0.7, 1.0)        # доля недоигранных часов, которые доигрывают за 72 часа: мин, база, макс
CARRYOVER = {"1JB6NB4A", "1JB6N7pB", "bKqXNKEG",  # «не сгорают 72 часа» в прайсах: URSA, TOP GAME, Goat
             "krKEyOPv",  # Epsilon: в сервисе есть «3 часа» и «3 часа СГОРАЕМЫЕ» — обычные, значит, не сгорают
             # с 10.11 по прайсам от пользователя: «не сгорают / сохраняются 72 часа»
             "12O49yj2", "16Xp6WAL", "1avrAZ8r", "b56KpWW7", "b7g5XPAd", "bAaJQgP9"}

# Акции и бонусы: доля выручки, которой они касаются, и скидка для неё — ДОПУЩЕНИЯ (мин, база, макс)
PROMOS = {
    "1jelye8o": [("Бонус +50% при пополнении на 5 000 / 10 000 ₸ (скидка 1/3 для тех, кто пополняет)",
                  (0.10, 0.30, 0.50), 1 / 3)],
    "kywedwxo": [("−20% навсегда игрокам с высоким рангом", (0.05, 0.10, 0.20), 0.20)],
    "1JB6NB4A": [("Бонусы лояльности 3–20% от сыгранных часов (в среднем ~5%)", (0.6, 0.8, 1.0), 0.05)],
    "bKqXNKEG": [("Накопительная скидка 5–20% (в среднем ~5%)", (0.5, 0.7, 1.0), 0.05),
                 ("Студентам AITU/NU −15%", (0.05, 0.10, 0.20), 0.15)],
    "16Xp6Rly": [],
    "1JB6N7pB": [],
    "krKEyOPv": [],  # прайса с акциями пока нет
    "kgGamr9N": [],  # прайса с акциями пока нет
    "12O49yj2": [("Лояльность ReHub 1–15% по сумме пополнений (в среднем ~3%)", (0.5, 0.7, 1.0), 0.03)],
    "YZPKeGgN": [("Студентам и школьникам −15%", (0.05, 0.10, 0.20), 0.15)],
    "b7g5XPAd": [("Персональная скидка «чем больше играешь» (размер не указан, ~5%)", (0.2, 0.4, 0.7), 0.05)],
    "16Xp6WAL": [], "1avrAZ8r": [], "b56KpWW7": [], "bAaJQgP9": [],
}


def hours(d: timedelta) -> float:
    return d.total_seconds() / 3600


class Prices:
    """Цены клуба из сервиса брони (data/<код>/prices.json): почасовой и пакеты по зонам, будни/выходные."""

    def __init__(self, club: str):
        base = build.ROOT / "data" / club
        p = json.loads((base / "prices.json").read_text(encoding="utf-8"))
        self.club_name = p["club_name"]
        weekday_date, weekend_date = p["days"]
        day_type = lambda s: "будни" if s.startswith(weekday_date) else "выходные"  # noqa: E731
        # Какие дни недели идут по ценам будней — по прайсу клуба (пн-пт / пн-чт / все дни)
        if (base / "pricelist.json").exists():
            keys = " ".join(json.loads((base / "pricelist.json").read_text(encoding="utf-8"))["prices"])
            self.weekdays = {0, 1, 2, 3, 4} if "пн-пт" in keys else {0, 1, 2, 3} if "пн-чт" in keys else set(range(7))
        else:
            # Прайса с картинок нет: если в сервисе цены понедельника и субботы разные, будни — пн–пт (допущение)
            def by_day(rows, key):
                out = {"будни": {}, "выходные": {}}
                for x in rows:
                    if "price" in x:
                        out[day_type(x["start"])][key(x)] = x["price"]
                return out
            h = by_day(p["hourly"], lambda x: (x["zone"], x["start"][11:13]))
            k = by_day(p["packets"], lambda x: (x["zone"], x["tariff_id"]))
            differs = any(h["будни"].get(z) not in (None, v) for z, v in h["выходные"].items()) or \
                any(k["будни"].get(z) not in (None, v) for z, v in k["выходные"].items())
            self.weekdays = {0, 1, 2, 3, 4} if differs else set(range(7))
        self.hourly = {}
        for x in p["hourly"]:
            if "price" in x:
                self.hourly[(x["zone"], day_type(x["start"]), int(x["start"][11:13]))] = x["price"]
        self.packets = {}
        for x in p["packets"]:
            if "price" in x:
                self.packets.setdefault((x["tariff_id"], x["zone"], day_type(x["start"])), x["price"])
        # Когда кончается пакет, если взять его в каждый час: старт + длительность из списка тарифов сервиса
        spans = {}
        for start, items in (p.get("tariffs_by_start") or {}).items():
            for t in items:
                if t.get("type") == "packet" and t.get("minutes_duration"):
                    a = datetime.strptime(start, "%Y-%m-%d %H:%M")
                    spans.setdefault(t["id"], []).append((a, a + timedelta(minutes=int(t["minutes_duration"]))))
        self.windows, self.durations = [], {}
        for t in p["tariffs"]:
            if t.get("type") != "packet":
                continue
            name = t.get("tariff_name") or ""
            m = re.search(r"\((\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\)", name)
            avail = {(day_type(s), int(s[11:13])) for s in t.get("available_from", [])}
            if m:
                a, b = (int(m.group(1)), int(m.group(2))), (int(m.group(3)), int(m.group(4)))
                self.windows.append({"id": t["id"], "name": short(name), "start": a, "end": b, "avail": avail})
                continue
            sp = spans.get(t["id"], [])
            if len(sp) >= 2 and len({b.strftime("%H:%M") for a, b in sp}) == 1:
                # Окно без времени в названии (Epsilon: Atlas, Nova): с какого часа ни возьми — кончается в одно время
                a, b = max(sp, key=lambda x: x[1] - x[0])
                self.windows.append({"id": t["id"], "name": short(name), "start": (a.hour, a.minute),
                                     "end": (b.hour, b.minute), "avail": avail})
                continue
            n = 3 if name.strip() == "2+1" else 5 if name.strip() == "3+2" else None
            m = re.search(r"(\d+)\s*час", name)
            n = n or (int(m.group(1)) if m else None)
            if n and (n not in self.durations or "сгора" in self.durations[n]["name"].lower()):
                self.durations[n] = {"id": t["id"], "name": short(name), "hours": n}
        # Почасовой тариф клуб онлайн не считает («калькулятор бронирования не активирован», Epsilon):
        # базовый тариф из списка — для самой большой зоны, для остальных — в той же пропорции, что их пакет на 3 часа
        self.hourly_assumed = False
        basic = next((t for t in p["tariffs"] if t.get("type") != "packet" and t.get("default_price")), None)
        if not self.hourly and basic and p.get("zones"):
            main = max(p["zones"], key=p["zones"].get)
            ref = self.durations.get(3) or (min(self.durations.values(), key=lambda d: d["hours"]) if self.durations else None)
            for zone in p["zones"]:
                ratio = 1.0
                if ref:
                    z, mz = self.packets.get((ref["id"], zone, "будни")), self.packets.get((ref["id"], main, "будни"))
                    ratio = z / mz if z and mz else 1.0
                for dt in ("будни", "выходные"):
                    for slot in (11, 19, 23):
                        self.hourly[(zone, dt, slot)] = round(basic["default_price"] * ratio / 50) * 50
            self.hourly_assumed = True

    def day_type(self, when: datetime) -> str:
        if len(self.weekdays) == 7:
            return "будни"  # цены одинаковые во все дни; ключ цен — «будни»
        return "будни" if when.weekday() in self.weekdays else "выходные"

    def day_label(self, when: datetime) -> str:
        return "все дни" if len(self.weekdays) == 7 else self.day_type(when)

    def hour_price(self, zone: str, when: datetime) -> float | None:
        slot = 11 if 8 <= when.hour < 17 else 19 if 17 <= when.hour < 23 else 23
        dt = self.day_type(when)
        return self.hourly.get((zone, dt, slot)) or self.hourly.get((zone, "будни" if dt == "выходные" else "выходные", slot))

    def packet_price(self, tariff_id, zone: str, when: datetime) -> float | None:
        dt = self.day_type(when)
        return self.packets.get((tariff_id, zone, dt)) or self.packets.get(
            (tariff_id, zone, "будни" if dt == "выходные" else "выходные"))


def short(name: str) -> str:
    name = re.sub(r"^Пакет\s+", "", name.strip()).replace('"', "").replace("«", "").replace("»", "")
    name = re.sub(r"\s*\((?:c|с)горает\)\s*", "", name)
    name = re.sub(r"^на\s+", "", name)
    return re.sub(r"\s+", " ", name).strip()


@dataclass
class Session:
    seat: str
    zone: str
    start_lo: datetime | None      # не раньше (None — началась до начала сбора)
    start_hi: datetime             # не позже (первый замер, где место занято)
    end: datetime                  # оплачено до (конец сессии по сервису, с продлениями)
    end0: datetime | None = None   # конец при первом замере — по нему цена самой сессии
    booking: tuple | None = None   # (начало, конец) идущей брони, если сессия держится на ней
    kind: str = ""                 # «пакет с окном», «пакет», «почасовой»
    tariff: str = ""
    hours_paid: float = 0.0
    price: float = 0.0
    day_type: str = ""
    extensions: list = field(default_factory=list)   # (время, часы, тариф, цена)
    early_left: float | None = None                   # недоиграно, ч (середина интервала)
    early_range: tuple = ()
    last_seen: datetime | None = None
    moved_from: str | None = None
    no_show: bool = False              # бронь закончилась, а за местом так никто и не играл
    after_booking: tuple | None = None  # сессия началась после брони, которая шла без игры (гость опоздал)


def club_buffer(snaps: list, prices: Prices, profile: dict) -> int:
    """Запас клуба в минутах: насколько «занято до» позже конца пакета с окном (мода по всем сессиям).
    Если по окнам не видно — запас из мест, придержанных под бронь (club_profile), иначе 0."""
    ends = {w["end"] for w in prices.windows}
    diffs = Counter()
    for now, seats in snaps:
        for s in seats:
            until = build.when(s.get("stop_at"))
            if s.get("seat_state") != "BUSY" or not until:
                continue
            for h, m in ends:
                for day in (until.date(), until.date() - timedelta(days=1)):
                    w_end = datetime.combine(day, datetime.min.time()).replace(hour=h, minute=m)
                    d = round((until - w_end).total_seconds() / 60)
                    if 0 <= d <= 45:
                        diffs[round(d / 5) * 5] += 1
    if sum(diffs.values()) >= 5:
        return diffs.most_common(1)[0][0]
    return profile.get("buffer") or 0


def window_for(prices: Prices, end: datetime, start_lo, start_hi: datetime, zone: str, tol: timedelta = WINDOW_TOL):
    """Пакет с окном, который кончается в end. Из подходящих — тот, что можно было купить в момент начала
    (начало между start_lo и start_hi; окно начинается не позже start_hi), а из них — самый дешёвый."""
    starts = [start_hi] if start_lo is None else [start_lo + k * HOUR for k in range(int(hours(start_hi - start_lo)) + 1)] + [start_hi]
    cands = []
    for w in prices.windows:
        w_end = end.replace(hour=w["end"][0], minute=w["end"][1], second=0, microsecond=0)
        if abs(end - w_end) > tol:
            continue
        w_start = w_end.replace(hour=w["start"][0], minute=w["start"][1])
        if w_start > w_end:
            w_start -= timedelta(days=1)
        fits = start_hi >= w_start - HOUR  # окно уже началось (или вот-вот начнётся) к моменту, когда место заняли
        ok = any((prices.day_type(t), t.hour) in w["avail"] for t in starts)
        price = prices.packet_price(w["id"], zone, start_hi)
        cands.append((not fits, not ok, price if price is not None else 1e12, w))
    if not cands:
        return None
    cands.sort(key=lambda c: (c[0], c[1], c[2]))
    return cands[0][3]


def price_duration(prices: Prices, n: float, zone: str, when: datetime) -> tuple[str, str, float]:
    """Цена n часов без окна: 1–2 ч и нецелые — почасово; 3–5 ч — пакет (нет пакета на 4 ч — 4/5 цены пакета на 5 ч);
    больше 5 ч — пакет на 5 ч + остальное почасово."""
    hp = prices.hour_price(zone, when) or 0
    whole = abs(n - round(n)) < 0.15
    if n <= 2.25 or not whole and n < 2.75:
        return "почасовой", f"{n:g} ч почасово", round(n * hp)
    k = round(n) if whole else round(n)
    if 3 <= k <= 5:
        d = prices.durations.get(k)
        if d:
            return "пакет", d["name"], prices.packet_price(d["id"], zone, when) or k * hp
        five = prices.durations.get(5)
        if five:
            return "пакет", f"{k} ч (по цене часа пакета на 5 ч)", round((prices.packet_price(five["id"], zone, when) or 5 * hp) * k / 5)
        return "почасовой", f"{k} ч почасово", k * hp
    five = prices.durations.get(5)
    base = prices.packet_price(five["id"], zone, when) if five else 5 * hp
    return "пакет", f"5 ч + {n - 5:g} ч почасово", round((base or 5 * hp) + (n - 5) * hp)


def sessions_of(club: str):
    snaps_raw, on_map, club_name = build.load(club)
    snaps = [(now, build.seats_of(raw, on_map)) for now, raw in snaps_raw]
    profile = build.club_profile(snaps)
    prices = Prices(club)
    buffer = timedelta(minutes=club_buffer(snaps, prices, profile))
    open_s: dict[str, Session] = {}
    done: list[Session] = []
    prev_t = None
    early_events = []
    period_start = snaps[0][0]
    EXT_MIN = timedelta(minutes=15)    # меньший сдвиг конца — не покупка, а неточность «занято до»
    BOOK_TOL = timedelta(minutes=12)   # конец сессии совпал с концом идущей брони
    # Брони, которые идут, а за местом никто не играет: ждём, придёт ли гость до конца брони
    paid_open: dict[str, dict] = {}
    # Последняя бронь, под которую место держали или по которой оно шло без игры. Там, где бронь пропадает из списка,
    # как только гость запустил по ней сессию (LORAN), только так видно, что сессия — это бронь
    seen_booking: dict[str, tuple] = {}

    def close_paid(u: str, ended: bool):
        p = paid_open.pop(u, None)
        if p and ended:  # бронь кончилась без игры — неявка; оплачена, поэтому идёт в выручку отдельной строкой
            a, b = p["booking"]
            done.append(Session(seat=p["seat"], zone=p["zone"], start_lo=a if a >= period_start else None,
                                start_hi=a, end=b, end0=b, booking=(a, b), last_seen=p["last"], no_show=True))

    def mark_early(cur: Session, t: datetime):
        limit = max(cur.end, cur.booking[1] if cur.booking else cur.end)
        if cur.end > t + EARLY_MIN and not (cur.booking and cur.booking[1] <= t + EARLY_MIN):
            lo, hi = hours(cur.end - t), hours(cur.end - (cur.last_seen or t))
            cur.early_left, cur.early_range = (lo + hi) / 2, (lo, hi)
            early_events.append(cur)
        return limit

    for t, seats in snaps:
        if prev_t is not None and t - prev_t < timedelta(minutes=20):
            continue  # два замера в одном часе — берём первый, второй почти ничего не добавляет
        new_this_round = []
        for s in seats:
            u = s.get("device_uuid")
            state = build.classify(s, t, profile)
            until = build.when(s.get("stop_at"))
            end = until - buffer if until else None
            cur = open_s.get(u)
            if state == "soon" and end:
                # Место придержано под бронь: запоминаем её — сессия, которая потом кончится вместе с ней, и есть эта бронь
                future = [(a, b) for a, b in build.bookings(s) if a > t]
                if future:
                    seen_booking[u] = min(future, key=lambda x: abs(end - x[1]))
            if state == "paid":
                bk = next(((a, b) for a, b in build.bookings(s) if a <= t < b), None)
                p = paid_open.get(u)
                if bk:
                    seen_booking[u] = bk
                    paid_open[u] = {"booking": bk, "seat": str(s.get("name")), "zone": s.get("zone_name") or "?",
                                    "first": p["first"] if p and p["booking"] == bk else t, "last": t}
            elif state != "playing" and u in paid_open:
                a, b = paid_open[u]["booking"]
                if t >= b - SAME_END:
                    close_paid(u, ended=True)       # бронь кончилась, никто не играл
                elif (a, b) not in build.bookings(s):
                    close_paid(u, ended=False)      # бронь отменили или перенесли
            if state != "playing" or not end:
                if cur:
                    if state != "soon":  # под бронь место показывают занятым заранее — идёт ли ещё сессия, не видно
                        mark_early(cur, t)
                    done.append(open_s.pop(u))
                continue
            # Идущая бронь, на которой держится место (у клубов, где бронь видна и во время сессии)
            booking = next(((a, b) for a, b in build.bookings(s) if a <= t < b and abs(end - b) <= BOOK_TOL), None)
            if cur and cur.booking and (booking == cur.booking or cur.booking in build.bookings(s)):
                # Та же оплаченная бронь: гость запустил сессию или конец уточнился — не новая покупка
                if end > cur.booking[1] + EXT_MIN and end > cur.end + EXT_MIN:
                    add = end - max(cur.end, cur.booking[1])
                    kind, name, price = price_duration(prices, round(hours(add) * 4) / 4, cur.zone, t)
                    cur.extensions.append((t, hours(add), name, price))
                cur.end, cur.last_seen = max(end, cur.end) if end > cur.booking[1] + EXT_MIN else end, t
                continue
            if cur and abs(end - cur.end) < EXT_MIN and cur.end > t - SAME_END:
                cur.end, cur.last_seen = max(cur.end, end), t  # та же сессия (сдвиг «занято до» на минуты)
                continue
            if cur and end >= cur.end + EXT_MIN and cur.end > t - SAME_END:
                # Продление: сессия идёт, конец сдвинулся позже
                add = end - cur.end
                w = window_for(prices, end, None, t, cur.zone)
                cur_w = window_for(prices, cur.end, cur.start_lo, cur.start_hi, cur.zone)
                if w and (not cur_w or cur_w["id"] != w["id"]):
                    price = prices.packet_price(w["id"], cur.zone, t) or 0
                    cur.extensions.append((t, hours(add), w["name"], price))
                else:
                    kind, name, price = price_duration(prices, round(hours(add) * 4) / 4, cur.zone, t)
                    cur.extensions.append((t, hours(add), name, price))
                cur.end, cur.last_seen = end, t
                continue
            if cur:  # сессия сменилась: прежняя кончилась (возможно, раньше оплаченного)
                mark_early(cur, t)
                done.append(open_s.pop(u))
                start_lo = (max(prev_t, cur.end) if cur.end <= t else prev_t) if prev_t else None
            else:
                start_lo = prev_t
            sess = Session(seat=str(s.get("name")), zone=s.get("zone_name") or "?", start_lo=start_lo,
                           start_hi=t, end=end, end0=end, last_seen=t)
            if u in paid_open:  # гость пришёл к своей брони позже её начала: цена — по его сессии
                sess.after_booking = paid_open.pop(u)["booking"]
            if not booking and u in seen_booking:
                a, b = seen_booking[u]
                if a <= t and abs(end - b) <= BOOK_TOL and (start_lo is None or a >= start_lo - HOUR):
                    booking = (a, b)  # бронь уже не в списке, но сессия кончается вместе с ней — это она
            seen_booking.pop(u, None)
            if booking:
                # Сессия на брони: оплачено время брони целиком, начало — начало брони.
                # Бронь началась до начала сбора — значит, и куплена до него: в выручку периода не идёт.
                sess.booking = booking
                if booking[0] < period_start:
                    sess.start_lo = None
                elif booking[0] <= t:
                    sess.start_lo = sess.start_hi = booking[0]
            open_s[u] = sess
            new_this_round.append((u, sess))
        # Пересадка: новая сессия на другом месте с тем же концом, что у сессии, ушедшей раньше в этом же интервале
        for u, sess in new_this_round:
            for e in early_events:
                if e.early_left is not None and not e.moved_from and e.last_seen == prev_t and e.seat != sess.seat \
                        and abs(e.end - sess.end) <= timedelta(minutes=3) and not window_for(prices, e.end, None, e.start_hi, e.zone):
                    sess.moved_from = e.seat
                    e.early_left, e.early_range = None, ()
                    break
        prev_t = t
    done.extend(open_s.values())
    # Брони, которые ещё идут без игры к последнему замеру: гость может прийти — пока не считаем
    pending_paid = []
    for u, p in list(paid_open.items()):
        if p["booking"][1] <= prev_t:
            close_paid(u, ended=True)
        else:
            pending_paid.append(paid_open.pop(u))

    # Тариф и цена каждой сессии
    for s in done:
        mid = s.start_hi if s.start_lo is None else s.start_lo + (s.start_hi - s.start_lo) / 2
        s.day_type = prices.day_type(mid)
        base_end = s.booking[1] if s.booking else (s.end0 or s.end)
        # Бронь ставится шагом 15 минут, поэтому её конец сверяем с окном пакета мягче
        w = window_for(prices, base_end, s.start_lo, s.start_hi, s.zone,
                       tol=timedelta(minutes=15) if s.booking else WINDOW_TOL)
        if s.start_lo is None:
            s.kind, s.tariff = "началась до сбора", (w["name"] if w else "—")
        elif s.no_show:
            n = round(hours(s.booking[1] - s.booking[0]) * 4) / 4
            if w:
                s.tariff, s.price = w["name"], prices.packet_price(w["id"], s.zone, mid) or 0
            else:
                _, s.tariff, s.price = price_duration(prices, n, s.zone, mid)
            s.kind, s.hours_paid = "бронь без игры", n
        elif w:
            s.kind, s.tariff = "пакет с окном", w["name"]
            s.price = prices.packet_price(w["id"], s.zone, mid) or 0
            s.hours_paid = hours(base_end - mid)
        elif s.booking:
            n = round(hours(s.booking[1] - s.booking[0]) * 4) / 4
            s.kind, s.tariff, s.price = price_duration(prices, n, s.zone, mid)
            s.hours_paid = n
        else:
            lo, hi = hours(base_end - s.start_hi), hours(base_end - s.start_lo)
            n = min(range(1, 13), key=lambda k: (0 if lo - 0.25 <= k <= hi + 0.1 else 1, abs(k - (lo + hi) / 2)))
            if not lo - 0.25 <= n <= hi + 0.1 and hi < 1:
                n = max(0.25, round((lo + hi) / 2 * 4) / 4)  # меньше часа и целый час не подходит: почасово по минутам
            s.kind, s.tariff, s.price = price_duration(prices, n, s.zone, mid)
            s.hours_paid = n
    return {"club": club, "club_name": prices.club_name, "snaps": snaps, "profile": profile, "prices": prices,
            "buffer_min": int(buffer.total_seconds() // 60), "sessions": done, "pending_paid": pending_paid,
            "period": (snaps[0][0], snaps[-1][0])}


def revenue(club: str) -> dict:
    r = sessions_of(club)
    prices, period = r["prices"], r["period"]
    lines = defaultdict(lambda: {"count": 0, "hours": 0.0, "sum": 0.0, "unit": set()})
    for s in r["sessions"]:
        if s.start_lo is not None and not s.moved_from:
            line = lines[(s.kind, s.tariff, s.zone, s.day_type)]
            line["count"] += 1
            line["hours"] += s.hours_paid
            line["sum"] += s.price
            line["unit"].add(s.price)
        for t, h, name, price in s.extensions:
            if period[0] <= t <= period[1]:
                ext = lines[("продление", name, s.zone, prices.day_type(t))]
                ext["count"] += 1
                ext["hours"] += h
                ext["sum"] += price
                ext["unit"].add(price)
    gross = sum(v["sum"] for v in lines.values())

    # Доигрывание пакетов 3–5 ч (только клубы, где часы не сгорают 72 часа)
    left = [s for s in r["sessions"] if s.early_left and s.kind == "пакет" and club in CARRYOVER]
    left_hours = sum(s.early_left for s in left)
    left_value = sum(price_duration(prices, round(s.early_left * 4) / 4, s.zone, s.start_hi)[2] for s in left)
    short_hours = sum(v["hours"] for k, v in lines.items() if k[0] == "почасовой")
    leftover = {"sessions": len(left), "hours": left_hours, "value": left_value,
                "deduct": {f"{int(sh * 100)}%": round(left_value * sh) for sh in LEFTOVER_SHARE},
                "share_of_short": (left_hours * LEFTOVER_SHARE[1] / short_hours) if short_hours else None}
    net_after_left = gross - leftover["deduct"]["70%"] if club in CARRYOVER else gross
    promos = []
    for name, (lo, mid, hi), disc in PROMOS.get(club, []):
        promos.append({"name": name, "share": (lo, mid, hi), "discount": disc,
                       "deduct": tuple(round(net_after_left * x * disc) for x in (lo, mid, hi))})
    promo_mid = sum(p["deduct"][1] for p in promos)
    promo_lo, promo_hi = sum(p["deduct"][0] for p in promos), sum(p["deduct"][2] for p in promos)
    left_lo = leftover["deduct"]["50%"] if club in CARRYOVER else 0
    left_hi = leftover["deduct"]["100%"] if club in CARRYOVER else 0
    return {**r, "lines": lines, "gross": gross, "leftover": leftover, "promos": promos,
            "net": (gross - left_hi - promo_hi, gross - (leftover["deduct"]["70%"] if club in CARRYOVER else 0) - promo_mid,
                    gross - left_lo - promo_lo)}


def main():
    club = sys.argv[1] if len(sys.argv) > 1 else "1jelye8o"
    r = revenue(club)
    p0, p1 = r["period"]
    print(f"{r['club_name']}: {p0:%d.%m %H:%M} – {p1:%d.%m %H:%M}, замеров {len(r['snaps'])}, запас {r['buffer_min']} мин")
    for s in sorted(r["sessions"], key=lambda s: (s.start_hi, build.num(s.seat))):
        start = "до сбора" if s.start_lo is None else f"{s.start_lo:%d %H:%M}–{s.start_hi:%H:%M}"
        ext = "; ".join(f"+{h:.2g} ч {n} {p:.0f}" for t, h, n, p in s.extensions)
        print(f"  #{s.seat:>3} {s.zone:10} старт {start:17} до {s.end:%d %H:%M} | {s.kind:15} {s.tariff:28} "
              f"{s.price:>6.0f} {s.day_type}{' | продл: ' + ext if ext else ''}"
              f"{f' | ушёл раньше, недоиграно ~{s.early_left:.1f} ч' if s.early_left else ''}"
              f"{f' | пересадка с #{s.moved_from}' if s.moved_from else ''}"
              f"{f' | после брони {s.after_booking[0]:%H:%M}–{s.after_booking[1]:%H:%M} без игры' if s.after_booking else ''}"
              f"{f' | на брони {s.booking[0]:%d %H:%M}–{s.booking[1]:%H:%M}' if s.booking and not s.no_show else ''}")
    for p in r["pending_paid"]:
        a, b = p["booking"]
        print(f"  #{p['seat']:>3} бронь {a:%d %H:%M}–{b:%H:%M} идёт без игры к последнему замеру — пока не считаем")
    print("Выручка по прайсу:")
    for (kind, tariff, zone, dt), v in sorted(r["lines"].items(), key=lambda kv: -kv[1]["sum"]):
        print(f"  {kind:15} {tariff:28} {zone:10} {dt:8} ×{v['count']:>3} = {v['sum']:>9,.0f}")
    print(f"  ИТОГО {r['gross']:,.0f}; доигрывание {r['leftover']}; акции {[(p['name'], p['deduct']) for p in r['promos']]}")
    print(f"  ИТОГ вилкой: {r['net'][0]:,.0f} … {r['net'][1]:,.0f} … {r['net'][2]:,.0f}")


if __name__ == "__main__":
    main()
