"""Разовый поиск: какие клубы Астаны из каталога langame.ru принимают онлайн-бронь (ссылка booking.langame.ru/<код>).

Запуск: python discover/catalog.py <папка_для_результата>
Открывает страницу города как обычный посетитель, раскрывает весь список и по очереди, с паузой,
читает страницы клубов. Ничего не бронирует и не отправляет.
"""
import asyncio
import json
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright

CITY = "https://langame.ru/kazakhstan/computerniy_club_astana"
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "out")
CLUB_RE = re.compile(r"/kazakhstan/\d+_[a-z0-9_-]+_astana\b", re.I)
BOOK_RE = re.compile(r"booking\.langame\.ru/([A-Za-z0-9]{6,12})")


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    log = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(locale="ru-RU", timezone_id="Asia/Almaty",
                                        viewport={"width": 1400, "height": 1000})
        page = await ctx.new_page()
        api = []

        async def on_response(resp):
            if resp.request.resource_type in ("xhr", "fetch"):
                item = {"url": resp.url, "method": resp.request.method, "status": resp.status,
                        "post": resp.request.post_data}
                try:
                    body = await resp.text()
                    item["len"] = len(body)
                    item["head"] = body[:600]
                    item["codes"] = sorted(set(BOOK_RE.findall(body)))
                except Exception as e:  # noqa: BLE001
                    item["error"] = str(e)
                api.append(item)

        page.on("response", lambda r: asyncio.create_task(on_response(r)))
        await page.goto(CITY, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(6000)

        async def links():
            hrefs = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
            return sorted({h.split("?")[0].split("#")[0] for h in hrefs if CLUB_RE.search(h)})

        prev, still = -1, 0
        for i in range(120):
            cur = len(await links())
            log.append(f"шаг {i}: клубов {cur}")
            still = still + 1 if cur == prev else 0
            if still >= 3:
                break
            prev = cur
            await page.mouse.wheel(0, 20000)
            await page.wait_for_timeout(800)
            more = page.get_by_text("Показать ещё", exact=False)
            try:
                if await more.count() and await more.first.is_visible():
                    await more.first.click(timeout=5000)
            except Exception as e:  # noqa: BLE001
                log.append(f"  клик: {e}"[:200])
            await page.wait_for_timeout(2500)
        club_urls = await links()
        await page.screenshot(path=str(OUT / "city.png"), full_page=False)
        (OUT / "city.html").write_text(await page.content(), encoding="utf-8")
        (OUT / "api.json").write_text(json.dumps(api, ensure_ascii=False, indent=1), encoding="utf-8")

        clubs = []
        for n, url in enumerate(club_urls):
            item = {"url": url}
            try:
                r = await ctx.request.get(url, timeout=40000)
                html = await r.text()
                item["status"] = r.status
                m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
                item["title"] = re.sub(r"<[^>]+>|\s+", " ", m.group(1)).strip() if m else None
                item["codes"] = sorted(set(BOOK_RE.findall(html)))
            except Exception as e:  # noqa: BLE001
                item["error"] = str(e)[:200]
            clubs.append(item)
            await asyncio.sleep(0.7)
        await browser.close()

    (OUT / "clubs.json").write_text(json.dumps(clubs, ensure_ascii=False, indent=1), encoding="utf-8")
    with_booking = [c for c in clubs if c.get("codes")]
    lines = [f"Клубов в каталоге: {len(clubs)}; с онлайн-бронью: {len(with_booking)}", ""]
    lines += [f"{' '.join(c['codes'])}\t{c.get('title')}\t{c['url']}" for c in with_booking]
    lines += ["", "Ошибки:"] + [f"{c['url']}\t{c.get('status')}\t{c.get('error')}" for c in clubs
                                if c.get("error") or c.get("status") != 200]
    lines += ["", "Ход:"] + log
    (OUT / "CATALOG.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:60]))


asyncio.run(main())
