"""Разведка: открывает страницу бронирования как обычный посетитель и записывает,
что страница загружает (запросы, ответы API, websocket), плюс скриншот и текст.

Запуск: python discover/discover.py <папка_для_результата>
"""
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

URL = os.environ.get("TARGET_URL", "https://booking.langame.ru/1jelye8o")
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "out")
TEXT_TYPES = ("json", "javascript", "text", "html", "xml", "x-www-form-urlencoded")
BODY_TYPES = {"xhr", "fetch", "document", "script", "other", "eventsource"}
MAX_BODY = 3_000_000


def slug(url: str, n: int) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", url.split("://", 1)[-1])[:90]
    return f"{n:03d}_{s}"


async def run_pass(p, name: str, context_kwargs: dict) -> dict:
    out = OUT / name
    (out / "bodies").mkdir(parents=True, exist_ok=True)
    browser = await p.chromium.launch()
    ctx = await browser.new_context(locale="ru-RU", timezone_id="Asia/Almaty", **context_kwargs)
    page = await ctx.new_page()

    requests, sockets, console, pending = [], [], [], []
    counter = {"n": 0}

    async def on_response(resp):
        req = resp.request
        counter["n"] += 1
        n = counter["n"]
        entry = {
            "n": n, "t": round(time.time(), 3), "type": req.resource_type, "method": req.method,
            "url": resp.url, "status": resp.status, "post_data": req.post_data,
        }
        try:
            entry["req_headers"] = await req.all_headers()
            entry["resp_headers"] = await resp.all_headers()
        except Exception as e:  # noqa: BLE001
            entry["headers_error"] = str(e)
        ctype = (entry.get("resp_headers") or {}).get("content-type", "")
        entry["content_type"] = ctype
        if req.resource_type in BODY_TYPES and any(t in ctype for t in TEXT_TYPES):
            try:
                body = await resp.body()
                entry["body_len"] = len(body)
                fname = slug(resp.url, n) + (".json" if "json" in ctype else ".txt")
                (out / "bodies" / fname).write_bytes(body[:MAX_BODY])
                entry["body_file"] = f"bodies/{fname}"
                entry["body_head"] = body[:400].decode("utf-8", "replace")
            except Exception as e:  # noqa: BLE001
                entry["body_error"] = str(e)
        requests.append(entry)

    def on_ws(ws):
        rec = {"url": ws.url, "frames": []}
        sockets.append(rec)
        ws.on("framesent", lambda d: rec["frames"].append({"t": round(time.time(), 3), "dir": "sent", "data": str(d)[:20000]}))
        ws.on("framereceived", lambda d: rec["frames"].append({"t": round(time.time(), 3), "dir": "recv", "data": str(d)[:50000]}))

    page.on("response", lambda r: pending.append(asyncio.ensure_future(on_response(r))))
    page.on("websocket", on_ws)
    page.on("console", lambda m: console.append(f"[{m.type}] {m.text}"))
    page.on("pageerror", lambda e: console.append(f"[pageerror] {e}"))

    result = {"pass": name, "url": URL}
    try:
        await page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception as e:  # noqa: BLE001
            result["networkidle"] = f"timeout: {e}"
        await page.wait_for_timeout(12_000)
        result["final_url"] = page.url
        result["title"] = await page.title()
        await page.screenshot(path=str(out / "screenshot.png"), full_page=True)
        (out / "page.html").write_text(await page.content(), encoding="utf-8")
        (out / "page_text.txt").write_text(await page.locator("body").inner_text(), encoding="utf-8")
        storage = await page.evaluate("() => ({local: {...localStorage}, session: {...sessionStorage}, cookie: document.cookie})")
        (out / "storage.json").write_text(json.dumps(storage, ensure_ascii=False, indent=1), encoding="utf-8")
        elements = await page.evaluate(
            """() => [...document.querySelectorAll('button, a, input, select, [role=button], [onclick], [class*=seat], [class*=place], [class*=pc], svg')]
                 .slice(0, 400).map(e => {const r = e.getBoundingClientRect();
                   return {tag: e.tagName, text: (e.innerText || e.value || e.getAttribute('aria-label') || '').trim().slice(0, 80),
                           cls: (e.className && e.className.baseVal !== undefined ? e.className.baseVal : e.className || '').toString().slice(0, 120),
                           x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)}})"""
        )
        (out / "elements.json").write_text(json.dumps(elements, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        result["error"] = repr(e)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    (out / "requests.json").write_text(json.dumps(requests, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "websockets.json").write_text(json.dumps(sockets, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "console.txt").write_text("\n".join(console), encoding="utf-8")
    result["requests"] = len(requests)
    result["websockets"] = [{"url": s["url"], "frames": len(s["frames"])} for s in sockets]
    result["api_like"] = [
        {k: r.get(k) for k in ("n", "type", "method", "status", "url", "content_type", "body_len", "body_file")}
        for r in requests if r["type"] in ("xhr", "fetch", "eventsource", "other")
    ]
    await browser.close()
    return result


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        passes = [
            ("mobile", dict(p.devices["iPhone 13"])),
            ("desktop", {"viewport": {"width": 1440, "height": 900}}),
        ]
        results = []
        for name, kwargs in passes:
            results.append(await run_pass(p, name, kwargs))
    (OUT / "summary.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=1)[:20000])


if __name__ == "__main__":
    asyncio.run(main())
