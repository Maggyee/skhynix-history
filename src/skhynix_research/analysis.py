from __future__ import annotations
import numpy as np
import pandas as pd

def symmetric_spread_bps(a, b):
    return 10000.0 * 2.0 * (a - b) / (a + b)

def hourly_equivalent(rate, hours):
    return rate / hours

def infer_intervals(times: pd.Series, default: float) -> pd.Series:
    d = pd.to_datetime(times, utc=True).sort_values().diff().dt.total_seconds().div(3600)
    return d.where(d.between(0.5, 24), default).fillna(default)

def detect_scale_mismatch(a: pd.Series, b: pd.Series) -> tuple[bool, float]:
    ratio = (a / b).replace([np.inf, -np.inf], np.nan).dropna()
    if len(ratio) < 10: return False, np.nan
    med = float(ratio.median())
    stable = float((ratio / med - 1).abs().median()) < 0.01
    mismatch = stable and (med > 1.5 or med < 0.67)
    return mismatch, med

def align_prices(prices: pd.DataFrame) -> pd.DataFrame:
    """Backward-only asof fill, maximum age two minutes."""
    frames=[]
    for (ex, typ), g in prices.groupby(["exchange","price_type"]):
        g=g.sort_values("open_time").drop_duplicates("open_time", keep="last")
        idx=pd.date_range(g.open_time.min().floor("min"), g.open_time.max().floor("min"),freq="1min",tz="UTC")
        z=pd.DataFrame({"minute":idx})
        src=g[["open_time","close"]].rename(columns={"open_time":"source_time","close":"price"})
        z=pd.merge_asof(z,src.sort_values("source_time"),left_on="minute",right_on="source_time",direction="backward",tolerance=pd.Timedelta("2min"))
        z["age_seconds"]=(z.minute-z.source_time).dt.total_seconds()
        z["exchange"],z["price_type"]=ex,typ
        frames.append(z)
    return pd.concat(frames,ignore_index=True) if frames else pd.DataFrame()

def threshold_events(df: pd.DataFrame, threshold: float) -> list[dict]:
    if df.empty: return []
    g=df.sort_values("minute").copy(); active=g.abs_spread>=threshold
    rows=g.loc[active]
    if rows.empty:return []
    groups=(rows.minute.diff().dt.total_seconds().div(60).fillna(99)>2).cumsum()
    out=[]
    for _,x in rows.groupby(groups):
        peak=x.loc[x.abs_spread.idxmax()]
        out.append({"event_start":x.minute.min(),"event_end":x.minute.max(),"duration_minutes":int((x.minute.max()-x.minute.min()).total_seconds()/60)+1,"peak_abs_spread_bps":peak.abs_spread,"peak_time":peak.minute,"spread_at_end_bps":x.iloc[-1].spread})
    return out

