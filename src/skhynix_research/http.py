from __future__ import annotations
import hashlib, json, logging, random, time
from datetime import datetime
from pathlib import Path
import httpx
from .config import ROOT

class CachedHTTP:
    def __init__(self, exchange: str, timeout=25, attempts=5, delay=.08, archive_ndjson=False, ttl=None):
        self.exchange, self.attempts, self.delay, self.archive_ndjson = exchange, attempts, delay, archive_ndjson
        self.ttl = ttl
        self.last_retrieved_at = None
        self.last_response_path = None
        self.last_from_cache = False
        self.client=httpx.Client(timeout=timeout,headers={"User-Agent":"skhynix-public-research/0.1"})
        self.dir=ROOT/"data"/"raw"/exchange; self.dir.mkdir(parents=True,exist_ok=True)
        self.log=logging.getLogger("download")
    @staticmethod
    def _ttl_seconds(ttl):
        return ttl.total_seconds() if hasattr(ttl, "total_seconds") else float(ttl)

    def _cache_is_fresh(self, path, payload, ttl):
        if ttl is None:
            return True
        retrieved_at = payload.get("retrieved_at") if isinstance(payload, dict) else None
        try:
            cached_at = datetime.fromisoformat(retrieved_at).timestamp()
        except (TypeError, ValueError):
            cached_at = path.stat().st_mtime
        return time.time() - cached_at <= self._ttl_seconds(ttl)

    def request(self, method, url, *, params=None, json_body=None, force_refresh=False, ttl=None):
        key=hashlib.sha256(json.dumps([method,url,params,json_body],sort_keys=True,default=str).encode()).hexdigest()
        path=self.dir/f"{key}.json"
        effective_ttl = self.ttl if ttl is None else ttl
        if path.exists() and not self.archive_ndjson and not force_refresh:
            try:
                cached=json.loads(path.read_text())
                if self._cache_is_fresh(path, cached, effective_ttl):
                    self.last_retrieved_at = cached.get("retrieved_at")
                    self.last_response_path = str(path.relative_to(ROOT))
                    self.last_from_cache = True
                    return cached.get("response",cached),self.last_response_path
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
                payload={"request":{"method":method,"url":url,"params":params,"json":json_body},"retrieved_at":pdnow(),"response":data}
                if self.archive_ndjson:
                    path=self.dir/f"{payload['retrieved_at'][:10]}.ndjson"
                    with path.open("a") as handle: handle.write(json.dumps(payload,ensure_ascii=False)+"\n")
                else:
                    path.write_text(json.dumps(payload,ensure_ascii=False))
                self.last_retrieved_at = payload["retrieved_at"]
                self.last_response_path = str(path.relative_to(ROOT))
                self.last_from_cache = False
                return data,self.last_response_path
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
    def get(self,url,params=None,*,force_refresh=False,ttl=None):
        return self.request("GET",url,params=params,force_refresh=force_refresh,ttl=ttl)
    def post(self,url,body,*,force_refresh=False,ttl=None):
        return self.request("POST",url,json_body=body,force_refresh=force_refresh,ttl=ttl)

def pdnow():
    from datetime import datetime,timezone
    return datetime.now(timezone.utc).isoformat()
