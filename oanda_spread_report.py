#!/usr/bin/env python3
import os,json,csv
from pathlib import Path
from datetime import datetime,timezone,timedelta
import requests

REST_URL=os.getenv("OANDA_BASE_URL","https://api-fxpractice.oanda.com").rstrip("/")
TOKEN=(os.getenv("OANDA_API_TOKEN","") or os.getenv("OANDA_TOKEN","")).strip()
ACCOUNT=os.getenv("OANDA_ACCOUNT_ID","").strip()
DAYS=int(os.getenv("OANDA_REPORT_DAYS","30"))
OUT=Path(os.getenv("OANDA_OUTPUT_DIR","./oanda_output")); OUT.mkdir(parents=True,exist_ok=True)

def main():
    if not TOKEN: raise RuntimeError("OANDA token missing")
    s=requests.Session(); s.headers.update({"Authorization":f"Bearer {TOKEN}","Content-Type":"application/json"})
    if not ACCOUNT:
        a=s.get(f"{REST_URL}/v3/accounts",timeout=30); a.raise_for_status(); ACCOUNT=a.json()["accounts"][0]["id"]
    end=datetime.now(timezone.utc); start=end-timedelta(days=DAYS)
    u=f"{REST_URL}/v3/accounts/{ACCOUNT}/transactions"
    q={"from":start.isoformat().replace("+00:00","Z"),"to":end.isoformat().replace("+00:00","Z"),"pageSize":1000,"type":"ORDER_FILL"}
    r=s.get(u,params=q,timeout=60); r.raise_for_status(); meta=r.json()
    tx=[]
    for page in meta.get("pages",[]):
        x=s.get(page,timeout=60); x.raise_for_status(); tx.extend(x.json().get("transactions",[]))
    opens={}; trades=[]
    for t in tx:
        if t.get("type")!="ORDER_FILL": continue
        inst=t.get("instrument","")
        tag=""
        oe=t.get("tradeOpened")
        if oe:
            tid=str(oe.get("tradeID")); opens[tid]={"instrument":inst,"open_time":t.get("time"),"open_spread":float(oe.get("halfSpreadCost",0) or 0),"units":oe.get("units"),"order_id":t.get("orderID"),"tag":t.get("clientExtensions",{}).get("tag","")}
        for c in t.get("tradesClosed",[]) or []:
            tid=str(c.get("tradeID")); o=opens.pop(tid,{})
            pl=float(c.get("realizedPL",0) or 0); fin=float(c.get("financing",0) or 0)
            sp=float(o.get("open_spread",0))+float(c.get("halfSpreadCost",0) or 0)
            net=pl+fin
            trades.append({"pair":inst,"tradeID":tid,"open_time":o.get("open_time"),"close_time":t.get("time"),"engine":o.get("tag",""),"realizedPL":pl,"financing":fin,"spread":sp,"net":net,"pre_spread":net+sp})
    fields=["pair","tradeID","open_time","close_time","engine","realizedPL","financing","spread","net","pre_spread"]
    with open(OUT/"oanda_trade_ledger.csv","w",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(trades)
    summary={}
    for z in trades:
        a=summary.setdefault(z["pair"],{"trades":0,"wins":0,"gp":0.0,"gl":0.0,"net":0.0,"spread":0.0,"pre":0.0})
        a["trades"]+=1; a["wins"]+=z["net"]>0; a["net"]+=z["net"]; a["spread"]+=z["spread"]; a["pre"]+=z["pre_spread"]
        if z["net"]>0:a["gp"]+=z["net"]
        elif z["net"]<0:a["gl"]+=z["net"]
    for p,a in summary.items():
        a["wr"]=100*a["wins"]/a["trades"] if a["trades"] else 0
        a["pf"]=a["gp"]/(-a["gl"]) if a["gl"] else 0
        a["trades"]=int(a["trades"]); a["wins"]=int(a["wins"])
    report={"account":ACCOUNT,"from":start.isoformat(),"to":end.isoformat(),"transaction_count":len(tx),"closed_trades":len(trades),"pairs":summary}
    with open(OUT/"oanda_spread_report.json","w") as f: json.dump(report,f,indent=2)
    print("[SPREAD REPORT]",json.dumps(report,separators=(",",":")),flush=True)
    print("[SPREAD REPORT FILE] oanda_output/oanda_spread_report.json",flush=True)
    print("[TRADE LEDGER FILE] oanda_output/oanda_trade_ledger.csv",flush=True)
if __name__=="__main__": main()
