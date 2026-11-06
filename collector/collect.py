"""Раз в час: берёт из API онлайн-бронирования Langame, на каких местах клуба сейчас идёт игра,
сохраняет ответ в data/<клуб>/raw/<дата>/<время замера>.json и пересобирает occupancy.csv и SUMMARY.md.

Запрос тот же, что делает страница бронирования для ближайшего часа: в ответе состояние каждого
места на момент запроса, время окончания текущих сессий, будущие брони и брони на этот час.

Запуск: python3 collector/collect.py
Переменные окружения:
  CLUB_HASH      — код клуба из ссылки booking.langame.ru/<код> (по умолчанию LORAN)
  COLLECT_UNTIL  — после этого момента (ISO, с часовым поясом) сбор не выполняется
"""
import http.client
import json
import math
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402

BASE = "https://booking.langame.ru"
CLUB_HASH = os.environ.get("CLUB_HASH", "1jelye8o")
COLLECT_UNTIL = os.environ.get("COLLECT_UNTIL", "")
DATA = build.ROOT / "data" / CLUB_HASH
WINDOW_MIN = 60
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
# Сервис иногда минутами не отвечает: повторяем запросы, пока не выйдет бюджет на клуб
# (RETRY_BUDGET_S, по умолчанию 6 минут). Между запросами — пауза, чтобы не нагружать сервис.
START = time.monotonic()
BUDGET_S = int(os.environ.get("RETRY_BUDGET_S", "360"))
REQUEST_PAUSE_S = float(os.environ.get("REQUEST_PAUSE_S", "2"))
REQUEST_TIMEOUT_S = 30
DELAYS_S = (10, 20, 30, 45, 60)
_last_request = None


def api(method: str, path: str, token: str | None = None, body: dict | None = None, query: str = "") -> dict:
    global _last_request
    if _last_request is not None:
        time.sleep(max(0.0, REQUEST_PAUSE_S - (time.monotonic() - _last_request)))
    try:
        return _api(method, path, token, body, query)
    finally:
        _last_request = time.monotonic()


def _api(method: str, path: str, token: str | None, body: dict | None, query: str) -> dict:
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
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err = RuntimeError(f"{method} {path} -> HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:2000]}")
            if e.code < 500 and e.code != 429:
                raise err from None
        except (OSError, http.client.HTTPException, ValueError) as e:  # таймаут, обрыв, не-JSON
            err = RuntimeError(f"{method} {path} -> {e!r}")
        delay = DELAYS_S[min(attempt, len(DELAYS_S) - 1)]
        if time.monotonic() - START + delay + REQUEST_TIMEOUT_S > BUDGET_S:
            raise err
        attempt += 1
        print(f"{err}; повтор {attempt} через {delay} с", file=sys.stderr, flush=True)
        time.sleep(delay)


def diagnose() -> None:
    """После неудачи печатает, доступен ли сервер из сети раннера, чтобы по логу было видно причину."""
    host = urllib.parse.urlsplit(BASE).hostname
    try:
        ips = sorted({a[4][0] for a in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)})
    except OSError as e:
        print(f"DNS {host}: {e!r}")
        return
    for ip in ips[:4]:
        t = time.monotonic()
        try:
            socket.create_connection((ip, 443), timeout=10).close()
            print(f"TCP {ip}:443 — соединение есть, {time.monotonic() - t:.1f} с")
        except OSError as e:
            print(f"TCP {ip}:443 — {e!r}")
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=10) as resp:
            print("IP раннера:", resp.read().decode())
    except OSError as e:
        print(f"IP раннера не узнать: {e!r}")


def main() -> int:
    if COLLECT_UNTIL and datetime.now(timezone.utc) > datetime.fromisoformat(COLLECT_UNTIL):
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
    start = earliest.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=math.ceil(minutes / step) * step)
    stop = start + timedelta(minutes=WINDOW_MIN)
    window = {"start_at": start.strftime("%Y-%m-%d %H:%M"), "stop_at": stop.strftime("%Y-%m-%d %H:%M")}

    raw = {"collected_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "club_name": (config.get("club_info") or {}).get("name") or CLUB_HASH,
           "server_now": server_now.isoformat(), "window": window}
    if config.get("booking_calculator"):
        raw["mode"] = "calculator"
        raw["seats_response"] = api("POST", "/api/booking/seats/calculator", token, window)
    else:
        raw["mode"] = "packet"
        tariffs = api("GET", "/api/tariff", token, query="?start_at=" + urllib.parse.quote(window["start_at"]))
        basic = next(t for t in build.as_list(tariffs["data"]["tariffs"]) if t.get("type") != "packet")
        raw["seats_response"] = api("POST", "/api/booking/seats/packet", token,
                                    {"start_at": window["start_at"], "tariff_id": basic["id"]})

    DATA.mkdir(parents=True, exist_ok=True)
    scheme_path = DATA / "scheme.json"
    if not scheme_path.exists() or (start.hour == 12 and start.minute == 0):
        scheme_path.write_text(json.dumps(api("GET", "/api/seats/scheme/full", token), ensure_ascii=False, indent=1),
                               encoding="utf-8")

    raw_path = DATA / "raw" / server_now.strftime("%Y-%m-%d") / f"{server_now.strftime('%H%M')}.json"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    # Тарифы и пакеты клуба на завтра (раз в день): по ним видно цены и окна пакетов
    tomorrow = (server_now + timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
    tariffs_path = DATA / "tariffs" / f"{tomorrow:%Y-%m-%d}.json"
    if not tariffs_path.exists():
        try:
            tariffs = {}
            for hour in (8, 12, 18, 23):
                key = tomorrow.replace(hour=hour).strftime("%Y-%m-%d %H:%M")
                tariffs[key] = api("GET", "/api/tariff", token, query="?start_at=" + urllib.parse.quote(key))
            tariffs_path.parent.mkdir(parents=True, exist_ok=True)
            tariffs_path.write_text(json.dumps(tariffs, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:  # тарифы — дополнение, сбор занятости из-за них не должен падать
            print(f"Тарифы не сохранены: {e!r}", file=sys.stderr)

    build.build(CLUB_HASH)
    measured, zones = build.parse(raw, build.seat_uuids(json.loads(scheme_path.read_text(encoding="utf-8"))))
    a = zones[build.ALL]
    print(f"замер {measured:%Y-%m-%d %H:%M}: играют {len(a['playing'])}, бронь идёт без игры {len(a['paid'])}, "
          f"придержано под бронь {len(a['soon'])}, свободно {len(a['free'])}, выключено {len(a['unavailable'])} "
          f"из {a['seats']}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        diagnose()
        raise
