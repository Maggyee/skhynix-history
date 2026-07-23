from __future__ import annotations
import json, logging, time
from datetime import datetime, timezone
import numpy as np, pandas as pd
from .config import ROOT, load_config
from .http import CachedHTTP, pdnow

ENDPOINTS={
"binance":"https://fapi.binance.com","bitget":"https://api.bitget.com",
"okx":"https://www.okx.com","gate":"https://api.gateio.ws/api/v4",
"hyperliquid":"https://api.hyperliquid.xyz/info"}

def ms(ts): return int(pd.Timestamp(ts).timestamp()*1000)
def num(x):
    try:return float(x)
    except:return np.nan

def metadata_row(ex, requested, resolved, raw, **kw):
    def exact(v): return None if v is None else str(v)
    return {"exchange":ex,"requested_symbol":requested,"resolved_symbol":resolved,"status":kw.pop("status","active"),"listing_time":kw.pop("listing_time",pd.NaT),"quote_currency":kw.pop("quote_currency",None),"collateral_currency":kw.pop("collateral_currency",None),"contract_type":kw.pop("contract_type","perpetual"),"contract_multiplier":exact(kw.pop("contract_multiplier",None)),"price_tick":exact(kw.pop("price_tick",None)),"quantity_step":exact(kw.pop("quantity_step",None)),"funding_interval_current":exact(kw.pop("funding_interval_current",None)),"metadata_retrieved_at":pdnow(),"raw_metadata":json.dumps(raw,ensure_ascii=False),**kw}

def candle(ex,symbol,typ,row,endpoint,rawfile,layout):
    if layout=="array": t,o,h,l,c,v,q,ct=row
    else:t,o,h,l,c,v,q,ct=row["t"],row["o"],row["h"],row["l"],row["c"],row.get("v"),row.get("sum"),row.get("T",int(row["t"])+59999)
    t=int(t); t=t*1000 if t<10**12 else t; ct=int(ct); ct=ct*1000 if ct<10**12 else ct
    return {"exchange":ex,"symbol":symbol,"price_type":typ,"open_time":pd.to_datetime(t,unit="ms",utc=True),"close_time":pd.to_datetime(ct,unit="ms",utc=True),"open":num(o),"high":num(h),"low":num(l),"close":num(c),"volume_base":num(v),"volume_quote":num(q),"source_endpoint":endpoint,"retrieved_at":pdnow(),"raw_file":rawfile}

