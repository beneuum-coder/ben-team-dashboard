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
import stat
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


def cx_date(moment):
    """CellXpert wants M/D/YYYY without leading zeros.

    The original code used ``strftime("%-m/%-d/%Y")``, which is a glibc
    extension and raises ValueError on Windows.
    """
    return f"{moment.month}/{moment.day}/{moment.year}"


def crm_login():
    cookie = os.environ.get("CRM_COOKIE", "").removeprefix("Cookie:").strip()
    cookie_file = os.environ.get("CRM_COOKIE_FILE", "").strip()
    if not cookie and cookie_file and os.path.exists(cookie_file):
        file_stat = os.stat(cookie_file, follow_symlinks=False)
        # Windows has no POSIX permission bits; st_mode there reports 0o666 for
        # ordinary files, which would fail this check on every valid cookie.
        posix = os.name == "posix"
        if not stat.S_ISREG(file_stat.st_mode) or (posix and file_stat.st_mode & 0o077):
            raise RuntimeError("CRM session file must be a private regular file")
        cookie = Path(cookie_file).read_text(encoding="utf-8").strip()
        if not cookie:
            raise RuntimeError("CRM session file is empty")
    if cookie:
        return {"Cookie": cookie, "Content-Type": "application/json", "current-regulator": "SVG", "X-Requested-With": "XMLHttpRequest"}
    session = requests.Session()
    landing = session.get("https://admin.ultimarkets.com/login/to_login", timeout=15, stream=True)
    try:
        if landing.status_code == 403 and landing.headers.get("cf-mitigated") == "challenge":
            raise RuntimeError("CRM Cloudflare security challenge blocked automated access")
        landing.raise_for_status()
    finally:
        landing.close()
    password = hashlib.md5(os.environ["CRM_PASS"].encode()).hexdigest()
    first_factor = session.post("https://admin.ultimarkets.com/login/to_login", data={"userName_login": os.environ["CRM_USER"], "password_login": password, "utc": "28800000"}, timeout=15, allow_redirects=False)
    if first_factor.status_code == 403 and first_factor.headers.get("cf-mitigated") == "challenge":
        raise RuntimeError("CRM Cloudflare security challenge blocked automated access")
    first_factor.raise_for_status()
    response = session.post("https://admin.ultimarkets.com/login/to_login", data={"userName_login": os.environ["CRM_USER"], "password_login": password, "twoFactorType": "googleAuth", "googleAuthTotp": pyotp.TOTP(os.environ["CRM_TOTP"]).now()}, timeout=15, allow_redirects=False)
    if response.status_code == 403 and response.headers.get("cf-mitigated") == "challenge":
        raise RuntimeError("CRM Cloudflare security challenge blocked automated access")
    if response.status_code != 302:
        raise RuntimeError(f"CRM credential login failed (status {response.status_code})")
    session_id = session.cookies.get("JSESSIONID")
    js_id = session.cookies.get("jsId")
    if not session_id or not js_id:
        raise RuntimeError("CRM login did not return both session cookies")
    cookie = f"JSESSIONID={session_id}; jsId={js_id}"
    if cookie_file:
        fd = os.open(cookie_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(cookie)
    return {"Cookie": cookie, "Content-Type": "application/json", "current-regulator": "SVG", "X-Requested-With": "XMLHttpRequest"}


RETRY_DELAYS = (3, 10, 20)
RETRYABLE_STATUS = {408, 425, 429, 502, 503, 504, 520, 522, 524}


def post(headers, path, payload, timeout=120):
    # These CRM POST endpoints are read-only queries. Retry transient timeouts and
    # Cloudflare edge errors (520/522/524) with backoff, but never retry an
    # authorization failure or a security challenge.
    for attempt in range(len(RETRY_DELAYS) + 1):
        delay = RETRY_DELAYS[attempt] if attempt < len(RETRY_DELAYS) else None
        try:
            response = requests.post(f"https://admin.ultimarkets.com{path}", headers=headers, json=payload, timeout=timeout)
            if response.status_code == 403 and response.headers.get("cf-mitigated") == "challenge":
                raise RuntimeError("CRM Cloudflare security challenge blocked automated access")
            if response.status_code in RETRYABLE_STATUS and delay is not None:
                response.close()
                print(f"CRM read retry path={path} reason=HTTP {response.status_code}", file=sys.stderr, flush=True)
                time.sleep(delay)
                continue
            response.raise_for_status()
            return response.json()
        except (requests.Timeout, requests.ConnectionError) as exc:
            if delay is None:
                raise
            print(f"CRM read retry path={path} reason={type(exc).__name__}", file=sys.stderr, flush=True)
            time.sleep(delay)
    raise RuntimeError(f"CRM read failed after {len(RETRY_DELAYS) + 1} attempts: {path}")


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


def _norm(value):
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def contribution_rows(headers, members, month, rates, today):
    """Per-source MTD deposits: IB channels, one CPA bucket, and direct clients by name.

    Ben's CRM account is the team director, so his client report already contains
    every other salesperson's clients; subtract those client ids before bucketing.
    """
    start = f"{month}-01"
    end = today.isoformat()
    collected = {}
    for name in SALES:
        uid, org = members[name]
        items, page = [], 1
        while True:
            body = {"limit": 500, "pageNo": page, "offset": (page - 1) * 500, "order": "asc",
                    "search": {"directLevel": "3", "user_id": uid, "org_id": org, "searchType": "-1",
                               "search": "", "agentuserQuery": "", "startDate": start, "endDate": end,
                               "dataSourceId": "0", "dataType": "0", "accType": "1", "paymentMethod": "0",
                               "countryCodes": ""}}
            chunk = post(headers, "/report/view/depositAndWithdrawReports", body, 180).get("rows", [])
            items += chunk
            if len(chunk) < 500:
                break
            page += 1
        collected[name] = items
    claimed = set()
    for name in SALES:
        if name != "Ben.Eu":
            claimed |= {str(row.get("userId")) for row in collected[name]}
    rows = []
    for name in SALES:
        alias = _norm(name)
        buckets = defaultdict(float)
        for row in collected[name]:
            if name == "Ben.Eu" and str(row.get("userId")) in claimed:
                continue
            if str(row.get("dataType")) != "1":
                continue
            amount = usd(row.get("depositCount"), row.get("currency", "USD"), rates)
            if not amount:
                continue
            ib = str(row.get("ibName") or "").strip()
            key = _norm(ib)
            if key.endswith("cpa"):
                buckets[("CPA", "CPA")] += amount
            elif not key or key == alias or key == _norm(DISPLAY[name]):
                buckets[("直客", str(row.get("userName") or "客户").strip())] += amount
            else:
                buckets[("IB", ib)] += amount
        for (kind, label), amount in buckets.items():
            if amount <= 0.005:
                continue
            rows.append({"sales": DISPLAY[name], "sourceName": label, "sourceType": kind,
                         "sourceId": "", "gross": round(amount, 2)})
    return rows


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


def lark_records(lark, table=TABLE):
    items, page_token = [], None
    while True:
        body = {"page_size": 500}
        if page_token:
            body["page_token"] = page_token
        data = lark.request("POST", f"/bitable/v1/apps/{lark.app}/tables/{table}/records/search", body).get("data", {})
        items.extend(data.get("items", []))
        if not data.get("has_more"):
            return items
        page_token = data.get("page_token")
        if not page_token:
            raise RuntimeError(f"Lark pagination ended without a page token for table {table}")


def lark_fields(lark, table=TABLE):
    return lark.request("GET", f"/bitable/v1/apps/{lark.app}/tables/{table}/fields").get("data", {}).get("items", [])


def write_plan(lark, creates, updates):
    for records, action in ((creates, "batch_create"), (updates, "batch_update")):
        for index in range(0, len(records), 100):
            lark.request("POST", f"/bitable/v1/apps/{lark.app}/tables/{TABLE}/records/{action}", {"records": records[index:index + 100]})


# Only these fields carry data. 更新时间 is bookkeeping: rewriting it on every
# run is what turned a no-op refresh into a Lark write.
COMPARED_FIELDS = ("入金(USD)", "出金(USD)", "净入金(USD)", "Master IB", "Sub IB")


def same_number(left, right):
    try:
        return round(float(left), 2) == round(float(right), 2)
    except (TypeError, ValueError):
        return left == right


def date_key(value):
    if not value:
        return ""
    if isinstance(value, str) and len(value) >= 10 and value[4:5] == "-":
        return value[:10]
    return datetime.fromtimestamp(int(value) / 1000, MSK).date().isoformat()


def text_value(value):
    """Normalize Lark text fields, which are returned as rich-text fragments."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(str(item.get("text", "")) for item in value if isinstance(item, dict))
    if isinstance(value, dict):
        return str(value.get("text", ""))
    return "" if value is None else str(value)


def select_day_records(records, today):
    selected, duplicates = {}, {}
    for record in records:
        fields = record.get("fields", {})
        name = text_value(fields.get("销售"))
        if name not in (*DISPLAY.values(), "Total") or date_key(fields.get("日期")) != today.isoformat():
            continue
        if name in selected:
            duplicates.setdefault(name, []).append(record["record_id"])
        selected[name] = fields
    return selected, duplicates


def export_latest(records, today, destination, contributions=None):
    selected, duplicates = select_day_records(records, today)
    expected = {*DISPLAY.values(), "Total"}
    if duplicates:
        raise RuntimeError(f"X5 daily Lark read-back has duplicate sales records: {duplicates}")
    if set(selected) != expected:
        raise RuntimeError(f"X5 daily Lark read-back incomplete: expected {sorted(expected)}, got {sorted(selected)}")
    updated = max(int(row.get("更新时间") or 0) for row in selected.values())
    payload = {"date": today.isoformat(), "updatedAt": updated, "records": selected}
    if contributions is not None:
        payload["contributions"] = contributions
    Path(destination).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def export_after_readback(lark, today, destination, contributions=None):
    fields = {field.get("field_name"): field.get("type") for field in lark_fields(lark)}
    print(
        f"phase=Lark read-back query date={today.isoformat()} "
        f"date_field_type={fields.get('日期')} query=client-side-full-table",
        flush=True,
    )
    for attempt, delay in enumerate((2, 4, 6, 8, 10), 1):
        time.sleep(delay)
        records = lark_records(lark)
        selected, duplicates = select_day_records(records, today)
        print(
            f"read-back attempt {attempt}/5 records={len(records)} "
            f"sales={sorted(selected)} duplicates={sorted(duplicates)}",
            flush=True,
        )
        if not duplicates and set(selected) == {*DISPLAY.values(), "Total"}:
            export_latest(records, today, destination, contributions)
            return
    raise RuntimeError(
        f"X5 daily Lark read-back incomplete after 5 attempts: "
        f"expected {sorted((*DISPLAY.values(), 'Total'))}, got {sorted(selected)}"
    )


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
    existing = {(text_value(fields.get("销售")), date_key(fields.get("日期"))): record for record in lark_records(lark) if (fields := record.get("fields", {}))}
    all_days = []
    cursor = date(today.year, today.month, 1)
    while cursor <= today:
        all_days.append(cursor)
        cursor += timedelta(days=1)
    cumulative = {name: {"master": 0, "sub": 0} for name in SALES}
    creates, updates, unchanged = [], [], 0
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
            if not record:
                creates.append({"fields": fields})
                continue
            current = record.get("fields", {})
            if all(same_number(current.get(key), fields[key]) for key in COMPARED_FIELDS):
                unchanged += 1
                continue
            updates.append({"record_id": record["record_id"], "fields": fields})
    print(json.dumps({"create": len(creates), "update": len(updates), "unchanged": unchanged, "delete": 0}, ensure_ascii=False))
    if not args.apply:
        return
    if creates or updates:
        print("phase=Lark snapshot write", flush=True)
        write_plan(lark, creates, updates)
    else:
        print("phase=Lark snapshot write skipped: every row is already current", flush=True)
    if args.dashboard_export:
        print("phase=Lark snapshot read-back and dashboard export", flush=True)
        try:
            contributions = contribution_rows(headers, members, args.month, rates, today)
            print(f"phase=CRM contribution sources rows={len(contributions)}", flush=True)
        except Exception as exc:
            print(f"warning=contribution sources unavailable: {type(exc).__name__}: {exc}", flush=True)
            contributions = None
        export_after_readback(lark, today, args.dashboard_export, contributions)


if __name__ == "__main__":
    main()
