#!/usr/bin/env python3
"""Single current-month X5 IB + CPA preview/upsert entry point."""
import argparse, json, os, subprocess, sys, uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests
from sync_x5_daily_gross import LarkTransport, cx_date, lark_records, text_value

TABLE="tblOJxbrBo7zs997"; SUMMARY_TABLE="tblUn0uqcKU7KOVF"; TZ=timezone(timedelta(hours=8))
METRICS=("registration","ftd","gross","withdrawal","net")
FIELDS={"registration":"Registration","ftd":"FTD","gross":"Gross Deposit","withdrawal":"Withdrawal","net":"Net"}
CPA_MANAGERS={"BenEu":"Ben","RitaEU":"Rita","KellyLin":"Kelly"}
SALES=("Ben","Rita","Kelly","Elroy")
_LAST_AFFILIATES=None

def num(v):
    try:return round(float(v),2)
    except (TypeError,ValueError):return None
def month(v): return v[:7] if isinstance(v,str) else (datetime.fromtimestamp(int(v)/1000,TZ).strftime("%Y-%m") if v else "")
def key(r): return (r["month"],r["sales"],r["type"],str(r["channelId"]).strip())
def metrics(f): return {k:num(f.get(n)) for k,n in FIELDS.items()}
def index_lark(records,target):
    result,issues={},[]
    for record in records:
        f=record.get("fields",{})
        if month(f.get("月份"))!=target: continue
        row={"month":target,"sales":text_value(f.get("销售")).strip(),"type":text_value(f.get("类型")).strip(),"channelId":text_value(f.get("渠道ID")).strip()}
        if not any((row["sales"],row["type"],row["channelId"])): issues.append({"classification":"IGNORED_PLACEHOLDER","recordId":record["record_id"]});continue
        if not all((row["sales"],row["type"],row["channelId"])): issues.append({"classification":"INVALID_KEY","recordId":record["record_id"],**row});continue
        if key(row) in result: issues.append({"classification":"DUPLICATE_KEY","key":key(row)});continue
        result[key(row)]=record
    return result,issues
def classify(rows,existing):
    out=[];seen=set()
    for r in rows:
        if r.get("classification")=="SKIPPED_NO_MONTHLY_DATA":out.append(r);continue
        if not all((r.get("sales"),r.get("type"),r.get("channelId"),r.get("channelName"))) or not all(r.get(x) is not None for x in METRICS) or key(r) in seen:out.append({"classification":"INVALID_KEY",**r});continue
        seen.add(key(r)); old=existing.get(key(r))
        if not old:out.append({"classification":"CREATE",**r});continue
        out.append({"classification":"UPDATE" if metrics(old["fields"])!={x:r[x] for x in METRICS} else "UNCHANGED","recordId":old["record_id"],**r})
    return out
def cx_headers():
    r=requests.post("https://go.ultimamarkets.com/authenticate/admin-auth",data={"user":os.environ["CX_USER"],"pass":os.environ["CX_PASS"],"url":"Ultimarkets"},headers={"Content-Type":"application/x-www-form-urlencoded","admin_url":"Ultimarkets"},timeout=30);r.raise_for_status()
    token=r.json().get("token")
    if not token: raise RuntimeError("CellXpert login returned no access token")
    return {"Authorization":"Bearer "+token,"admin_url":"Ultimarkets"}
def affiliates(h):
    affiliate_response=requests.get("https://adminapi.cellxpert.com/",params={"command":"browseaffiliatesjson","json":1},headers=h,timeout=60)
    affiliate_response.raise_for_status();affiliate_data=affiliate_response.json()
    aff=affiliate_data.get("ManageAffiliatesData") if isinstance(affiliate_data,dict) else None
    if not isinstance(aff,list) or not aff:raise RuntimeError("invalid or empty CellXpert affiliate list")
    return aff
def cpa_counts(aff,target):
    # Approved CPA affiliates registered in `target` month, mapped to their owner.
    # Unmapped managers stay out of the count, matching the channel sync rule.
    counts={s:0 for s in SALES};unmapped=Counter()
    for a in aff:
        if str(a.get("Status") or "").casefold()!="approved":continue
        if str(a.get("Created") or "")[:7]!=target:continue
        mgr=str(a.get("AffiliateManager") or "").strip()
        if mgr not in CPA_MANAGERS:unmapped[mgr]+=1;continue
        counts[CPA_MANAGERS[mgr]]+=1
    counts["Total"]=sum(counts[s] for s in SALES)
    return counts,dict(unmapped)
def summary_rows(l,target):
    rows={}
    for record in lark_records(l,SUMMARY_TABLE):
        f=record.get("fields",{})
        if month(f.get("月份"))!=target:continue
        name=text_value(f.get("销售")).strip()
        if not name:continue
        if name in rows:raise RuntimeError(f"团队数据汇总 has duplicate {target} rows for {name}")
        rows[name]=record
    return rows