def discover_all():
    cfg=load_config(); out=[]; errors={}
    # Binance
    try:
        h=CachedHTTP("binance"); d,f=h.get(ENDPOINTS["binance"]+"/fapi/v1/exchangeInfo"); x=next(x for x in d["symbols"] if x["symbol"]==cfg["symbols"]["binance"]); fs={z["filterType"]:z for z in x["filters"]}
        out.append(metadata_row("binance",cfg["symbols"]["binance"],x["symbol"],x,listing_time=pd.to_datetime(x["onboardDate"],unit="ms",utc=True),quote_currency=x["quoteAsset"],collateral_currency=x["marginAsset"],contract_type=x["contractType"],contract_multiplier=1,price_tick=fs["PRICE_FILTER"]["tickSize"],quantity_step=fs["LOT_SIZE"]["stepSize"],funding_interval_current=4))
    except Exception as e: errors["binance"]=str(e)
    try:
        h=CachedHTTP("bitget"); d,f=h.get(ENDPOINTS["bitget"]+"/api/v2/mix/market/contracts",{"productType":"USDT-FUTURES"}); x=next(x for x in d["data"] if x["symbol"]==cfg["symbols"]["bitget"])
        out.append(metadata_row("bitget",cfg["symbols"]["bitget"],x["symbol"],x,listing_time=pd.to_datetime(int(x["openTime"]),unit="ms",utc=True),quote_currency=x["quoteCoin"],collateral_currency=",".join(x["supportMarginCoins"]),contract_type=x["symbolType"]+("_RWA" if x.get("isRwa")=="YES" else ""),contract_multiplier=x["sizeMultiplier"],price_tick=10**-int(x["pricePlace"]),quantity_step=x["sizeMultiplier"],funding_interval_current=float(x["fundInterval"])))
    except Exception as e: errors["bitget"]=str(e)
    try:
        h=CachedHTTP("okx"); d,f=h.get(ENDPOINTS["okx"]+"/api/v5/public/instruments",{"instType":"SWAP"}); x=next(x for x in d["data"] if x["instId"]==cfg["symbols"]["okx"])
        out.append(metadata_row("okx",cfg["symbols"]["okx"],x["instId"],x,listing_time=pd.to_datetime(int(x["listTime"]),unit="ms",utc=True),quote_currency="USDT",collateral_currency=x["settleCcy"],contract_type=x["ctType"]+"_swap",contract_multiplier=x["ctVal"],price_tick=x["tickSz"],quantity_step=x["lotSz"],funding_interval_current=8))
    except Exception as e: errors["okx"]=str(e)
    try:
        h=CachedHTTP("gate"); x,f=h.get(ENDPOINTS["gate"]+"/futures/usdt/contracts/"+cfg["symbols"]["gate"])
        out.append(metadata_row("gate",cfg["symbols"]["gate"],x["name"],x,listing_time=pd.to_datetime(int(x["create_time"]),unit="s",utc=True),quote_currency="USDT",collateral_currency="USDT",contract_type=x.get("type","direct")+"_perpetual",contract_multiplier=x["quanto_multiplier"],price_tick=x["order_price_round"],quantity_step=x["order_size_min"],funding_interval_current=float(x["funding_interval"])/3600))
    except Exception as e: errors["gate"]=str(e)
    try:
        h=CachedHTTP("hyperliquid"); dexes,_=h.post(ENDPOINTS["hyperliquid"],{"type":"perpDexs"}); candidates=[]
        for dex in dexes:
            name="" if dex is None else dex["name"]
            meta,_=h.post(ENDPOINTS["hyperliquid"],{"type":"meta",**({"dex":name} if name else {})})
            for i,x in enumerate(meta["universe"]):
                full=x["name"] if ":" in x["name"] or not name else name+":"+x["name"]
                if "SKHX" in full.upper() or "SKHYNIX" in full.upper(): candidates.append((name,i,full,x,meta))
        dex,i,full,x,meta=candidates[0]
        out.append(metadata_row("hyperliquid","xyz:SKHX|SKHX|SKHYNIX",full,{"dex":dex,"asset_index":i,"universe":x,"marginTables":meta.get("marginTables")},quote_currency="oracle-defined USD price unit",collateral_currency="USDC",contract_type="HIP-3 builder-deployed perpetual",contract_multiplier=1,quantity_step=10**-int(x["szDecimals"]),funding_interval_current=1,dex=dex,asset_index=i))
    except Exception as e: errors["hyperliquid"]=str(e)
    # Failed discoveries remain explicit rows.
    for ex,msg in errors.items(): out.append(metadata_row(ex,cfg["symbols"].get(ex,ex),None,{"error":msg},status="failed",error=msg))
    pd.DataFrame(out).to_parquet(ROOT/"data"/"normalized"/"instrument_metadata.parquet",index=False)
    (ROOT/"data"/"raw"/"discovery_errors.json").write_text(json.dumps(errors,ensure_ascii=False,indent=2))
    return pd.DataFrame(out),errors

