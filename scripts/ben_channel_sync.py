#!/usr/bin/env python3
"""Read-only preview for Ben's dynamically discovered IB and CPA channels.

This module intentionally has no Lark write path.  It is the classification
and preview half of the channel synchronisation workflow: a later authorised
apply command must consume this manifest, rather than reconstructing a list
of channels from a hard-coded roster.
"""
import argparse
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sync_x5_daily_gross import LarkTransport, crm_login, lark_records, post, text_value

TABLE = "tblOJxbrBo7zs997"
BEN_ALIAS = "Ben.Eu"
BEN_UID = "964492"
SHANGHAI = timezone(timedelta(hours=8))
METRICS = ("registration", "ftd", "grossDeposit", "withdrawal", "net")


def value(row, *names):
    for name in names:
        candidate = row.get(name)
        if candidate not in (None, ""):
            return str(candidate)
    return ""


def number(raw):
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def admin_tree(headers):
    """Return the live team hierarchy; no identity is inferred from ``type``."""
    payload = {"limit": 9999, "pageNo": 1, "offset": 0, "order": "asc",
               "search": {"directLevel": "5", "user_id": "", "org_id": "73",
                          "search_type": "", "searchText": ""}}
    rows = post(headers, "/admin/query_adminListSimple", payload, 30).get("rows", [])
    by_id = {str(row.get("user_id")): row for row in rows if row.get("user_id") is not None}
    if BEN_UID not in by_id:
        # The lead is intentionally absent from this CRM list on some tenants.
        by_id[BEN_UID] = {"user_id": BEN_UID, "user_name": BEN_ALIAS, "parent_id": None}
    return by_id


def descendants(by_id, root_ids):
    found, pending = set(), {str(item) for item in root_ids}
    while pending:
        parent = pending.pop()
        for node_id, row in by_id.items():
            if str(row.get("parent_id")) == parent and node_id not in found:
                found.add(node_id)
                pending.add(node_id)
    return found


def team_sales_roots(by_id):
    """Discover active sales roots from the live administrator hierarchy.

    A sales root is a live direct Ben child with the CRM sales commission flag.
    This deliberately has no salesperson-name, count, or channel-list config.
    """
    return {node_id for node_id, row in by_id.items()
            if str(row.get("parent_id")) == BEN_UID and row.get("status", 1) == 1
            and str(row.get("commission_type")) == "1"}


def ben_cpa_aggregate(by_id):
    matches = [node_id for node_id, row in by_id.items()
               if str(row.get("parent_id")) == BEN_UID and row.get("user_name") == "Ben.EuCPA"]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one Ben.EuCPA aggregate node, got {len(matches)}")
    return matches[0]


def query_crm_nodes(headers):
    """Fetch the live Ben scope. Pagination is mandatory so 59 is not baked in."""
    rows, page = [], 1
    while True:
        payload = {"skipCount": False,
                   # Keep the full current Ben scope in one response when
                   # possible; 500 has stalled in production, while 200 is
                   # within the CRM's reliable response window.
                   "pagination": {"limit": 200, "pageNo": page, "offset": (page - 1) * 200,
                                  "order": "asc", "sort": ""},
                   "parameters": {"directLevel": {"filterType": "CUSTOM", "input": "5"},
                                  "user_id": {"filterType": "CUSTOM", "input": BEN_UID},
                                  "ib_level": {"filterType": "CUSTOM", "input": "1"}}}
        result = post(headers, "/user/query_userList", payload, 180)
        batch = result.get("rows", [])
        rows.extend(batch)
        total = result.get("total")
        if len(batch) < 200 or (total is not None and len(rows) >= int(total)):
            return rows
        page += 1


