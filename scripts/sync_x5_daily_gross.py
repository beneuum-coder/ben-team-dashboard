#!/usr/bin/env python3
"""Build or apply X5 daily MTD Gross snapshots for the published dashboard.

This is the portable copy of the established X5 sync.  Its CRM query logic is
kept intact; only the Lark transport uses environment-backed credentials so it
can run in GitHub Actions without a local Keychain profile.
"""
import argparse
import hashlib
import json
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pyotp
import requests

TABLE = "tbl9L956hdRmwd2g"
LOCAL_LARK_CONFIG = "/Users/x/.codex/lark-bitable-tools.json"
MSK = timezone(timedelta(hours=3))
SALES = ("Rita.EU", "Ben.Eu", "Kelly.Lin", "Elroy.Chuan")
DISPLAY = {"Rita.EU": "Rita", "Ben.Eu": "Ben", "Kelly.Lin": "Kelly", "Elroy.Chuan": "Elroy"}
BEN_UID, BEN_ORG = "964492", "73"


def crm_login():
    cookie = os.environ.get("CRM_COOKIE", "").removeprefix("Cookie:").strip()
    if cookie:
        return {"Cookie": cookie, "Content-Type": "application/json", "current-regulator": "SVG", "X-Requested-With": "XMLHttpRequest"}
    session = requests.Session()
    session.get("https://admin.ultimarkets.com/login/to_login", timeout=15)
    password = hashlib.md5(os.environ["CRM_PASS"].encode()).hexdigest()
    session.post("https://admin.ultimarkets.com/login/to_login", data={"userName_login": os.environ["CRM_USER"], "password_login": password, "utc": "28800000"}, timeout=15, allow_redirects=False)
    response = session.post("https://admin.ultimarkets.com/login/to_login", data={"userName_login": os.environ["CRM_USER"], "password_login": password, "twoFactorType": "googleAuth", "googleAuthTotp": pyotp.TOTP(os.environ["CRM_TOTP"]).now()}, timeout=15, allow_redirects=False)
    if response.status_code != 302:
        raise RuntimeError(f"CRM credential login failed (status {response.status_code})")
    return {"Cookie": f"JSESSIONID={session.cookies.get('JSESSIONID', '')}; jsId={session.cookies.get('jsId', '')}", "Content-Type": "application/json", "current-regulator": "SVG", "X-Requested-With": "XMLHttpRequest"}


def post(headers, path, payload, timeout=120):
    response = requests.post(f"https://admin.ultimarkets.com{path}", headers=headers, json=payload, timeout=timeout)
    response.raise_for_status()
    return response.json()


def team_members(headers):
    rows = post(headers, "/admin/query_adminListSimple", {"limit": 9999, "pageNo": 1, "offset": 0, "order": "asc", "search": {"directLevel": "5", "user_id": "", "org_id": "73", "search_type": "", "searchText": ""}}, 30).get("rows", [])
    found = {row.get("user_name"): (str(row.get("user_id")), str(row.get("org_id"))) for row in rows}
    found["Ben.Eu"] = (BEN_UID, BEN_ORG)
    missing = set(SALES) - set(found)
    if missing:
        raise RuntimeError(f"Missing current X5 members: {sorted(missing)}")
    return {name: found[name] for name in SALES}


def exchange_rates(headers):
    return post(headers, "/exchangeRate/data/getExchangeRateData", {}, 30).get("data", {})


def usd(value, currency, rates):
    value = float(value or 0)
    if currency == "USD":
        return value
    if currency == "USC":
        return value / 100
    return value * float(rates.get(f"{currency}2USD") or rates.get(f"{currency}2USDWITH") or 1)


def stats(headers, uid, org, start, end, rates):
    body = {"pageNo": None, "order": "asc", "search": {"directLevel": "3", "user_id": uid, "org_id": org, "searchType": "-1", "search": "", "agentuserQuery": "", "startDate": start, "endDate": end, "dataSourceId": "0", "dataType": "0", "accType": "1", "paymentMethod": "0", "countryCodes": ""}}
    rows = post(headers, "/report/view/depositAndWithdrawReportsStatistics", body).get("rows", [])
    gross = sum(usd(row.get("totalDepositCount"), row.get("totalCurrency", "USD"), rates) for row in rows)
    net = sum(usd(row.get("totalSum"), row.get("totalCurrency", "USD"), rates) for row in rows)
    return round(gross, 2), round(gross - net, 2), round(net, 2)


