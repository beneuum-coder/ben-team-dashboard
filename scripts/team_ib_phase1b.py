#!/usr/bin/env python3
"""Read-only audited CRM IB channels for every live sales root under Ben."""
import argparse
import hashlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from ben_channel_sync import BEN_UID, SHANGHAI, admin_tree, team_sales_roots, value
from ben_ib_monthly_preview import account_ids, channel_status, customers_for_subtree, money, payload_hash, performance, subtree
from sync_x5_daily_gross import crm_login, exchange_rates, post

MONTH = datetime.now(SHANGHAI).strftime("%Y-%m")
START, END = f"{MONTH}-01 00:00:00", datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
SCHEMA_VERSION = "2.0"


def sales_nodes(headers, sales_id):
    rows, page = [], 1
    while True:
        body = {"skipCount": False, "pagination": {"limit": 200, "pageNo": page, "offset": (page - 1) * 200, "order": "asc", "sort": ""},
                "parameters": {"directLevel": {"filterType": "CUSTOM", "input": "5"}, "user_id": {"filterType": "CUSTOM", "input": sales_id}}}
        result = post(headers, "/user/query_userList", body, 90)
        batch = result.get("rows", []); rows.extend(batch)
        if len(batch) < 200 or len(rows) >= int(result.get("total", 0)):
            return rows
        page += 1


def detail_page(headers, sales_id, page_no):
    search = {"directLevel": "3", "user_id": sales_id, "org_id": "73", "searchType": "-1", "search": "", "agentuserQuery": "", "startDate": START, "endDate": END, "dataSourceId": "0", "dataType": "0", "accType": "1", "paymentMethod": "0", "countryCodes": ""}
    return post(headers, "/report/view/depositAndWithdrawReports", {"limit": 100, "pageNo": page_no, "offset": 0, "order": "asc", "search": search}, 60)


def sales_details(headers, sales_id):
    first = detail_page(headers, sales_id, 1); total = int(first.get("total") or 0)
    pages = (total + 99) // 100
    rows = first.get("rows", [])
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = [executor.submit(detail_page, headers, sales_id, page) for page in range(2, pages + 1)]
        for future in as_completed(futures): rows.extend(future.result().get("rows", []))
    if len(rows) != total: raise RuntimeError(f"detail pagination mismatch sales={sales_id} expected={total} got={len(rows)}")
    return rows