def classify_ib_nodes(rows, by_id, sales_roots):
    """Keep only Ben-direct level-1 IB nodes outside all sales branches.

    CRM versions label parent fields differently, so the resolver accepts the
    documented alternatives. Rows without a resolvable parent are reported,
    never silently assigned by their ``type`` field.
    """
    sales_branch = descendants(by_id, sales_roots)
    sales_aliases = {by_id[root].get("user_name") for root in sales_roots}
    # Channel records have their own parent-id tree. Seed it from the live
    # sales-root mapping, then recursively mark the complete channel branch.
    channel_by_id = {value(row, "user_id", "userId", "id"): row for row in rows
                     if value(row, "user_id", "userId", "id")}
    sales_channel_branch = {node_id for node_id, row in channel_by_id.items()
                            if value(row, "ownerAlias", "owner") in sales_aliases}
    changed = True
    while changed:
        changed = False
        for node_id, row in channel_by_id.items():
            if value(row, "parent_id", "parentId") in sales_channel_branch and node_id not in sales_channel_branch:
                sales_channel_branch.add(node_id)
                changed = True
    channels, anomalies = [], []
    for row in rows:
        level = value(row, "ib_level", "ibLevel")
        if level != "1":
            continue
        node_id = value(row, "user_id", "userId", "id", "mt4Account")
        parent_id = value(row, "parent_id", "parentId", "owner_id", "ownerId")
        name = value(row, "userName", "user_name", "name", "clientNameIpt", "mt4Account")
        if not node_id:
            anomalies.append({"kind": "ib_missing_id", "name": name})
        elif parent_id in sales_branch or node_id in sales_channel_branch:
            continue
        elif parent_id and parent_id != BEN_UID:
            anomalies.append({"kind": "ib_non_ben_parent", "channelId": node_id, "parentId": parent_id, "name": name})
        elif not parent_id:
            # Older CRM responses use ownerAlias rather than an admin parent.
            # It is valid only for Ben itself, and never for a team member.
            owner = value(row, "ownerAlias", "owner")
            if owner != BEN_ALIAS:
                anomalies.append({"kind": "ib_unresolved_parent", "channelId": node_id, "owner": owner, "name": name})
            else:
                channels.append({"channelId": node_id, "accountName": name, "type": "IB",
                                 "metrics": {key: None for key in METRICS}, "source": "CRM ib_level=1"})
        elif parent_id == BEN_UID:
            channels.append({"channelId": node_id, "accountName": name, "type": "IB",
                             "metrics": {key: None for key in METRICS}, "source": "CRM ib_level=1"})
    duplicate_ids = {item["channelId"] for item in channels if sum(x["channelId"] == item["channelId"] for x in channels) > 1}
    if duplicate_ids:
        anomalies.extend({"kind": "duplicate_ib_channel_id", "channelId": item} for item in sorted(duplicate_ids))
        channels = [item for index, item in enumerate(channels) if item["channelId"] not in duplicate_ids or not any(x["channelId"] == item["channelId"] for x in channels[:index])]
    return channels, anomalies