def ib_by_day(headers, month):
    start = f"{month}-01 00:00:00"
    end = f"{month}-{(date.fromisoformat(month + '-01').replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1):%d} 23:59:59"
    body = {"skipCount": True, "pagination": {"limit": 9999, "pageNo": None}, "parameters": {"approvedTime": {"filterType": "DATEPICKER", "input": {"startDate": start, "endDate": end}}, "is_archive": {"filterType": "SELECT", "input": "0"}, "directLevel": {"filterType": "CUSTOM", "input": "5"}, "user_id": {"filterType": "CUSTOM", "input": ""}, "org_id": {"filterType": "CUSTOM", "input": ""}}}
    out = defaultdict(lambda: defaultdict(lambda: {"master": 0, "sub": 0}))
    for row in post(headers, "/account/query_rebateAccountList", body).get("rows", []):
        if row.get("commissionType") == 0 or "CX 2287" in (row.get("cpa") or "") or "Internal Account" in (row.get("opNote") or "") or (row.get("clientNameIpt") or "").startswith("(CA)"):
            continue
        owner = row.get("ownerAlias")
        approved = (row.get("approvedTime") or "")[:10]
        if owner in SALES and approved:
            out[approved][owner]["master" if row.get("owner") == owner else "sub"] += 1
    return out


class LarkTransport:
    """Use the existing local CLI profile, or GitHub Actions environment Secrets."""

    def __init__(self):
        config_path = os.getenv("LARK_CONFIG", LOCAL_LARK_CONFIG)
        config = {}
        if os.path.isfile(config_path):
            config = (json.loads(Path(config_path).read_text(encoding="utf-8")).get("lark") or {})
        cli = config.get("lark_cli") or config.get("cli")
        self.app = os.getenv("LARK_APP_TOKEN") or config.get("app_token")
        self.cli_bin = cli.get("bin") if cli else None
        self.cli_profile = cli.get("profile") if cli else None
        self.tenant_token = None
        if not self.app:
            raise RuntimeError("Missing LARK_APP_TOKEN or Lark CLI config")

    def request(self, method, path, body=None):
        if self.cli_bin:
            command = [self.cli_bin]
            if self.cli_profile:
                command += ["--profile", self.cli_profile]
            command += ["api", method, f"/open-apis{path}", "--as", "bot", "--format", "json"]
            if body is not None:
                command += ["--data", json.dumps(body, ensure_ascii=False, separators=(",", ":"))]
            result = __import__("subprocess").run(command, check=True, capture_output=True, text=True)
            payload = json.loads(result.stdout)
        else:
            if not self.tenant_token:
                response = requests.post("https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal", json={"app_id": os.environ["LARK_APP_ID"], "app_secret": os.environ["LARK_APP_SECRET"]}, timeout=30)
                response.raise_for_status()
                token_payload = response.json()
                if token_payload.get("code") != 0:
                    raise RuntimeError("Unable to obtain Lark access token")
                self.tenant_token = token_payload["tenant_access_token"]
            response = requests.request(method, f"https://open.larksuite.com/open-apis{path}", headers={"Authorization": f"Bearer {self.tenant_token}", "Content-Type": "application/json"}, json=body, timeout=30)
            response.raise_for_status()
            payload = response.json()
        if payload.get("code") not in (0, None):
            raise RuntimeError(f"Lark API error: {payload.get('msg', payload.get('code'))}")
        return payload


def lark_records(lark):
    items, page_token = [], None
    while True:
        body = {"page_size": 500}
        if page_token:
            body["page_token"] = page_token
        data = lark.request("POST", f"/bitable/v1/apps/{lark.app}/tables/{TABLE}/records/search", body).get("data", {})
        items.extend(data.get("items", []))
        if not data.get("has_more"):
            return items
        page_token = data.get("page_token")


def write_plan(lark, creates, updates):
    for records, action in ((creates, "batch_create"), (updates, "batch_update")):
        for index in range(0, len(records), 100):
            lark.request("POST", f"/bitable/v1/apps/{lark.app}/tables/{TABLE}/records/{action}", {"records": records[index:index + 100]})


def date_key(value):
    return datetime.fromtimestamp(int(value) / 1000, MSK).date().isoformat() if value else ""


