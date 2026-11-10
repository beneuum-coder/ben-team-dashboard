"""Estimate the monthly net-deposit bonus from CRM client deposits.

Base = average net deposits of the latest three months (IB + direct clients,
CPA-suffixed clients excluded). Tier table from the finance doc:
1.5% up to 200k, then 1.75/2/2.25/2.5/3%.
Results are estimates; finance settles one month later with its own data.
"""
import calendar, json, time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sync_x5_daily_gross import DISPLAY, SALES, crm_login, exchange_rates, post, team_members, usd

TZ = timezone(timedelta(hours=8))
CACHE = Path(__file__).resolve().parent.parent / "work" / "income-cache"
TIERS = ((200000, 0.015), (500000, 0.0175), (1000000, 0.02),
         (2000000, 0.0225), (3000000, 0.025), (10 ** 12, 0.03))


def norm(value):
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def tier_rate(value):
    for cap, rate in TIERS:
        if value <= cap:
            return rate
    return 0.03


def previous_months(label, count):
    year, month = (int(part) for part in label.split("-"))
    out = []
    for _ in range(count):
        month -= 1
        if month == 0:
            month, year = 12, year - 1
        out.append(f"{year}-{month:02d}")
    return out[::-1]


def _fetch(headers, uid, start, end):
    rows, page = [], 1
    while True:
        body = {"limit": 500, "pageNo": page, "offset": (page - 1) * 500, "order": "asc",
                "search": {"directLevel": "3", "user_id": uid, "org_id": "73", "searchType": "-1",
                           "search": "", "agentuserQuery": "", "startDate": start, "endDate": end,
                           "dataSourceId": "0", "dataType": "0", "accType": "1", "paymentMethod": "0",
                           "countryCodes": ""}}
        chunk = post(headers, "/report/view/depositAndWithdrawReports", body, 180).get("rows", [])
        rows += chunk
        if len(chunk) < 500:
            break
        page += 1
    return rows


def _month_net(headers, members, rates, label, current):
    """Return {sales: ib_net} for one month, cached once the month is closed."""
    cache = CACHE / f"{label}.json"
    if cache.exists() and label != current:
        return json.loads(cache.read_text(encoding="utf-8"))
    year, month = (int(part) for part in label.split("-"))
    start, end = f"{label}-01", f"{label}-{calendar.monthrange(year, month)[1]:02d}"
    collected = {name: _fetch(headers, members[name][0], start, end) for name in SALES}
    claimed = {str(row.get("userId")) for name in SALES if name != "Ben.Eu"
               for row in collected[name]}
    result = {}
    for name in SALES:
        alias = norm(name)
        total = 0.0
        for row in collected[name]:
            if name == "Ben.Eu" and str(row.get("userId")) in claimed:
                continue
            key = norm(row.get("ibName"))
            if key.endswith("cpa"):
                continue
            amount = (usd(row.get("depositCount"), row.get("currency", "USD"), rates)
                      if str(row.get("dataType")) == "1"
                      else -usd(row.get("withdrawCount"), row.get("currency", "USD"), rates))
            total += amount
        result[DISPLAY[name]] = round(total, 2)
    CACHE.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


def build(now=None):
    now = now or datetime.now(TZ)
    current = now.strftime("%Y-%m")
    window = previous_months(current, 2) + [current]
    try:
        headers, members, rates = crm_login(), None, None
        members = team_members(headers)
        rates = exchange_rates(headers)
        nets = {label: _month_net(headers, members, rates, label, current) for label in window}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    rows = []
    for name in ("Rita", "Ben", "Kelly", "Elroy"):
        values = [nets[label].get(name, 0.0) for label in window]
        average = round(sum(values) / 3, 2)
        rate = tier_rate(max(average, 0))
        rows.append({"name": name, "months": values, "average": average,
                     "rate": rate, "bonus": round(max(average, 0) * rate, 2)})
    return {"month": current, "window": window, "rows": rows,
            "note": "按近3月平均净入金×档位预估；财务实际结算滞后一个月且可能含调整"}