def cellxpert_headers():
    import requests
    response = requests.post("https://go.ultimamarkets.com/adminlogin/loginadmin.asp",
        data={"command": "logon", "user": os.environ["CX_USER"], "password": os.environ["CX_PASS"], "json": 1},
        headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
    response.raise_for_status()
    token = response.json().get("message")
    if not token:
        raise RuntimeError("CellXpert login returned no access token")
    return {"Authorization": f"Bearer {token}", "admin_url": "Ultimarkets"}


def cellxpert_get(headers, command, **params):
    import requests
    response = requests.get("https://adminapi.cellxpert.com/", params={"command": command, "json": 1, **params}, headers=headers, timeout=60)
    response.raise_for_status()
    return response.json()


def cpa_channels(aggregate_name, month):
    headers = cellxpert_headers()
    # CRM names the CPA aggregate ``Ben.EuCPA``; CellXpert stores the
    # corresponding manager code without the aggregate-only CPA suffix.
    manager = aggregate_name.removesuffix("CPA").replace(".", "").lower()
    affiliates = cellxpert_get(headers, "browseaffiliatesjson").get("ManageAffiliatesData", [])
    approved = [row for row in affiliates if str(row.get("AffiliateManager", "")).replace(".", "").lower() == manager and str(row.get("Status", "")).lower() == "approved"]
    start = datetime.fromisoformat(f"{month}-01").strftime("%-m/%-d/%Y")
    end = datetime.now(SHANGHAI).strftime("%-m/%-d/%Y")
    report = cellxpert_get(headers, "processreport", Affiliate="true", BTA="true", startDate=start, endDate=end, uniqueId=1)
    report_by_id = {str(row.get("BTA")): row for row in report if row.get("BTA") is not None}
    channels, anomalies = [], []
    for affiliate in approved:
        channel_id = value(affiliate, "AffiliateID")
        if not channel_id:
            anomalies.append({"kind": "cpa_missing_affiliate_id"})
            continue
        current = report_by_id.get(channel_id)
        if current is None:
            anomalies.append({"kind": "cpa_missing_current_month_processreport", "channelId": channel_id})
            current = {}
        withdrawal = number(current.get("Withdrawals"))
        channels.append({"channelId": channel_id, "accountName": value(affiliate, "Username"), "type": "CPA",
                         "metrics": {"registration": number(current.get("Registrations")), "ftd": number(current.get("FTD")),
                                     "grossDeposit": number(current.get("Deposits")), "withdrawal": abs(withdrawal) if withdrawal is not None else None,
                                     "net": number(current.get("Net_Deposits"))},
                         "source": "CellXpert approved affiliate"})
    return channels, anomalies


def totals(channels):
    return {key: round(sum(channel["metrics"].get(key) or 0 for channel in channels), 2) for key in METRICS}


def metric_coverage(channels):
    return {key: sum(channel["metrics"].get(key) is not None for channel in channels) for key in METRICS}


def existing_channels(lark, month):
    result = {}
    for record in lark_records(lark, TABLE):
        fields = record.get("fields", {})
        if text_value(fields.get("销售")) != "Ben":
            continue
        date_value = fields.get("月份")
        date_text = str(date_value)[:7] if isinstance(date_value, str) else datetime.fromtimestamp(int(date_value) / 1000, SHANGHAI).strftime("%Y-%m") if date_value else ""
        if date_text == month and text_value(fields.get("渠道ID")):
            result[text_value(fields.get("渠道ID"))] = {"type": text_value(fields.get("类型")), "name": text_value(fields.get("IB/CPA账户名称"))}
    return result


def changes(current, existing):
    current_by_id = {row["channelId"]: row for row in current}
    added = sorted(set(current_by_id) - set(existing))
    missing = sorted(set(existing) - set(current_by_id))
    moved = [{"channelId": channel_id, "from": existing[channel_id], "to": {"type": current_by_id[channel_id]["type"], "name": current_by_id[channel_id]["accountName"]}}
             for channel_id in sorted(set(current_by_id) & set(existing))
             if existing[channel_id]["type"] != current_by_id[channel_id]["type"] or existing[channel_id]["name"] != current_by_id[channel_id]["accountName"]]
    return {"new": added, "missing": missing, "ownershipOrIdentityChanged": moved}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", default=datetime.now(SHANGHAI).strftime("%Y-%m"))
    parser.add_argument("--compare-lark", action="store_true", help="Read existing Ben rows to report drift; never writes")
    args = parser.parse_args()
    if args.month != datetime.now(SHANGHAI).strftime("%Y-%m"):
        raise RuntimeError("Preview supports only the current month")
    headers = crm_login()
    tree = admin_tree(headers)
    roots = team_sales_roots(tree)
    aggregate_id = ben_cpa_aggregate(tree)
    crm_rows = query_crm_nodes(headers)
    ib, anomalies = classify_ib_nodes(crm_rows, tree, roots)
    cpa, cpa_anomalies = cpa_channels(tree[aggregate_id]["user_name"], args.month)
    all_channels = ib + cpa
    drift = {"new": [], "missing": [], "ownershipOrIdentityChanged": []}
    if args.compare_lark:
        drift = changes(all_channels, existing_channels(LarkTransport(), args.month))
    print(json.dumps({"mode": "preview", "month": args.month, "larkWrite": False,
                      "roots": {"teamSalesRootIds": sorted(roots), "cpaAggregateId": aggregate_id},
                      "counts": {"ib": len(ib), "cpa": len(cpa), "total": len(all_channels)},
                      "totals": totals(all_channels), "metricCoverage": metric_coverage(all_channels), "changes": drift,
                      "anomalies": anomalies + cpa_anomalies,
                      "channels": all_channels}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