def export_latest(records, today, destination):
    selected = {}
    for record in records:
        fields = record.get("fields", {})
        name = fields.get("销售")
        if name in (*DISPLAY.values(), "Total") and date_key(fields.get("日期")) == today.isoformat():
            selected[name] = fields
    expected = {*DISPLAY.values(), "Total"}
    if set(selected) != expected:
        raise RuntimeError(f"X5 daily Lark read-back incomplete: expected {sorted(expected)}, got {sorted(selected)}")
    updated = max(int(row.get("更新时间") or 0) for row in selected.values())
    Path(destination).write_text(json.dumps({"date": today.isoformat(), "updatedAt": updated, "records": selected}, ensure_ascii=False), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", default=datetime.now(MSK).strftime("%Y-%m"))
    parser.add_argument("--mode", choices=("current", "full"), default="full", help="current writes only today's MTD snapshot; full recalibrates every day this month")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dashboard-export")
    args = parser.parse_args()
    today = datetime.now(MSK).date()
    if args.month != today.strftime("%Y-%m"):
        raise RuntimeError("Only the current CRM month may be synced")
    headers = crm_login()
    members = team_members(headers)
    rates, ib = exchange_rates(headers), ib_by_day(headers, args.month)
    lark = LarkTransport()
    existing = {(fields.get("销售"), date_key(fields.get("日期"))): record for record in lark_records(lark) if (fields := record.get("fields", {}))}
    all_days = []
    cursor = date(today.year, today.month, 1)
    while cursor <= today:
        all_days.append(cursor)
        cursor += timedelta(days=1)
    cumulative = {name: {"master": 0, "sub": 0} for name in SALES}
    creates, updates = [], []
    total_requests = len(SALES) * (1 if args.mode == "current" else len(all_days))
    completed_requests = 0
    print(f"mode={args.mode} phase=CRM MTD statistics requests=0/{total_requests}", flush=True)
    for day_index, day in enumerate(all_days, 1):
        day_s = day.isoformat()
        for name in SALES:
            cumulative[name]["master"] += ib[day_s][name]["master"]
            cumulative[name]["sub"] += ib[day_s][name]["sub"]
        if args.mode == "current" and day != today:
            continue
        end = datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S") if day == today else f"{day_s} 23:59:59"
        print(f"date={day_s} day={day_index}/{len(all_days)} phase=CRM MTD statistics", flush=True)
        values = {}
        for name in SALES:
            started = time.perf_counter()
            print(f"date={day_s} sales={DISPLAY[name]} phase=CRM request start", flush=True)
            values[name] = stats(headers, *members[name], f"{args.month}-01", end, rates)
            completed_requests += 1
            elapsed = time.perf_counter() - started
            print(f"date={day_s} sales={DISPLAY[name]} phase=CRM request done elapsed={elapsed:.2f}s requests={completed_requests}/{total_requests}", flush=True)
        ben = values["Ben.Eu"]
        others = tuple(round(sum(values[name][i] for name in SALES if name != "Ben.Eu"), 2) for i in range(3))
        values["Ben.Eu"] = tuple(round(ben[i] - others[i], 2) for i in range(3))
        for name in SALES + ("Total",):
            if name == "Total":
                gross, withdraw, net = (round(sum(values[x][i] for x in SALES), 2) for i in range(3))
                counts = {kind: sum(cumulative[x][kind] for x in SALES) for kind in ("master", "sub")}
            else:
                gross, withdraw, net, counts = *values[name], cumulative[name]
            fields = {"销售": DISPLAY.get(name, name), "日期": int(datetime.combine(day, datetime.min.time(), MSK).timestamp() * 1000), "入金(USD)": gross, "出金(USD)": withdraw, "净入金(USD)": net, "Master IB": counts["master"], "Sub IB": counts["sub"], "更新时间": int(time.time() * 1000)}
            record = existing.get((fields["销售"], day_s))
            (updates if record else creates).append({"record_id": record["record_id"], "fields": fields} if record else {"fields": fields})
    print(json.dumps({"create": len(creates), "update": len(updates), "delete": 0}, ensure_ascii=False))
    if not args.apply:
        return
    print("phase=Lark snapshot write", flush=True)
    write_plan(lark, creates, updates)
    if args.dashboard_export:
        print("phase=Lark snapshot read-back and dashboard export", flush=True)
        export_latest(lark_records(lark), today, args.dashboard_export)


if __name__ == "__main__":
    main()
