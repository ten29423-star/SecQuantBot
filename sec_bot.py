#!/usr/bin/env python3
"""SEC filings -> Telegram.

Следит за формами из config.json (по умолчанию 8-K и DEF 14A) для компаний из списка
и шлёт новые filings в Telegram.

Запуск:
  python sec_bot.py            обычный запуск (шлёт новые filings)
  python sec_bot.py --check    проверка: тикеры находятся, SEC отвечает, ничего не шлёт и не сохраняет

Переменные окружения:
  SEC_USER_AGENT      обязательно, формат "Имя email@example.com" (SEC блокирует запросы без него)
  TELEGRAM_BOT_TOKEN  токен бота (не нужен для --check)
  TELEGRAM_CHAT_ID    куда слать (не нужен для --check)
"""
import html
import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

# Коды пунктов 8-K -> короткое описание. Незнакомые коды показываются как есть.
ITEM_NAMES = {
    "1.01": "Material agreement",
    "1.02": "Agreement terminated",
    "1.03": "Bankruptcy",
    "1.05": "Cybersecurity incident",
    "2.01": "Acquisition / disposal of assets",
    "2.02": "Earnings results",
    "2.03": "New debt obligation",
    "3.02": "Unregistered share sale",
    "3.03": "Change in shareholder rights",
    "4.02": "Financials non-reliance",
    "5.01": "Change in control",
    "5.02": "Executives / directors change",
    "5.03": "Charter / bylaws change",
    "5.07": "Shareholder vote results",
    "7.01": "Reg FD disclosure",
    "8.01": "Other events",
    "9.01": "Financial statements / exhibits",
}


def sec_get(url, user_agent):
    r = requests.get(
        url,
        headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
        timeout=30,
    )
    r.raise_for_status()
    time.sleep(0.2)  # SEC разрешает до 10 запросов/сек, держим запас
    return r.json()


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def resolve_companies(companies, user_agent):
    """Возвращает (resolved, unresolved). resolved: список dict с name, ticker, cik."""
    tickers = sec_get(TICKERS_URL, user_agent)
    by_ticker = {v["ticker"].upper(): v for v in tickers.values()}
    resolved, unresolved = [], []
    for c in companies:
        ticker = c["ticker"].upper()
        if c.get("cik"):
            cik = int(c["cik"])
        elif ticker in by_ticker:
            cik = int(by_ticker[ticker]["cik_str"])
        else:
            unresolved.append(c)
            continue
        resolved.append({"name": c.get("name", ticker), "ticker": ticker, "cik": cik})
    return resolved, unresolved


def fetch_filings(cik, forms, user_agent):
    """Последние filings компании, отфильтрованные по типам форм. Старые первыми."""
    data = sec_get(SUBMISSIONS_URL.format(cik=cik), user_agent)
    recent = data["filings"]["recent"]
    n = len(recent["accessionNumber"])
    items = recent.get("items") or [""] * n
    out = []
    for i in range(n):
        if recent["form"][i] in forms:
            out.append(
                {
                    "accession": recent["accessionNumber"][i],
                    "form": recent["form"][i],
                    "date": recent["filingDate"][i],
                    "doc": recent["primaryDocument"][i],
                    "items": items[i],
                }
            )
    out.reverse()  # в API новые идут первыми
    return out


def filing_url(cik, f):
    acc = f["accession"].replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{f['doc']}"


def format_items(items):
    if not items:
        return ""
    parts = []
    for code in items.split(","):
        code = code.strip()
        name = ITEM_NAMES.get(code)
        parts.append(f"{name} ({code})" if name else code)
    return "; ".join(parts)


def format_message(company, f):
    lines = [
        f"<b>{html.escape(company['name'])} ({company['ticker']})</b> — {html.escape(f['form'])}",
        f["date"],
    ]
    items = format_items(f["items"])
    if items:
        lines.append(html.escape(items))
    lines.append(filing_url(company["cik"], f))
    return "\n".join(lines)


def telegram_send(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        },
        timeout=30,
    )
    r.raise_for_status()


def run_check(companies, forms, user_agent):
    resolved, unresolved = resolve_companies(companies, user_agent)
    print(f"Формы: {', '.join(forms)}")
    for c in resolved:
        try:
            n = len(fetch_filings(c["cik"], forms, user_agent))
            print(f"OK    {c['ticker']:6} CIK {c['cik']:>8}  {c['name']}  (filings в recent: {n})")
        except Exception as e:
            print(f"FAIL  {c['ticker']:6} CIK {c['cik']:>8}  {c['name']}  {e}")
    for c in unresolved:
        print(f"НЕТ   {c['ticker']:6} не найден в company_tickers.json  {c.get('name', '')}")


def run(companies, forms, user_agent):
    state = load_json(STATE_PATH, {})
    warned = set(state.get("_warned", []))
    resolved, unresolved = resolve_companies(companies, user_agent)

    # Один раз сообщаем в Telegram про тикеры, которых нет в SEC
    new_missing = [c for c in unresolved if c["ticker"].upper() not in warned]
    if new_missing:
        names = ", ".join(f"{c['ticker']}" for c in new_missing)
        telegram_send(f"SEC bot: не нашёл в SEC тикеры: {html.escape(names)}")
        warned.update(c["ticker"].upper() for c in new_missing)
    state["_warned"] = sorted(warned)

    sent = 0
    for c in resolved:
        key = str(c["cik"])
        try:
            filings = fetch_filings(c["cik"], forms, user_agent)
        except Exception as e:
            print(f"{c['ticker']}: ошибка запроса: {e}", file=sys.stderr)
            continue

        if key not in state:
            # Первый запуск для компании: запоминаем всё, что уже есть, и молчим
            state[key] = [f["accession"] for f in filings]
            print(f"{c['ticker']}: первичная загрузка, {len(filings)} filings запомнено")
            continue

        seen = set(state[key])
        for f in filings:
            if f["accession"] in seen:
                continue
            try:
                telegram_send(format_message(c, f))
            except Exception as e:
                # Не помечаем как увиденный, попробуем в следующий запуск
                print(f"{c['ticker']}: не отправилось {f['accession']}: {e}", file=sys.stderr)
                continue
            state[key].append(f["accession"])
            sent += 1

    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Готово, отправлено: {sent}")


def main():
    user_agent = os.environ.get("SEC_USER_AGENT")
    if not user_agent:
        sys.exit('Задай SEC_USER_AGENT, например "Artemy me@example.com"')

    config = load_json(CONFIG_PATH, None)
    if not config:
        sys.exit("Нет config.json")
    companies = config["companies"]
    forms = set(config.get("forms", ["8-K", "DEF 14A"]))

    if "--check" in sys.argv:
        run_check(companies, forms, user_agent)
    else:
        run(companies, forms, user_agent)


if __name__ == "__main__":
    main()