def _binance(start,end,symbol):
    h=CachedHTTP("binance"); prices=[]; funding=[]
    specs=[("trade","/fapi/v1/klines","symbol"),("mark","/fapi/v1/markPriceKlines","symbol"),("index","/fapi/v1/indexPriceKlines","pair")]
    for typ,ep,key in specs:
        cur=ms(start)
        while cur<=ms(end):
            d,f=h.get(ENDPOINTS["binance"]+ep,{key:symbol,"interval":"1m","startTime":cur,"endTime":ms(end),"limit":1500})
            if not d:break
            for x in d:
                # mark/index arrays have volume fields but they are not trading volume
                prices.append(candle("binance",symbol,typ,[*x[:6],x[7] if len(x)>7 else None,x[6]],ep,f,"array"))
            nxt=int(d[-1][0])+60000
            if nxt<=cur:break
            cur=nxt
    cur=ms(start)
    while cur<=ms(end):
        d,f=h.get(ENDPOINTS["binance"]+"/fapi/v1/fundingRate",{"symbol":symbol,"startTime":cur,"endTime":ms(end),"limit":1000})
        if not d:break
        for x in d: funding.append({"exchange":"binance","symbol":symbol,"funding_time":pd.to_datetime(int(x["fundingTime"]),unit="ms",utc=True),"funding_rate":num(x["fundingRate"]),"funding_interval_hours":np.nan,"mark_price_if_available":num(x.get("markPrice")),"index_price_if_available":np.nan,"source_endpoint":"/fapi/v1/fundingRate","retrieved_at":pdnow(),"raw_file":f,"interval_source":"inferred_from_events"})
        nxt=int(d[-1]["fundingTime"])+1
        if nxt<=cur:break
        cur=nxt
    return prices,funding

def _bitget(start,end,symbol):
    h=CachedHTTP("bitget"); prices=[]; funding=[]
    # V3 current official path; type is documented as MARKET/MARK/INDEX.
    for typ,ptype in [("trade","market"),("mark","mark"),("index","index")]:
        cursor=ms(end)
        while cursor>=ms(start):
            params={"category":"USDT-FUTURES","symbol":symbol,"interval":"1m","endTime":cursor,"limit":100,"type":ptype}
            d,f=h.get(ENDPOINTS["bitget"]+"/api/v3/market/history-candles",params); arr=d.get("data",[])
            if not arr: break
            for x in arr:
                if int(x[0])>=ms(start): prices.append(candle("bitget",symbol,typ,[*x[:7],int(x[0])+59999],"/api/v3/market/history-candles",f,"array"))
            oldest=min(int(x[0]) for x in arr)
            if oldest>=cursor or oldest<=ms(start):break
            cursor=oldest-1
    for page in range(1,101):
        d,f=h.get(ENDPOINTS["bitget"]+"/api/v3/market/history-fund-rate",{"category":"USDT-FUTURES","symbol":symbol,"limit":100,"cursor":page}); arr=d.get("data",{}).get("resultList",[])
        if not arr:break
        for x in arr:
            t=int(x["fundingRateTimestamp"])
            if ms(start)<=t<=ms(end):funding.append({"exchange":"bitget","symbol":symbol,"funding_time":pd.to_datetime(t,unit="ms",utc=True),"funding_rate":num(x["fundingRate"]),"funding_interval_hours":np.nan,"mark_price_if_available":np.nan,"index_price_if_available":np.nan,"source_endpoint":"/api/v3/market/history-fund-rate","retrieved_at":pdnow(),"raw_file":f,"interval_source":"inferred_from_events"})
        if min(int(x["fundingRateTimestamp"]) for x in arr)<ms(start):break
    return prices,funding

