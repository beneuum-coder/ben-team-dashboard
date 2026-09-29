#!/usr/bin/env python3
"""Preview or upsert one sales owner's CellXpert channel month into Lark.

The established CRM login and request helpers are reused unchanged.  CRM does
not presently expose a CellXpert AffiliateID on its channel-level reports, so
this first-stage sync deliberately refuses to allocate salesperson totals to
individual channels.  Per-channel financial metrics come from CellXpert's
monthly process report; the CRM probe is retained to make that missing join
explicit rather than silently mixing incompatible sources.
"""
import argparse
import json
import os
from datetime import date, datetime, timedelta, timezone

import requests

from sync_x5_daily_gross import LarkTransport, crm_login, lark_records, post, team_members


TABLE = "tblOJxbrBo7zs997"
SHANGHAI = timezone(timedelta(hours=8))
SALES = {
    "Rita": {"crm": "Rita.EU", "cellxpert_manager": "RitaEU"},
}
REQUIRED_FIELDS = {
    "月份", "销售", "渠道ID", "IB/CPA账户名称", "类型", "Registration",
    "FTD", "Gross Deposit", "Withdrawal", "Net", "更新时间",
}


def numeric(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def text(value):
    if isinstance(value, list):
        return "".join(text(item) for item in value)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or "")
    return "" if value is None else str(value)


def lark_day(value):
    if not value:
        return ""
    if isinstance(value, str) and len(value) >= 10:
        return value[:10]
    return datetime.fromtimestamp(int(value) / 1000, SHANGHAI).date().isoformat()


def cellxpert_headers():
    response = requests.post(
        "https://go.ultimamarkets.com/adminlogin/loginadmin.asp",
        data={"command": "logon", "user": os.environ["CX_USER"], "password": os.environ["CX_PASS"], "json": 1},
        headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30,
    )
    response.raise_for_status()
    token = response.json().get("message")
    if not token:
        raise RuntimeError("CellXpert login returned no access token")
    return {"Authorization": f"Bearer {token}", "admin_url": "Ultimarkets"}


def cellxpert_get(headers, command, **params):
    response = requests.get(
        "https://adminapi.cellxpert.com/", params={"command": command, "json": 1, **params},
        headers=headers, timeout=60,
    )
    response.raise_for_status()
    return response.json()


