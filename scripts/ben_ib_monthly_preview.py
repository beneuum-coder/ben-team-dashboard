#!/usr/bin/env python3
"""Read-only CRM proof and preview for Ben's top-IB monthly performance."""
import argparse
import hashlib
import json
import re
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from ben_channel_sync import BEN_UID, SHANGHAI, admin_tree, team_sales_roots, value
from sync_x5_daily_gross import crm_login, exchange_rates, post, usd

MONTH = datetime.now(SHANGHAI).strftime("%Y-%m")
START, END = f"{MONTH}-01 00:00:00", datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
SCHEMA_VERSION = "2.0"


def all_ben_nodes(headers):
    rows, page = [], 1
    while True:
        payload = {"skipCount": False, "pagination": {"limit": 200, "pageNo": page, "offset": (page - 1) * 200, "order": "asc", "sort": ""},
                   "parameters": {"directLevel": {"filterType": "CUSTOM", "input": "5"}, "user_id": {"filterType": "CUSTOM", "input": BEN_UID}}}
        result = post(headers, "/user/query_userList", payload, 60)
        batch = result.get("rows", [])
        rows.extend(batch)
        if len(batch) < 200 or len(rows) >= int(result.get("total", 0)):
            return rows
        page += 1


def discovery_snapshot(by_id, roots):
    sales = {node_id: by_id[node_id].get("user_name") for node_id in roots}
    channels = {node_id: {"name": row.get("clientNameIpt"), "parentId": value(row, "parentId", "parent_id")}
                for node_id, row in by_id.items() if value(row, "ib_level", "ibLevel") == "1" and value(row, "ownerAlias") == "Ben.Eu"}
    hierarchy = {node_id: value(row, "parentId", "parent_id") for node_id, row in by_id.items()}
    return {"sales": sales, "channels": channels, "hierarchy": hierarchy}


def discovery_diff(previous, current):
    before, after = previous or {"sales": {}, "channels": {}, "hierarchy": {}}, current
    moved = [key for key in set(before["channels"]) & set(after["channels"]) if before["channels"][key].get("parentId") != after["channels"][key].get("parentId")]
    hierarchy = [key for key in set(before["hierarchy"]) & set(after["hierarchy"]) if before["hierarchy"][key] != after["hierarchy"][key]]
    return {"new_channels": sorted(set(after["channels"]) - set(before["channels"])), "removed_channels": sorted(set(before["channels"]) - set(after["channels"])), "moved_channels": moved,
            "new_sales": sorted(set(after["sales"]) - set(before["sales"])), "removed_sales": sorted(set(before["sales"]) - set(after["sales"])), "hierarchy_changes": hierarchy}


def account_ids(row):
    # Account numbers are the numeric token immediately before ``(MT4|MT5...)``;
    # do not mistake the version digit in ``MT5`` for an account.
    return set(re.findall(r"(\d+)\(", str(row.get("mt4AccountStrAll") or "")))


def subtree(root_id, rows):
    by_id = {value(row, "user_id", "userId"): row for row in rows}
    result = {str(root_id)}
    changed = True
    while changed:
        changed = False
        for node_id, row in by_id.items():
            if value(row, "parentId", "parent_id") in result and node_id not in result:
                result.add(node_id)
                changed = True
    return [by_id[node_id] for node_id in result if node_id in by_id]


def individual_rows(headers, affiliate_id):
    body = {"skipCount": False, "pagination": {"limit": 9999, "pageNo": 1, "offset": 0, "order": "asc", "sort": ""},
            "parameters": {"affId": {"filterType": "CUSTOM", "input": affiliate_id}, "directLevel": {"filterType": "CUSTOM", "input": "5"}, "org_id": {"filterType": "CUSTOM", "input": "73"}}}
    return post(headers, "/individual/query_individualList", body, 90).get("rows", [])


def customers_for_subtree(headers, nodes):
    accounts = set().union(*(account_ids(node) for node in nodes))
    rows_by_uid, duplicate_uids = {}, set()
    with ThreadPoolExecutor(max_workers=100) as executor:
        futures = {executor.submit(individual_rows, headers, account): account for account in accounts}
        for future in as_completed(futures):
            account = futures[future]
            for row in future.result():
                uid = value(row, "user_id", "userId")
                if not uid:
                    continue
                if uid in rows_by_uid:
                    duplicate_uids.add(uid)
                rows_by_uid[uid] = row
    return rows_by_uid, sorted(duplicate_uids), sorted(accounts)


