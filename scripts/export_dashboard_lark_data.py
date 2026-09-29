#!/usr/bin/env python3
"""Read every Lark table rendered by the dashboard into one fail-closed export.

CRM/X5 calculations intentionally remain in sync_x5_daily_gross.py.  This
module is read-only: it normalizes the existing Lark tables into the current
browser data model and verifies that the separately verified X5 snapshot is
also present in Lark before publication.
"""
import argparse
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sync_x5_daily_gross import DISPLAY, LarkTransport, lark_fields, lark_records, select_day_records


TEAM_MANAGEMENT = "tblorZ8JzhmicRfB"
TEAM_SUMMARY = "tblUn0uqcKU7KOVF"
X5_DAILY = "tbl9L956hdRmwd2g"
PEOPLE = ("Rita", "Kelly", "Elroy", "Lulu", "Ben")
X5_NAMES = ("Rita", "Ben", "Kelly", "Elroy")
CUSTOMER_TABLES = {
    "Rita": "tblEs42D58J01UiP",
    "Kelly": "tblZQ5wUp9Ye5PoP",
    "Elroy": "tblpvf9wfb8q7Cnc",
    "Lulu": "tbloccPrqyZi8X7U",
}
SCHEDULE_TABLES = {
    "Rita": "tblFMjutPvKZr9Cd",
    "Kelly": "tblkPqJAdmP4GR2b",
    "Elroy": "tbl9mg6KjNlD3VDS",
    "Lulu": "tblUIGtewqUilEsf",
}
SHANGHAI = timezone(timedelta(hours=8))


def text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(text(item) for item in value)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or value.get("value") or "")
    return "" if value is None else str(value)


def number(value, default=0.0):
    if isinstance(value, dict):
        value = value.get("value", value)
    if isinstance(value, list):
        value = value[0] if value else default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def lark_date(value):
    """Decode native Lark Date fields using the Base's Asia/Shanghai calendar."""
    if not value:
        return ""
    if isinstance(value, str) and len(value) >= 10 and value[4:5] == "-":
        return value[:10]
    return datetime.fromtimestamp(int(value) / 1000, SHANGHAI).date().isoformat()


def nullable_number(value):
    if value is None or value == "":
        return None
    return number(value)


def field_names(lark, table):
    return {field.get("field_name") for field in lark_fields(lark, table)}


def load_table(lark, table, label, required_fields):
    names = field_names(lark, table)
    missing = set(required_fields) - names
    if missing:
        raise RuntimeError(f"{label} schema missing fields: {sorted(missing)}")
    records = lark_records(lark, table)
    if not isinstance(records, list):
        raise RuntimeError(f"{label} returned an invalid records payload")
    print(f"dashboard table={label} records={len(records)}", flush=True)
    return records


def load_schedule_table(lark, table, label):
    names = field_names(lark, table)
    required = {"时间", "解决思路"}
    missing = required - names
    if missing or not {"新增IB/CPA", "新增IB/Cpa"}.intersection(names):
        raise RuntimeError(f"{label} schema missing required schedule fields")
    records = lark_records(lark, table)
    if not isinstance(records, list):
        raise RuntimeError(f"{label} returned an invalid records payload")
    print(f"dashboard table={label} records={len(records)}", flush=True)
    return records


def month_label(value):
    key = lark_date(value)
    return key[:7] if key else ""


def month_index(hire, now):
    if not hire:
        return None
    try:
        start = datetime.fromisoformat(hire).date()
    except ValueError:
        return None
    return min(6, max(1, (now.year - start.year) * 12 + now.month - start.month + 1))


def current_month_rows(records, month):
    rows = {}
    for record in records:
        fields = record.get("fields", {})
        name = text(fields.get("销售"))
        if name and month_label(fields.get("月份")) == month:
            if name in rows:
                raise RuntimeError(f"团队数据汇总 has duplicate current-month rows for {name}")
            rows[name] = fields
    return rows


def current_value(fields, name):
    return nullable_number(fields.get(name)) if fields else None