def summary_cpa_write(l,counts,target):
    """Write the monthly CPA count back and prove the values landed."""
    existing=summary_rows(l,target)
    missing=sorted(name for name in counts if name not in existing)
    actions=[]
    for name in sorted(counts):
        record=existing.get(name)
        if record is None:continue
        if num(record["fields"].get("CPA"))!=float(counts[name]):actions.append({"record_id":record["record_id"],"fields":{"CPA":counts[name]},"sales":name})
    if actions:
        l.request("POST",f"/bitable/v1/apps/{l.app}/tables/{SUMMARY_TABLE}/records/batch_update",{"records":[{"record_id":a["record_id"],"fields":a["fields"]} for a in actions]})
    after=summary_rows(l,target)
    mismatched=[a["sales"] for a in actions if after.get(a["sales"]) is None or num(after[a["sales"]]["fields"].get("CPA"))!=float(a["fields"]["CPA"])]
    if mismatched:raise RuntimeError(f"summary CPA read-back mismatch: {mismatched}")
    return {"counts":counts,"updated":[a["sales"] for a in actions],"missing":missing}
def cpa_rows(target,h=None,aff=None):
    global _LAST_AFFILIATES
    h=h if h is not None else cx_headers(); aff=aff if aff is not None else affiliates(h); _LAST_AFFILIATES=aff
    report_response=requests.get("https://adminapi.cellxpert.com/",params={"command":"processreport","json":1,"Affiliate":"true","BTA":"true","startDate":cx_date(datetime.fromisoformat(target+"-01")),"endDate":cx_date(datetime.now(TZ)),"uniqueId":1},headers=h,timeout=60)
    report_response.raise_for_status();rep=report_response.json()
    if not isinstance(rep,list):raise RuntimeError("invalid CellXpert processreport")
    by={str(x.get("BTA")):x for x in rep if x.get("BTA") not in (None,"")};rows=[];issues=[]
    for a in aff:
        if str(a.get("Status","")).casefold()!="approved":continue
        mgr=str(a.get("AffiliateManager") or "").strip();cid=str(a.get("AffiliateID") or "").strip();name=str(a.get("Username") or a.get("AffiliateName") or "").strip()
        if mgr not in CPA_MANAGERS:issues.append({"classification":"UNMAPPED_MANAGER","manager":mgr,"channelId":cid});continue
        base={"month":target,"sales":CPA_MANAGERS[mgr],"type":"CPA","channelId":cid,"channelName":name}
        if not cid or not name:issues.append({"classification":"INVALID_KEY",**base});continue
        d=by.get(cid)
        if d is None:rows.append({"classification":"SKIPPED_NO_MONTHLY_DATA",**base});continue
        w=num(d.get("Withdrawals"));rows.append({**base,"registration":num(d.get("Registrations")),"ftd":num(d.get("FTD")),"gross":num(d.get("Deposits")),"withdrawal":abs(w) if w is not None else None,"net":num(d.get("Net_Deposits"))})
    return rows,issues
def run_ib(target):
    run="x5-monthly-"+uuid.uuid4().hex;asof=datetime.now(TZ).isoformat();root=Path("work/x5-monthly")/run
    base=[sys.executable,"scripts/ben_ib_monthly_preview.py","--as-of",asof,"--run-id",run,"--checkpoint-dir",str(root/"ben")]
    ben=json.loads(subprocess.check_output(base,text=True))
    if ben.get("status")!="complete" or ben.get("month")!=target:raise RuntimeError("Ben IB source is incomplete or has the wrong month")
    rows=[];units={"IB/Ben":{"status":"PASS","discovered":len(ben["report"])}}
    for x in ben["report"]: rows.append({"month":target,"sales":"Ben","type":"IB","channelId":str(x["channel_id"]),"channelName":x["channel_name"],"registration":x["registration"],"ftd":x["ftd"],"gross":x["gross_deposit"],"withdrawal":x["withdrawal"],"net":x["net"]})
    teamcmd=[sys.executable,"scripts/team_ib_phase1b.py","--as-of",asof,"--run-id",run,"--checkpoint-dir",str(root/"team")]
    subprocess.check_output(teamcmd,text=True)
    result=json.loads(next((root/"team"/"runs"/run).glob("*-results.json")).read_text())
    if result.get("status")!="complete" or result.get("month")!=target:raise RuntimeError("Team IB source is incomplete or has the wrong month")
    failed={x["sales"].replace(".EU","").replace(".Lin","").replace(".Chuan","") for x in result["failures"]}
    for sale in SALES[1:]:
        raw={"Rita":"Rita.EU","Kelly":"Kelly.Lin","Elroy":"Elroy.Chuan"}[sale]
        unit=[x for x in result["channels"] if x["sales"]==raw]
        if sale in failed:
            units["IB/"+sale]={"status":"FAIL","discovered":0,"failures":[item for item in result["failures"] if item["sales"]==raw]}
            continue
        units["IB/"+sale]={"status":"PASS","discovered":len(unit)}
        rows.extend({"month":target,"sales":sale,"type":"IB","channelId":str(x["channel_id"]),"channelName":x["channel_name"],"registration":x["registration"],"ftd":x["ftd"],"gross":x["gross_deposit"],"withdrawal":x["withdrawal"],"net":x["net"]} for x in unit)
    return rows,units
