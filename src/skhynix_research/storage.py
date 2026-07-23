from __future__ import annotations
from pathlib import Path
import duckdb, pandas as pd
from .config import ROOT

def write_parquet(df: pd.DataFrame, name: str):
    p=ROOT/"data"/"normalized"/name; p.parent.mkdir(parents=True,exist_ok=True)
    df.drop_duplicates().to_parquet(p,index=False)
    return p

def build_duckdb():
    db=duckdb.connect(str(ROOT/"data"/"research.duckdb"))
    for p in (ROOT/"data"/"normalized").glob("*.parquet"):
        table=p.stem
        db.execute(f'CREATE OR REPLACE TABLE "{table}" AS SELECT * FROM read_parquet(?)',[str(p)])
    db.close()

