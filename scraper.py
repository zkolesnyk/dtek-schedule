#!/usr/bin/env python3
"""Збирач графіків відключень ДТЕК (Київські регіональні електромережі) для Divine Tools.

Сайт ДТЕК закритий Imperva/Incapsula і не пускає сервери, але раннери GitHub Actions
відкривають його як звичайний відвідувач (headless Chromium). Збирач читає вбудований
обʼєкт `DisconSchedule.fact` (графіки всіх груп) і, для вулиці з секрету STREETS,
відповідь `getHomeNum` (група кожного будинку й поточне відключення за адресою).
Результат — schedule.json у гілці data; сервер панелі читає його звичайним GET.

STREETS — JSON у секреті репозиторію: [{"key": "...", "city": "...", "street": "..."}].
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

KYIV = ZoneInfo("Europe/Kyiv")
URL = "https://www.dtek-krem.com.ua/ua/shutdowns"

# Стан години в fact -> пів-години (перша, друга): 0 — світло є, 1 — відключення, 2 — можливе
HALVES = {
    "yes": (0, 0),
    "no": (1, 1),
    "first": (1, 0),
    "second": (0, 1),
    "maybe": (2, 2),
    "mfirst": (2, 0),
    "msecond": (0, 2),
}


def blocked(html: str) -> bool:
    low = html.lower()
    return "_incapsula_resource" in low or "request unsuccessful" in low


FACT_RE = re.compile(r"\bfact\s*=\s*\{")


def extract_fact(html: str):
    """Обʼєкт `fact = {...}` зі скрипту сторінки — балансуванням дужок з урахуванням рядків"""
    m = FACT_RE.search(html)
    if not m:
        return None
    start = m.end() - 1
    depth, in_str, esc = 0, False, False
    for i in range(start, len(html)):
        ch = html[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(html[start : i + 1])
    return None


def halves_of(day: dict) -> dict:
    """{"GPV3.2": {"1": "yes", ...}} -> {"3.2": [48 чисел]}; ключ години "1" — це 00:00–01:00"""
    out = {}
    for name, hours in day.items():
        m = re.match(r"GPV(\d+\.\d+)$", name)
        if not m or not isinstance(hours, dict):
            continue
        row = []
        for h in range(1, 25):
            row.extend(HALVES.get(str(hours.get(str(h), "yes")), (0, 0)))
        out[m.group(1)] = row
    return dict(sorted(out.items()))


def main() -> int:
    streets = json.loads(os.environ.get("STREETS") or "[]")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(locale="uk-UA")
        fact = None
        for attempt in range(3):
            page.goto(URL, wait_until="load", timeout=60_000)
            page.wait_for_timeout(4_000)
            if blocked(page.content()):
                print(f"спроба {attempt + 1}: заглушка Imperva")
                page.wait_for_timeout(5_000)
                continue
            fact = page.evaluate("() => (window.DisconSchedule && window.DisconSchedule.fact) || null") or extract_fact(page.content())
            if fact:
                break
        # Оголошення на сторінці (екстренні відключення, графіки не діють) — текст як є
        notice = page.evaluate(
            """() => {
                const re = /екстрен|аварійн|графік[иів]* не (діють|застосов)|без застосування графік/i
                const els = [...document.querySelectorAll('h1, h2, h3, p, div, span, strong')]
                const hit = els.find(el => el.children.length < 3 && re.test(el.textContent || '') && (el.textContent || '').trim().length < 400)
                return hit ? hit.textContent.replace(/\\s+/g, ' ').trim() : null
            }"""
        )
        title = page.title()
        if not fact:
            html = page.content()
            print("не отримав fact", title, len(html), "fact=" in html, notice)
            # Що є на сторінці навколо слова fact — щоб підлаштувати розбір
            i = html.find("fact")
            print(html[max(0, i - 200): i + 300] if i >= 0 else "слова fact немає")
            fact = {}

        days = {}
        for ts, day in (fact.get("data") or {}).items():
            try:
                date = datetime.fromtimestamp(int(ts), KYIV).date().isoformat()
            except (TypeError, ValueError):
                continue
            if isinstance(day, dict):
                days[date] = halves_of(day)

        # Будинки вулиць: група й поточне відключення. Назви вулиці в ДТЕК бувають різні
        # («вул. Бабкіна», «вул. Бабкіна Олега»…) — беремо всі збіги з довідника сторінки
        addresses = {}
        for s in streets:
            cands = page.evaluate(
                """({city, street}) => {
                    const all = (window.DisconSchedule && window.DisconSchedule.streets) || {}
                    const norm = (t) => t.toLowerCase().replace(/^(м|с|смт|вул|просп|пров|бульв)\\.?\\s*/, '').trim()
                    const cities = Object.keys(all).filter(k => norm(k) === norm(city) || k === city)
                    const out = []
                    for (const c of cities) for (const st of all[c] || []) if (norm(st).includes(norm(street))) out.push([c, st])
                    return out
                }""",
                {"city": s["city"], "street": s["street"]},
            ) or [[s["city"], s["street"]]]
            print("варіанти адреси:", json.dumps(cands, ensure_ascii=False))
            houses = {}
            errors = []
            for city, street in cands:
                res = page.evaluate(
                    """async ({city, street}) => {
                        const token = document.querySelector('meta[name="csrf-token"]')?.content || ''
                        const body = new URLSearchParams({
                            method: 'getHomeNum',
                            'data[0][name]': 'city', 'data[0][value]': city,
                            'data[1][name]': 'street', 'data[1][value]': street,
                        })
                        const r = await fetch('/ua/ajax', {
                            method: 'POST', body,
                            headers: {'X-CSRF-Token': token, 'X-Requested-With': 'XMLHttpRequest'},
                        })
                        try { return await r.json() } catch (e) { return {error: String(r.status)} }
                    }""",
                    {"city": city, "street": street},
                )
                for num, info in ((res or {}).get("data") or {}).items():
                    if not isinstance(info, dict):
                        continue
                    reasons = info.get("sub_type_reason") or []
                    group = reasons[0] if isinstance(reasons, list) and reasons else None
                    m = re.match(r"GPV(\d+\.\d+)", str(group or ""))
                    outage = None
                    if info.get("start_date") or info.get("sub_type"):
                        outage = {
                            "type": str(info.get("type") or ""),
                            "reason": str(info.get("sub_type") or ""),
                            "start": str(info.get("start_date") or ""),
                            "end": str(info.get("end_date") or ""),
                        }
                    key = num if len(cands) == 1 else f"{street}, {num}"
                    houses[key] = {"group": m.group(1) if m else group, "outage": outage, "street": street}
                if (res or {}).get("error"):
                    errors.append(str(res["error"]))
            addresses[s["key"]] = {
                "houses": houses,
                "error": None if houses else (", ".join(errors) or "будинків не знайдено"),
            }
        browser.close()

    out = {
        "version": 1,
        "region": "kr",
        "fetched_at": datetime.now(KYIV).isoformat(timespec="seconds"),
        "dtek_updated": fact.get("update"),
        "page_title": title,
        "notice": notice,
        "days": days,
        "addresses": addresses,
    }
    os.makedirs("out", exist_ok=True)
    with open("out/schedule.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print("днів:", list(days), "груп:", len(next(iter(days.values()), {})), "адрес:", {k: len(v["houses"]) for k, v in addresses.items()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