def ben_detail_rows(headers):
    search = {"directLevel": "3", "user_id": BEN_UID, "org_id": "73", "searchType": "-1", "search": "", "agentuserQuery": "", "startDate": START, "endDate": END, "dataSourceId": "0", "dataType": "0", "accType": "1", "paymentMethod": "0", "countryCodes": ""}
    return post(headers, "/report/view/depositAndWithdrawReports", {"limit": 9999, "pageNo": 1, "offset": 0, "order": "asc", "search": search}, 120).get("rows", [])


def day(value):
    return str(value or "")[:10]


def is_month(value):
    return str(value or "")[:7] == MONTH


def money(row, *keys):
    for key in keys:
        if row.get(key) not in (None, ""):
            raw = str(row[key]).replace(",", "").strip()
            return 0.0 if raw == "-" else float(raw)
    return 0.0


def performance(customers, details, rates):
    ids = set(customers)
    matched = [row for row in details if value(row, "userId", "user_id", "clientId") in ids]
    gross = withdrawal = 0.0
    for row in matched:
        currency = row.get("currency") or row.get("totalCurrency") or "USD"
        gross += usd(money(row, "depositCount", "depositAmount", "deposit"), currency, rates)
        # Detail endpoint uses signed withdrawals; report an outflow magnitude.
        withdrawal += abs(usd(money(row, "withdrawCount", "withdrawAmount", "withdrawal"), currency, rates))
    return {"customer_count": len(ids), "registration": sum(is_month(row.get("create_time") or row.get("createTime")) for row in customers.values()),
            "ftd": sum(is_month(row.get("ftd")) for row in customers.values()), "gross_deposit": round(gross, 2),
            "withdrawal": round(withdrawal, 2), "net": round(gross - withdrawal, 2), "detail_customer_matches": len({value(row, "userId", "user_id", "clientId") for row in matched})}


def channel_status(name, metrics):
    normalized = str(name or "").strip().casefold()
    if normalized.startswith("test") or "test " in normalized or normalized in {"tst tst", "testqwq tetstqw"}:
        return "test_or_suspected"
    if any(metrics[key] for key in ("registration", "ftd", "gross_deposit", "withdrawal")):
        return "active"
    return "inactive" if name else "unknown"