def top_channels(rows, sales_name):
    return [value(row, "user_id", "userId") for row in rows if value(row, "ib_level", "ibLevel") == "1" and value(row, "ownerAlias") == sales_name]


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--as-of", required=True); parser.add_argument("--run-id"); parser.add_argument("--checkpoint-dir", default="work/team-ib-phase1b")
    args = parser.parse_args(); run_id = args.run_id or str(uuid.uuid5(uuid.NAMESPACE_URL, f"team-ib:{MONTH}:{args.as_of}")); run_dir = Path(args.checkpoint_dir) / "runs" / run_id; run_dir.mkdir(parents=True, exist_ok=True)
    discovery_path = run_dir / "discovery.json"
    if discovery_path.exists():
        discovery = json.loads(discovery_path.read_text())
        if discovery.get("asOf") != args.as_of or discovery.get("month") != MONTH: raise RuntimeError("Discovery snapshot does not match run/asOf/month")
    else:
        headers = crm_login(); tree = admin_tree(headers); roots = team_sales_roots(tree)
        with ThreadPoolExecutor(max_workers=max(1, len(roots))) as executor:
            futures = {executor.submit(sales_nodes, headers, sales_id): sales_id for sales_id in roots}
            rows_by_sales = {futures[future]: future.result() for future in as_completed(futures)}
        sales = {sales_id: {"name": tree[sales_id]["user_name"], "nodes": rows_by_sales[sales_id]} for sales_id in roots}
        channels = {sales_id: top_channels(item["nodes"], item["name"]) for sales_id, item in sales.items()}
        discovery = {"schemaVersion": SCHEMA_VERSION, "runId": run_id, "asOf": args.as_of, "month": MONTH, "status": "discovery_complete", "sales": sales, "channels": channels,
                     "discoveredSalesCount": len(sales), "discoveredChannelCount": sum(len(items) for items in channels.values())}
        discovery["sourceHash"] = payload_hash({"sales": {key: value["nodes"] for key, value in sales.items()}, "channels": channels})
        discovery_path.write_text(json.dumps(discovery, ensure_ascii=False, indent=2))
    headers = crm_login(); rates = exchange_rates(headers); team_rows=[]; global_owners={}; overlaps=[]; sales_summary=[]; failures=[]
    expected = discovery["discoveredChannelCount"]
    def persist(status, current_sales="", current_channel=""):
        completed = len(team_rows); manifest = {"schemaVersion": SCHEMA_VERSION, "runId": run_id, "asOf": args.as_of, "month": MONTH, "status": status, "discovery_complete": True, "expected": expected, "completed": completed, "failed": failures, "remaining": expected - completed - len(failures), "currentSales": current_sales, "currentChannel": current_channel, "discoveryHash": discovery["sourceHash"]}
        (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    persist("calculation_partial")
    for sales_id, sale in discovery["sales"].items():
        sales_name = sale["name"]; rows=sale["nodes"]; by_id={value(row,"user_id","userId"):row for row in rows}; channels=discovery["channels"][sales_id]
        sales_dir=run_dir / sales_id; sales_dir.mkdir(exist_ok=True)
        pending=[]
        for channel_id in channels:
            checkpoint=sales_dir / f"{channel_id}.json"
            if checkpoint.exists() and (saved:=json.loads(checkpoint.read_text())).get("status") == "success":
                result=saved["result"]; team_rows.append(result)
                for uid in result["audit"]["client_uids"]:
                    if uid in global_owners and global_owners[uid] != (sales_name, channel_id): overlaps.append({"clientUid":uid,"first":global_owners[uid],"second":(sales_name,channel_id)})
                    global_owners[uid]=(sales_name,channel_id)
            else: pending.append(channel_id)
        details = sales_details(headers, sales_id) if pending else []
        source={"salesId":sales_id,"salesName":sales_name,"nodesHash":payload_hash(rows),"detailsHash":payload_hash(details) if details else "reused-success-checkpoints","month":MONTH,"asOf":args.as_of}
        for channel_id in pending:
            checkpoint=sales_dir / f"{channel_id}.json"
            try:
                nodes=subtree(channel_id,rows); customers,duplicates,accounts=customers_for_subtree(headers,nodes); metrics=performance(customers,details,rates)
                result={"sales":sales_name,"sales_root_id":sales_id,"channel_id":channel_id,"channel_name":by_id[channel_id].get("clientNameIpt"),"subtree_node_count":len(nodes),"account_count":len(accounts),"client_count":len(customers),**metrics,"channel_status":channel_status(by_id[channel_id].get("clientNameIpt"),metrics),"data_source":"CRM individualList(affId subtree) + CRM depositAndWithdrawReports(sales scope)","data_status":"verified" if not duplicates else "duplicate_customer_ids_inside_subtree","audit":{"subtree_node_ids":sorted(value(n,"user_id","userId") for n in nodes),"rebate_account_ids":accounts,"client_uids":sorted(customers),"client_uid_to_top_level_ib":{uid:channel_id for uid in sorted(customers)}}}
                checkpoint.write_text(json.dumps({"schemaVersion":SCHEMA_VERSION,"runId":run_id,"asOf":args.as_of,"month":MONTH,"salesId":sales_id,"topLevelChannelId":channel_id,"status":"success","sourceMetadata":source,"resultHash":payload_hash(result),"result":result},ensure_ascii=False)); team_rows.append(result)
                for uid in customers:
                    if uid in global_owners and global_owners[uid] != (sales_name,channel_id): overlaps.append({"clientUid":uid,"first":global_owners[uid],"second":(sales_name,channel_id)})
                    global_owners[uid]=(sales_name,channel_id)
                persist("calculation_partial", sales_name, channel_id)
            except Exception as exc:
                checkpoint.write_text(json.dumps({"schemaVersion":SCHEMA_VERSION,"runId":run_id,"asOf":args.as_of,"month":MONTH,"salesId":sales_id,"topLevelChannelId":channel_id,"status":"failed","error":str(exc),"sourceMetadata":source},ensure_ascii=False)); failures.append({"sales":sales_name,"channelId":channel_id}); persist("calculation_partial", sales_name, channel_id)
        own=[r for r in team_rows if r["sales"]==sales_name]; sales_summary.append({"sales":sales_name,"topIbCount":len(channels),"active":sum(r["channel_status"]=="active" for r in own),"totals":{k:round(sum(r[k] for r in own),2) for k in ("registration","ftd","gross_deposit","withdrawal","net")}})
    status="complete" if len(team_rows)==expected and not failures else "calculation_partial"
    result={"schemaVersion":SCHEMA_VERSION,"runId":run_id,"asOf":args.as_of,"month":MONTH,"status":status,"sales":sales_summary,"channels":team_rows,"clientUidOwner":{uid:{"sales":owner[0],"topLevelIb":owner[1]} for uid,owner in global_owners.items()},"crossSalesUidOverlap":overlaps,"failures":failures,"cpaIncluded":False}
    result["resultHash"]=payload_hash(result); (run_dir/("complete-results.json" if status=="complete" else "partial-results.json")).write_text(json.dumps(result,ensure_ascii=False,indent=2)); persist(status); print(json.dumps({"runId":run_id,"status":status,"sales":sales_summary,"channels":len(team_rows),"expected":expected,"overlaps":len(overlaps),"failures":len(failures),"checkpoint":str(run_dir)},ensure_ascii=False))


if __name__ == "__main__": main()