def _okx(start,end,symbol):
    h=CachedHTTP("okx"); prices=[]; funding=[]
    specs=[("trade","/api/v5/market/history-candles",symbol),("mark","/api/v5/market/history-mark-price-candles",symbol),("index","/api/v5/market/history-index-candles",symbol.replace("-SWAP",""))]
    for typ,ep,inst in specs:
        cursor=None
        while True:
            params={"instId":inst,"bar":"1m","limit":100}
            if cursor is not None: params["after"]=cursor
            d,f=h.get(ENDPOINTS["okx"]+ep,params); arr=d.get("data",[])
            if not arr:break
            for x in arr:
                t=int(x[0])
                if ms(start)<=t<=ms(end):
                    # OKX trade: vol at 5, quote vol 7. mark/index have no volume.
                    v=x[5] if len(x)>5 else None; q=x[7] if len(x)>7 else None
                    prices.append(candle("okx",symbol,typ,[x[0],x[1],x[2],x[3],x[4],v,q,t+59999],ep,f,"array"))
            oldest=min(int(x[0]) for x in arr)
            if oldest<=ms(start) or str(oldest)==str(cursor):break
            cursor=str(oldest)
    cursor=None
    while True:
        params={"instId":symbol,"limit":100}
        if cursor is not None:params["after"]=cursor
        d,f=h.get(ENDPOINTS["okx"]+"/api/v5/public/funding-rate-history",params); arr=d.get("data",[])
        if not arr:break
        for x in arr:
            t=int(x["fundingTime"])
            if ms(start)<=t<=ms(end): funding.append({"exchange":"okx","symbol":symbol,"funding_time":pd.to_datetime(t,unit="ms",utc=True),"funding_rate":num(x.get("realizedRate") or x["fundingRate"]),"funding_interval_hours":np.nan,"mark_price_if_available":np.nan,"index_price_if_available":np.nan,"source_endpoint":"/api/v5/public/funding-rate-history","retrieved_at":pdnow(),"raw_file":f,"interval_source":"inferred_from_events"})
        oldest=min(int(x["fundingTime"]) for x in arr)
        if oldest<=ms(start) or str(oldest)==str(cursor):break
        cursor=str(oldest)
    return prices,funding

def _gate(start,end,symbol):
    h=CachedHTTP("gate"); prices=[]; funding=[]
    for typ,prefix in [("trade",""),("mark","mark_"),("index","index_")]:
        end_s=int(pd.Timestamp(end).timestamp())
        # Gate currently rejects 1m candles older than its most recent 10,000 points.
        # Leave safety room for wall-clock movement and align to a minute boundary.
        cur=max(int(pd.Timestamp(start).timestamp()),int(time.time())-9400*60); cur=cur//60*60
        while cur<=end_s:
            upto=min(end_s,cur+1900*60)
            ep="/futures/usdt/candlesticks"
            d,f=h.get(ENDPOINTS["gate"]+ep,{"contract":prefix+symbol,"from":cur,"to":upto,"interval":"1m"})
            for x in d if isinstance(d,list) else []:
                prices.append(candle("gate",symbol,typ,x,ep,f,"dict"))
            cur=upto+1
    cursor=int(pd.Timestamp(end).timestamp())
    while cursor>=int(pd.Timestamp(start).timestamp()):
        ep="/futures/usdt/funding_rate"
        d,f=h.get(ENDPOINTS["gate"]+ep,{"contract":symbol,"to":cursor,"limit":1000})
        if not d:break
        for x in d:
            t=int(x["t"]); 
            if int(pd.Timestamp(start).timestamp())<=t<=int(pd.Timestamp(end).timestamp()): funding.append({"exchange":"gate","symbol":symbol,"funding_time":pd.to_datetime(t,unit="s",utc=True),"funding_rate":num(x["r"]),"funding_interval_hours":np.nan,"mark_price_if_available":np.nan,"index_price_if_available":np.nan,"source_endpoint":ep,"retrieved_at":pdnow(),"raw_file":f,"interval_source":"inferred_from_events"})
        oldest=min(int(x["t"]) for x in d)
        if oldest>=cursor or oldest<int(pd.Timestamp(start).timestamp()):break
        cursor=oldest-1
    return prices,funding

