#!/usr/bin/env python3
"""Merge an already verified X5 daily export into the deployed JS snapshot."""
import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

PREFIX = "window.LARK_DASHBOARD_DATA = "
X5_NAMES = ("Rita", "Ben", "Kelly", "Elroy")


def number(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--export", required=True)
    parser.add_argument("--dashboard-export", help="Complete read-only Dashboard Lark export to publish atomically")
    parser.add_argument("--data", default="outputs/lark-dashboard-data.js")
    parser.add_argument("--html", default="outputs/团队扩编与筛选体系框架-可视化编辑版.html")
    args = parser.parse_args()
    exported = json.loads(Path(args.export).read_text(encoding="utf-8"))
    records = exported.get("records", {})
    expected = {*X5_NAMES, "Total"}
    if set(records) != expected:
        raise RuntimeError("Refusing to publish an incomplete X5 export")
    data_path = Path(args.data)
    if args.dashboard_export:
        data = json.loads(Path(args.dashboard_export).read_text(encoding="utf-8"))
        required = {"generatedAt", "dashboardDataUpdatedAt", "people", "issues", "customers", "trends", "teamGrossTrend", "teamCompletion"}
        missing = required - set(data)
        if missing:
            raise RuntimeError(f"Dashboard export is incomplete: missing {sorted(missing)}")
    else:
        raw = data_path.read_text(encoding="utf-8").strip()
        if not raw.startswith(PREFIX) or not raw.endswith(";"):
            raise RuntimeError("Unexpected dashboard data file format")
        data = json.loads(raw[len(PREFIX):-1])
    people = {person.get("name"): person for person in data.get("people", [])}
    if not set(X5_NAMES).issubset(people):
        raise RuntimeError("Dashboard snapshot is missing an X5 salesperson")
    for name in X5_NAMES:
        source, target = records[name], people[name]
        target["actualGross"] = number(source.get("入金(USD)"))
        target["actualWithdrawal"] = number(source.get("出金(USD)"))
        target["actualNet"] = number(source.get("净入金(USD)"))
        target["master"] = int(number(source.get("Master IB")))
        target["sub"] = int(number(source.get("Sub IB")))
        target["actualIB"] = target["master"] + target["sub"] + int(number(target.get("cpa")))
    month = str(exported["date"])[:7]
    for name in X5_NAMES:
        series = data.setdefault("trends", {}).setdefault(name, [])
        item = next((row for row in series if row.get("label") == month), None)
        if item is None:
            item = {"label": month, "lead": 0, "ib": 0, "gross": 0}
            series.append(item)
        item["gross"] = people[name]["actualGross"]
        item["ib"] = people[name]["actualIB"]
        series.sort(key=lambda row: row.get("label", ""))
    total = records["Total"]
    team = data.setdefault("teamGrossTrend", [])
    entry = next((row for row in team if row.get("label") == month), None)
    if entry is None:
        entry = {"label": month, "gross": 0, "actualIB": 0}
        team.append(entry)
    entry["gross"] = number(total.get("入金(USD)"))
    entry["actualIB"] = int(number(total.get("Master IB"))) + int(number(total.get("Sub IB")))
    team.sort(key=lambda row: row.get("label", ""))
    data["grossDataUpdatedAt"] = datetime.fromtimestamp(int(exported["updatedAt"]) / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
    data["grossDataSource"] = "X5 daily MTD Gross snapshot"
    rendered_data = PREFIX + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + ";\n"
    revision = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    html_path = Path(args.html)
    html = html_path.read_text(encoding="utf-8")
    html, count = re.subn(r"lark-dashboard-data[.]js[?]rev=[^\"']+", f"lark-dashboard-data.js?rev={revision}", html, count=1)
    if count != 1:
        raise RuntimeError("Dashboard HTML does not contain the expected data script")
    # All validation is complete before either deployable artifact is replaced.
    data_path.write_text(rendered_data, encoding="utf-8")
    html_path.write_text(html, encoding="utf-8")
    print(f"Published X5 data for {exported['date']} at {data['grossDataUpdatedAt']}")


if __name__ == "__main__":
    main()