def sum_schedule_leads(rows):
    ordered = sorted(rows, key=lambda row: row["date"], reverse=True)[:30]
    return int(sum(number(row["lead"]) for row in ordered))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--x5-export", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    x5_export = json.loads(Path(args.x5_export).read_text(encoding="utf-8"))
    expected_x5 = {*DISPLAY.values(), "Total"}
    if set(x5_export.get("records", {})) != expected_x5:
        raise RuntimeError("Refusing to build Dashboard export from an incomplete X5 export")
    export_date = str(x5_export.get("date") or "")
    if len(export_date) != 10:
        raise RuntimeError("X5 export is missing its snapshot date")

    lark = LarkTransport()
    management = load_table(lark, TEAM_MANAGEMENT, "团队管理汇总", {"销售", "入职时间"})
    summary = load_table(lark, TEAM_SUMMARY, "团队数据汇总", {"销售", "月份", "Monthly Gross", "Monthly Net", "Master IB", "Sub IB", "CPA", "Registration", "FTD", "月IB/CPA数量", "月Gross", "当月入金完成度"})
    customer_records = {name: load_table(lark, table, f"{name}客户记录表", {"时间", "姓名", "国家", "联系方式", "来源", "客户类型", "联系渠道", "意向度", "转化", "记录"}) for name, table in CUSTOMER_TABLES.items()}
    schedule_records = {name: load_schedule_table(lark, table, f"{name}日程表") for name, table in SCHEDULE_TABLES.items()}
    x5_records = load_table(lark, X5_DAILY, "X5日度Gross", {"销售", "日期", "入金(USD)", "出金(USD)", "净入金(USD)", "Master IB", "Sub IB", "更新时间"})
    x5_today, x5_duplicates = select_day_records(x5_records, datetime.fromisoformat(export_date).date())
    if x5_duplicates or set(x5_today) != expected_x5:
        raise RuntimeError(f"X5日度Gross does not contain one verified snapshot for {export_date}")

    now = datetime.now(timezone.utc)
    month = export_date[:7]
    hires = {}
    for record in management:
        fields = record.get("fields", {})
        name = text(fields.get("销售"))
        if name:
            hires[name] = lark_date(fields.get("入职时间"))
    monthly = current_month_rows(summary, month)
    for name in PEOPLE:
        if name not in monthly:
            print(f"warning=missing_current_monthly_record sales={name} month={month}", flush=True)

    schedules = defaultdict(list)
    issues = []
    for owner, records in schedule_records.items():
        for record in records:
            fields = record.get("fields", {})
            day = lark_date(fields.get("时间"))
            if not day:
                continue
            lead = number(fields.get("新增意向", fields.get("当日新增意向", 0)))
            ib = number(fields.get("新增IB/CPA", fields.get("新增IB/Cpa", 0)))
            row = {"name": owner, "date": day, "problem": text(fields.get("今日复盘问题") or fields.get("今日复盘总结")), "idea": text(fields.get("解决思路")), "lead": int(lead), "ib": int(ib)}
            schedules[owner].append(row)
            issues.append(row)
    issues.sort(key=lambda row: (row["date"], row["name"]), reverse=True)

    customers = []
    for owner, records in customer_records.items():
        for record in records:
            fields = record.get("fields", {})
            customers.append({
                "date": lark_date(fields.get("时间")), "name": text(fields.get("姓名")) or owner,
                "country": text(fields.get("国家")), "contact": text(fields.get("联系方式")),
                "source": text(fields.get("来源")), "type": text(fields.get("客户类型")),
                "channel": text(fields.get("联系渠道")), "intent": text(fields.get("意向度")),
                "converted": text(fields.get("转化")), "note": text(fields.get("记录")),
            })
    customers.sort(key=lambda row: (row["date"], row["name"]), reverse=True)

    history = defaultdict(dict)
    team_history = {}
    for record in summary:
        fields = record.get("fields", {})
        name, label = text(fields.get("销售")), month_label(fields.get("月份"))
        if not name or not label:
            continue
        row = {
            "label": label,
            "lead": 0,
            "ib": int(number(fields.get("Master IB"))) + int(number(fields.get("Sub IB"))) + int(number(fields.get("CPA"))),
            "gross": number(fields.get("Monthly Gross")),
        }
        if name == "Total":
            team_history[label] = {"label": label, "gross": row["gross"], "actualIB": row["ib"]}
        elif name in PEOPLE:
            history[name][label] = row
    for name, rows in schedules.items():
        month_leads = defaultdict(int)
        for row in rows:
            month_leads[row["date"][:7]] += row["lead"]
        for label, lead in month_leads.items():
            if label in history[name]:
                history[name][label]["lead"] = lead

    people = []
    for name in PEOPLE:
        fields = monthly.get(name)
        missing = fields is None
        master, sub, cpa = (current_value(fields, key) for key in ("Master IB", "Sub IB", "CPA"))
        people.append({
            "name": name,
            "early": name != "Rita",
            "hire": hires.get(name, ""),
            "stageMonth": month_index(hires.get(name, ""), datetime.fromisoformat(export_date).date()),
            "actualGross": current_value(fields, "Monthly Gross"),
            "targetGross": current_value(fields, "月Gross"),
            "grossCompletion": current_value(fields, "当月入金完成度"),
            "master": master,
            "sub": sub,
            "cpa": cpa,
            "actualIB": None if missing else int(master or 0) + int(sub or 0) + int(cpa or 0),
            "targetIB": current_value(fields, "月IB/CPA数量"),
            "reg": current_value(fields, "Registration"),
            "ftd": current_value(fields, "FTD"),
            "lead": sum_schedule_leads(schedules.get(name, [])),
            "channel": "", "intent": "", "converted": "",
            "actualWithdrawal": None if missing else number(fields.get("Monthly Gross")) - number(fields.get("Monthly Net")),
            "actualNet": current_value(fields, "Monthly Net"),
            "monthlyDataStatus": "unavailable" if missing else "available",
        })

    total_fields = monthly.get("Total")
    if total_fields is None:
        raise RuntimeError(f"团队数据汇总 is missing Total for {month}")
    payload = {
        "generatedAt": now.isoformat().replace("+00:00", "Z"),
        "dashboardDataUpdatedAt": now.isoformat().replace("+00:00", "Z"),
        "source": "Lark dashboard snapshot",
        "people": people,
        "issues": issues,
        "customers": customers,
        "trends": {name: [history[name][label] for label in sorted(history[name])] for name in PEOPLE},
        "teamGrossTrend": [team_history[label] for label in sorted(team_history)],
        "teamCompletion": current_value(total_fields, "当月入金完成度"),
    }
    Path(args.output).write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"dashboard export complete people={len(people)} issues={len(issues)} customers={len(customers)}", flush=True)


if __name__ == "__main__":
    main()
