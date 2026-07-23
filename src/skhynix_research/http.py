from __future__ import annotations
import hashlib, json, logging, random, time
from pathlib import Path
import httpx
from .config import ROOT

class CachedHTTP:
    def __init__(self, exchange: str, timeout=25, attempts=5, delay=.08):
        self.exchange, self.attempts, self.delay = exchange, attempts, delay
        self.client=httpx.Client(timeout=timeout,headers={"User-Agent":"skhynix-public-research/0.1"})
        self.dir=ROOT/"data"/"raw"/exchange; self.dir.mkdir(parents=True,exist_ok=True)
        self.log=logging.getLogger("download")
    def request(self, method, url, *, params=None, json_body=None):
        key=hashlib.sha256(json.dumps([method,url,params,json_body],sort_keys=True,default=str).encode()).hexdigest()
        path=self.dir/f"{key}.json"
        if path.exists():
            try:
                cached=json.loads(path.read_text())
                return cached.get("response",cached),str(path.relative_to(ROOT))
            except Exception: pass
        err=None
        for attempt in range(1,self.attempts+1):
            try:
                time.sleep(self.delay)
                r=self.client.request(method,url,params=params,json=json_body)
                if r.status_code==429 or r.status_code>=500:
                    wait=float(r.headers.get("retry-after",min(16,2**(attempt-1))))
                    raise httpx.HTTPStatusError(f"retryable {r.status_code}; wait={wait}",request=r.request,response=r)
                r.raise_for_status(); data=r.json()
                path.write_text(json.dumps({"request":{"method":method,"url":url,"params":params,"json":json_body},"retrieved_at":pdnow(),"response":data},ensure_ascii=False))
                return data,str(path.relative_to(ROOT))
            except Exception as e:
                err=e; status=getattr(getattr(e,"response",None),"status_code",None)
                self.log.error(json.dumps({"exchange":self.exchange,"endpoint":url,"parameters_without_secrets":params or json_body,"status_code":status,"attempt":attempt,"error":str(e),"timestamp":pdnow()},default=str))
                if status and 400 <= status < 500 and status != 429:
                    try:
                        ep=self.dir/f"{key}.error.json"
                        ep.write_text(json.dumps({"request":{"method":method,"url":url,"params":params,"json":json_body},"status_code":status,"response_text":e.response.text,"retrieved_at":pdnow()},ensure_ascii=False))
                    except Exception: pass
                    break
                if attempt<self.attempts: time.sleep(min(16,2**(attempt-1))+random.random()/4)
        raise RuntimeError(f"{self.exchange} {url}: {err}")
    def get(self,url,params=None): return self.request("GET",url,params=params)
    def post(self,url,body): return self.request("POST",url,json_body=body)

def pdnow():
    from datetime import datetime,timezone
    return datetime.now(timezone.utc).isoformat()