def crm_channel_probe(sales, month):
    """Use the existing CRM account request without changing its logic.

    This is evidence only.  Its records identify newly approved customer/IB
    relationships but have no CellXpert AffiliateID, so they cannot be used to
    split month-to-date money between CellXpert channels.
    """
    headers = crm_login()
    uid, org = team_members(headers)[SALES[sales]["crm"]]
    last_day = (date.fromisoformat(f"{month}-01").replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    payload = {
        "skipCount": True, "pagination": {"limit": 9999, "pageNo": None},
        "parameters": {
            "approvedTime": {"filterType": "DATEPICKER", "input": {"startDate": f"{month}-01 00:00:00", "endDate": f"{last_day:%Y-%m-%d} 23:59:59"}},
            "is_archive": {"filterType": "SELECT", "input": "0"},
            "directLevel": {"filterType": "CUSTOM", "input": "5"},
            "user_id": {"filterType": "CUSTOM", "input": uid},
            "org_id": {"filterType": "CUSTOM", "input": org},
        },
    }
    rows = post(headers, "/account/query_rebateAccountList", payload).get("rows", [])
    owned = [row for row in rows if row.get("ownerAlias") == SALES[sales]["crm"]]
    return {
        "newly_approved_rows": len(owned),
        "crm_rebate_account_ids": sorted({str(row.get("rebateAccount")) for row in owned if row.get("rebateAccount")}),
    }


def cellxpert_channels(sales, month):
    headers = cellxpert_headers()
    affiliates = cellxpert_get(headers, "browseaffiliatesjson").get("ManageAffiliatesData", [])
    manager = SALES[sales]["cellxpert_manager"]
    active = [row for row in affiliates if str(row.get("AffiliateManager", "")).lower() == manager.lower() and str(row.get("Status", "")).lower() == "approved"]
    start = datetime.fromisoformat(f"{month}-01").strftime("%-m/%-d/%Y")
    end = datetime.now(SHANGHAI).strftime("%-m/%-d/%Y")
    report = cellxpert_get(headers, "processreport", Affiliate="true", BTA="true", startDate=start, endDate=end, uniqueId=1)
    if not isinstance(report, list):
        raise RuntimeError("CellXpert processreport returned an invalid payload")
    by_id = {str(row.get("BTA")): row for row in report if row.get("BTA") is not None}
    rows, warnings = [], []
    for affiliate in active:
        channel_id = str(affiliate.get("AffiliateID") or "")
        if not channel_id:
            raise RuntimeError("Approved CellXpert affiliate is missing AffiliateID")
        report_row = by_id.get(channel_id)
        if report_row is None:
            warnings.append(f"missing_current_month_processreport channel_id={channel_id}")
        metrics = report_row or {}
        rows.append({
            "month": month,
            "sales": sales,
            "channelId": channel_id,
            "accountName": text(affiliate.get("Username")),
            # CellXpert's Affiliate directory has no reliable IB/CPA category.
            "type": None,
            "registration": numeric(metrics.get("Registrations")),
            "ftd": numeric(metrics.get("FTD")),
            "grossDeposit": numeric(metrics.get("Deposits")),
            # CellXpert returns withdrawals as negative values; Lark stores an outflow magnitude.
            "withdrawal": abs(numeric(metrics.get("Withdrawals"))) if numeric(metrics.get("Withdrawals")) is not None else None,
            "net": numeric(metrics.get("Net_Deposits")),
            "sources": {
                "channelId": "CellXpert AffiliateID", "accountName": "CellXpert Username",
                "type": "unavailable: no confirmed CRM/CellXpert classification",
                "registration": "CellXpert processreport.Registrations", "ftd": "CellXpert processreport.FTD",
                "grossDeposit": "CellXpert processreport.Deposits", "withdrawal": "CellXpert processreport.Withdrawals",
                "net": "CellXpert processreport.Net_Deposits",
            },
        })
    return rows, warnings


def lark_payload(row, now):
    fields = {
        "月份": int(datetime.fromisoformat(f"{row['month']}-01").replace(tzinfo=SHANGHAI).timestamp() * 1000),
        "销售": row["sales"], "渠道ID": row["channelId"], "IB/CPA账户名称": row["accountName"],
        "更新时间": int(now.timestamp() * 1000),
    }
    # Explicit nulls clear previously published values when a source row is
    # absent, rather than retaining a stale month-to-date number.
    for field, key in (("Registration", "registration"), ("FTD", "ftd"), ("Gross Deposit", "grossDeposit"), ("Withdrawal", "withdrawal"), ("Net", "net")):
        fields[field] = row[key]
    fields["类型"] = row["type"]
    return fields


def upsert_plan(lark, rows, now):
    field_names = {field.get("field_name") for field in lark.request("GET", f"/bitable/v1/apps/{lark.app}/tables/{TABLE}/fields").get("data", {}).get("items", [])}
    missing = REQUIRED_FIELDS - field_names
    if missing:
        raise RuntimeError(f"X5渠道月度表现 schema missing fields: {sorted(missing)}")
    existing = lark_records(lark, TABLE)
    by_key = {}
    for record in existing:
        fields = record.get("fields", {})
        key = (lark_day(fields.get("月份"))[:7], text(fields.get("销售")), text(fields.get("渠道ID")))
        fallback = (key[0], key[1], text(fields.get("IB/CPA账户名称")))
        lookups = []
        if key[2]:
            lookups.append(key)
        if fallback[2] and fallback != key:
            lookups.append(fallback)
        for lookup in lookups:
            if lookup in by_key:
                raise RuntimeError(f"Duplicate existing Lark channel key: {lookup}")
            by_key[lookup] = record["record_id"]
    creates, updates = [], []
    for row in rows:
        key = (row["month"], row["sales"], row["channelId"])
        fallback = (row["month"], row["sales"], row["accountName"])
        record_id = by_key.get(key) or by_key.get(fallback)
        payload = {"fields": lark_payload(row, now)}
        if record_id:
            updates.append({"record_id": record_id, **payload})
        else:
            creates.append(payload)
    return creates, updates


def write(lark, action, records):
    for index in range(0, len(records), 100):
        lark.request("POST", f"/bitable/v1/apps/{lark.app}/tables/{TABLE}/records/{action}", {"records": records[index:index + 100]})


def verify_readback(lark, rows):
    records = lark_records(lark, TABLE)
    for row in rows:
        matches = [record for record in records if (
            lark_day(record.get("fields", {}).get("月份"))[:7],
            text(record.get("fields", {}).get("销售")),
            text(record.get("fields", {}).get("渠道ID")),
        ) == (row["month"], row["sales"], row["channelId"])]
        if len(matches) != 1:
            raise RuntimeError(f"Lark upsert read-back expected one record for channel {row['channelId']}, got {len(matches)}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sales", default="Rita", choices=sorted(SALES))
    parser.add_argument("--month", default=datetime.now(SHANGHAI).strftime("%Y-%m"))
    parser.add_argument("--apply", action="store_true", help="Apply the printed create/update plan to Lark")
    args = parser.parse_args()
    if args.month != datetime.now(SHANGHAI).strftime("%Y-%m"):
        raise RuntimeError("This first-stage verifier supports the current month only")
    crm_probe = crm_channel_probe(args.sales, args.month)
    rows, warnings = cellxpert_channels(args.sales, args.month)
    crm_probe["matching_cellxpert_channel_ids"] = sorted(
        set(crm_probe["crm_rebate_account_ids"]) & {row["channelId"] for row in rows}
    )
    now = datetime.now(timezone.utc)
    lark = LarkTransport()
    creates, updates = upsert_plan(lark, rows, now)
    result = {"month": args.month, "sales": args.sales, "crmProbe": crm_probe, "channels": rows, "warnings": warnings, "plan": {"create": len(creates), "update": len(updates)}}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.apply:
        write(lark, "batch_create", creates)
        write(lark, "batch_update", updates)
        verify_readback(lark, rows)
        print(json.dumps({"applied": {"create": len(creates), "update": len(updates)}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