def payload_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def restore_owner_map(result, owners, overlaps):
    for uid in result.get("audit", {}).get("client_uids", []):
        owner = result["channel_id"]
        if uid in owners and owners[uid] != owner:
            overlaps.append({"clientUid": uid, "first": owners[uid], "second": owner})
        owners[uid] = owner


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", action="store_true")
    parser.add_argument("--nodes-json", help="Fresh same-run CRM node snapshot")
    parser.add_argument("--details-glob", help="Fresh same-run paginated CRM detail files")
    parser.add_argument("--checkpoint-dir", default="work/ben-ib-monthly-checkpoints")
    parser.add_argument("--as-of", default=datetime.now(SHANGHAI).isoformat())
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()
    headers = crm_login()
    tree = admin_tree(headers)
    roots = team_sales_roots(tree)
    all_rows = json.load(open(args.nodes_json)) if args.nodes_json else all_ben_nodes(headers)
    by_id = {value(row, "user_id", "userId"): row for row in all_rows}
    sales_aliases = {tree[root].get("user_name") for root in roots}
    top_ids = [node_id for node_id, row in by_id.items()
               if value(row, "ib_level", "ibLevel") == "1" and value(row, "ownerAlias") == "Ben.Eu"
               and value(row, "ownerAlias") not in sales_aliases]
    # Discovery, not a saved sample roster, defines every runnable channel.
    selected = top_ids
    if any(item not in by_id for item in selected):
        raise RuntimeError("Selected top IB missing from live CRM scope")
    if args.details_glob:
        from glob import glob
        detail_files = sorted(glob(args.details_glob))
        if len(detail_files) != 11:
            raise RuntimeError(f"Expected 11 detail pages, got {len(detail_files)}")
        details = [row for path in detail_files for row in json.load(open(path))]
        if len(details) != 1056:
            raise RuntimeError(f"Expected 1056 detail rows, got {len(details)}")
    else:
        details = ben_detail_rows(headers)
    rates = exchange_rates(headers)
    run_id = args.run_id or str(uuid.uuid5(uuid.NAMESPACE_URL, f"ben-ib:{MONTH}:{args.as_of}"))
    base_checkpoint_dir = Path(args.checkpoint_dir)
    checkpoint_dir = base_checkpoint_dir / "runs" / run_id
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    # The scoped channel query does not include the sales-root administrator
    # records.  Merge the live admin tree for discovery metadata only; channel
    # selection and all performance calculations still use the scoped rows.
    current_discovery = discovery_snapshot({**tree, **by_id}, roots)
    history_root = base_checkpoint_dir / "runs"
    historical = sorted(history_root.glob("*/discovery.json"), key=lambda path: path.stat().st_mtime) if history_root.exists() else []
    previous_discovery = json.loads(historical[-1].read_text()).get("discovery") if historical else None
    discovery = {"runId": run_id, "asOf": args.as_of, "discovery": current_discovery, "diff": discovery_diff(previous_discovery, current_discovery)}
    (checkpoint_dir / "discovery.json").write_text(json.dumps(discovery, ensure_ascii=False, indent=2))
    source_metadata = {"crmNodesHash": payload_hash(all_rows), "crmDetailHash": payload_hash(details), "month": MONTH, "asOf": args.as_of,
                       "nodesSource": "/user/query_userList", "fundsSource": "/report/view/depositAndWithdrawReports", "customersSource": "/individual/query_individualList(affId)"}
    report, all_customer_owner, overlaps, failed = [], {}, [], []
    # Invalidate any previous completion marker before resuming/migrating a run.
    (checkpoint_dir / "manifest.json").write_text(json.dumps({"schemaVersion": SCHEMA_VERSION, "runId": run_id, "status": "partial", "month": MONTH, "asOf": args.as_of, "completed": 0, "expected": len(selected), "failed": [], "sourceMetadata": source_metadata}, ensure_ascii=False, indent=2))
    for root_id in selected:
        checkpoint = checkpoint_dir / f"{root_id}.json"
        if checkpoint.exists():
            saved = json.loads(checkpoint.read_text())
            if saved.get("schemaVersion") == SCHEMA_VERSION and saved.get("month") == MONTH and saved.get("asOf") == args.as_of and saved.get("status") == "complete":
                report.append(saved["result"])
                restore_owner_map(saved["result"], all_customer_owner, overlaps)
                continue
        try:
            nodes = subtree(root_id, all_rows)
            customers, duplicate_uids, accounts = customers_for_subtree(headers, nodes)
            for uid in customers:
                if uid in all_customer_owner:
                    overlaps.append({"customerId": uid, "first": all_customer_owner[uid], "second": root_id})
                all_customer_owner[uid] = root_id
            item = {"channel_id": root_id, "channel_name": by_id[root_id].get("clientNameIpt"), "subtree_node_count": len(nodes),
                "subtree_accounts": accounts, "duplicate_customer_ids_inside_subtree": duplicate_uids,
                **performance(customers, details, rates), "data_source": "CRM individualList(affId subtree) + CRM depositAndWithdrawReports(Ben scope)",
                "data_status": "verified" if not duplicate_uids else "duplicate_customer_ids_inside_subtree"}
            item["channel_status"] = channel_status(item["channel_name"], item)
            item["audit"] = {"channel_root_id": root_id, "subtree_node_ids": sorted(value(node, "user_id", "userId") for node in nodes),
                             "rebate_account_ids": accounts, "client_uids": sorted(customers), "client_uid_to_top_level_ib": {uid: root_id for uid in sorted(customers)}}
            checkpoint_payload = {"schemaVersion": SCHEMA_VERSION, "runId": run_id, "status": "complete", "month": MONTH, "asOf": args.as_of,
                                  "sourceMetadata": source_metadata, "result": item}
            checkpoint_payload["resultHash"] = payload_hash(item)
            checkpoint.write_text(json.dumps(checkpoint_payload, ensure_ascii=False))
            report.append(item)
        except Exception as exc:
            checkpoint.write_text(json.dumps({"schemaVersion": SCHEMA_VERSION, "runId": run_id, "status": "failed", "month": MONTH, "asOf": args.as_of, "sourceMetadata": source_metadata, "error": str(exc)}, ensure_ascii=False))
            failed.append(root_id)
    status = "complete" if len(report) == len(selected) and not failed else "partial"
    manifest = {"schemaVersion": SCHEMA_VERSION, "runId": run_id, "status": status, "month": MONTH, "asOf": args.as_of, "completed": len(report), "expected": len(selected), "failed": failed, "sourceMetadata": source_metadata, "resultHash": payload_hash(report)}
    (checkpoint_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(json.dumps({"mode": "sample" if args.sample else "all", "month": MONTH, "asOf": args.as_of, "status": status, "larkWrite": False,
                      "schemaVersion": SCHEMA_VERSION, "runId": run_id, "sourceMetadata": source_metadata, "identity_anomalies": [], "cross_channel_customer_overlaps": overlaps, "clientUidToTopLevelIb": all_customer_owner,
                      "report": report}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