def payload(r,now):
    return {"月份":int(datetime.fromisoformat(r["month"]+"-01").replace(tzinfo=TZ).timestamp()*1000),"销售":r["sales"],"渠道ID":r["channelId"],"IB/CPA账户名称":r["channelName"],"类型":r["type"],"Registration":r["registration"],"FTD":r["ftd"],"Gross Deposit":r["gross"],"Withdrawal":r["withdrawal"],"Net":r["net"],"更新时间":now}
def main():
    p=argparse.ArgumentParser();p.add_argument("--apply",action="store_true");p.add_argument("--month",default=datetime.now(TZ).strftime("%Y-%m"));a=p.parse_args()
    if a.month!=datetime.now(TZ).strftime("%Y-%m"):raise RuntimeError("current month only")
    ib,units=run_ib(a.month)
    try:cpa,issues=cpa_rows(a.month);counts,unmapped=cpa_counts(_LAST_AFFILIATES,a.month) if _LAST_AFFILIATES else (None,None);units.update({"CPA/"+s:{"status":"PASS"} for s in SALES[:3]})
    except Exception as e:counts,unmapped=None,None;cpa=[];issues=[{"classification":"SOURCE_FAIL","source":"CPA","reason":str(e)}];units.update({"CPA/"+s:{"status":"FAIL","reason":str(e)} for s in SALES[:3]})
    if a.apply and any(unit["status"]=="FAIL" for unit in units.values()):
        raise RuntimeError("refusing Lark write with failed IB or CPA source units")
    units["CPA/Elroy"]={"status":"NOT_CONFIGURED"};l=LarkTransport();existing,an=index_lark(lark_records(l,TABLE),a.month)
    if any(x["classification"] in {"DUPLICATE_KEY","INVALID_KEY"} for x in an):raise RuntimeError("invalid or duplicate Lark business key")
    preview=classify(ib+cpa,existing);actions=[x for x in preview if x["classification"] in {"CREATE","UPDATE"}]
    if a.apply and any(x["classification"]=="INVALID_KEY" for x in preview+issues):
        raise RuntimeError("refusing Lark write with invalid source key or metrics")
    failed_sales={name.split("/",1)[1] for name,unit in units.items() if name.startswith("IB/") and unit["status"]=="FAIL"}
    protected={k:json.dumps(record["fields"],ensure_ascii=False,sort_keys=True) for k,record in existing.items() if (k[1] in failed_sales and k[2]=="IB") or any(key(row)==k for row in preview if row.get("classification")=="SKIPPED_NO_MONTHLY_DATA")}
    result={"mode":"APPLY" if a.apply else "PREVIEW","larkWrite":a.apply,"units":units,"totals":dict(Counter(x["classification"] for x in preview)),"anomalies":an+issues,"monthlyCpa":{"counts":counts,"unmappedManagers":unmapped}}
    failed_ib_sales = sorted(name.split("/", 1)[1] for name, unit in units.items() if name.startswith("IB/") and unit["status"] == "FAIL")
    if a.apply and failed_ib_sales:
        result["larkWrite"] = False
        result["writeBlocked"] = {"reason": "incomplete_ib_source", "failedSales": failed_ib_sales}
        print(json.dumps(result,ensure_ascii=False,indent=2))
        raise RuntimeError(f"refusing Lark write with failed IB source units: {', '.join(failed_ib_sales)}")
    if a.apply:
        now=int(datetime.now(timezone.utc).timestamp()*1000)
        for kind in ("CREATE","UPDATE"):
            rs=[x for x in actions if x["classification"]==kind]; records=[{"fields":payload(x,now)} if kind=="CREATE" else {"record_id":x["recordId"],"fields":payload(x,now)} for x in rs]
            for i in range(0,len(records),100):l.request("POST",f"/bitable/v1/apps/{l.app}/tables/{TABLE}/records/batch_{kind.lower()}",{"records":records[i:i+100]})
        after,check=index_lark(lark_records(l,TABLE),a.month)
        if any(x["classification"]=="DUPLICATE_KEY" for x in check) or any(key(x) not in after or metrics(after[key(x)]["fields"])!={m:x[m] for m in METRICS} for x in actions):raise RuntimeError("read-back failed")
        if any(k not in after or json.dumps(after[k]["fields"],ensure_ascii=False,sort_keys=True)!=before for k,before in protected.items()):raise RuntimeError("read-back changed a skipped or failed-unit record")
        result["readBack"]={"businessKeyUnique":True,"actionsVerified":len(actions),"skippedAndFailedRecordsPreserved":len(protected)}
        if counts is not None:result["summaryCpa"]=summary_cpa_write(l,counts,a.month)
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=="__main__":main()