def _hyperliquid(start,end,symbol):
    h=CachedHTTP("hyperliquid"); prices=[]; funding=[]; ep=ENDPOINTS["hyperliquid"]
    # Official API retains at most the latest 5000 1m candles. Never synthesize older bars.
    d,f=h.post(ep,{"type":"candleSnapshot","req":{"coin":symbol,"interval":"1m","startTime":ms(start),"endTime":ms(end)}})
    for x in d: prices.append(candle("hyperliquid",symbol,"trade",x,"POST /info candleSnapshot",f,"dict"))
    cur=ms(start)
    while cur<=ms(end):
        d,f=h.post(ep,{"type":"fundingHistory","coin":symbol,"startTime":cur,"endTime":ms(end)})
        if not d:break
        for x in d:
            t=int(x["time"])
            funding.append({"exchange":"hyperliquid","symbol":symbol,"funding_time":pd.to_datetime(t,unit="ms",utc=True),"funding_rate":num(x["fundingRate"]),"funding_interval_hours":1.0,"mark_price_if_available":np.nan,"index_price_if_available":np.nan,"source_endpoint":"POST /info fundingHistory","retrieved_at":pdnow(),"raw_file":f,"interval_source":"official_hourly_events"})
        nxt=int(d[-1]["time"])+1
        if nxt<=cur:break
        cur=nxt
    return prices,funding

DOWNLOADERS={"binance":_binance,"bitget":_bitget,"okx":_okx,"gate":_gate,"hyperliquid":_hyperliquid}

def download_all(start,end):
    meta,discovery_errors=discover_all(); cfg=load_config(); allp=[]; allf=[]; errors=dict(discovery_errors)
    oldp=pd.read_parquet(ROOT/"data"/"normalized"/"prices_1m.parquet") if (ROOT/"data"/"normalized"/"prices_1m.parquet").exists() else pd.DataFrame()
    oldf=pd.read_parquet(ROOT/"data"/"normalized"/"funding_events.parquet") if (ROOT/"data"/"normalized"/"funding_events.parquet").exists() else pd.DataFrame()
    if len(oldp): allp.extend(oldp.to_dict("records"))
    if len(oldf): allf.extend(oldf.drop(columns=["hourly_equivalent_rate"],errors="ignore").to_dict("records"))
    logging.info("download range %s %s",start,end)
    for ex,fn in DOWNLOADERS.items():
        if ex in discovery_errors:continue
        symbol=meta.loc[(meta.exchange==ex)&(meta.status!="failed"),"resolved_symbol"].iloc[0]
        try:
            resume=start
            prior=oldp[oldp.exchange==ex] if len(oldp) else pd.DataFrame()
            if len(prior):
                # Resume from the least advanced available price type so no type is skipped.
                resume=max(pd.Timestamp(start),prior.groupby("price_type").open_time.max().min()+pd.Timedelta(minutes=1))
            p,f=fn(resume,end,symbol); allp.extend(p); allf.extend(f)
            logging.info("%s prices=%d funding=%d",ex,len(p),len(f))
        except Exception as e:
            errors[ex]=str(e); logging.exception("exchange failed: %s",ex)
    prices=pd.DataFrame(allp)
    funding=pd.DataFrame(allf)
    if not prices.empty:
        prices=prices[(prices.close>0)&np.isfinite(prices.close)].sort_values(["exchange","price_type","open_time"]).drop_duplicates(["exchange","symbol","price_type","open_time"],keep="last")
        prices.to_parquet(ROOT/"data"/"normalized"/"prices_1m.parquet",index=False)
    if not funding.empty:
        funding=funding.sort_values(["exchange","funding_time"]).drop_duplicates(["exchange","symbol","funding_time"],keep="last")
        # Infer each exchange's historical interval from actual adjacent events.
        for ex,g in funding.groupby("exchange"):
            ix=g.index; dif=g.funding_time.sort_values().diff().dt.total_seconds().div(3600)
            inferred=dif.where(dif.between(.5,24)).median()
            missing=funding.loc[ix,"funding_interval_hours"].isna()
            funding.loc[ix[missing],"funding_interval_hours"]=inferred if pd.notna(inferred) else float(meta.loc[meta.exchange==ex,"funding_interval_current"].iloc[0])
        funding["hourly_equivalent_rate"]=funding.funding_rate/funding.funding_interval_hours
        funding.to_parquet(ROOT/"data"/"normalized"/"funding_events.parquet",index=False)
    (ROOT/"data"/"raw"/"download_errors.json").write_text(json.dumps(errors,ensure_ascii=False,indent=2))
    return prices,funding,errors
